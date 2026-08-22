from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from archivum.archgraph.cache import load_cached, save_cached
from archivum.archgraph.extract import extract_file
from archivum.archgraph.extractors.base import _file_namespace, _make_id
from archivum.archgraph.mapper import (
    CandidateArtifact,
    CandidateEntity,
    CandidateRelationship,
    Provenance,
    candidate_to_knowledge_object,
    candidate_to_knowledge_relationship,
    map_extraction,
)
from archivum.archgraph.models import Extraction
from archivum.archgraph.registry import CODE_SUFFIXES
from archivum.archgraph.repo import (
    changed_line_ranges,
    collect_files,
    repo_artifacts,
    snapshot_repo,
)
from archivum.archgraph.resolve import resolve_cross_file
from archivum.archgraph.cross_repo import resolve_cross_repo
from archivum.archgraph.bridge import bridge_evidence
from archivum.archgraph.lexical import build_lexical_index
from archivum.knowledge.repository import KnowledgeRepository


# Deriving cross-repo and evidence links means reading the whole store, so the
# scan is bounded the same way the graph audit and projection rebuild are.
_L1_SCAN_LIMIT = 100_000


@dataclass
class IngestReport:
    files: int
    nodes: int
    edges: int
    rejected: int
    cache_hits: int


class _KnowledgeL1View:
    """L1 read view over everything the vault knows, not just this run.

    This used to be built from the candidates of the ingest in progress, which
    made both derived resolvers unreachable. `resolve_cross_repo` compares
    scopes and one run only ever has one, so it could never emit; and
    `bridge_evidence` looks for conversation evidence, which a code-only run by
    definition does not contain. The comment here previously called that
    "evidence-gated" — but the gate could not open, because the view was reading
    from the wrong place rather than waiting on data that had not arrived.

    Distilled memory atoms are presented as conversation evidence: they are the
    owner's own recorded statements, already cited and already reviewed, which
    makes them the honest thing to link a symbol's existence to.
    """

    def __init__(self, objects: list[Any]) -> None:
        self._objects: list[dict] = []
        for object_ in objects:
            kind = object_.kind
            text = ""
            if kind == "memory_atom":
                kind = "conversation"
                text = str(object_.properties.get("text") or object_.label or "")
            self._objects.append(
                {
                    "id": object_.id,
                    "kind": kind,
                    "scope": object_.scope,
                    "label": object_.label,
                    "text": text,
                }
            )

    async def list_objects(self, kind: str | None = None, scope: str | None = None) -> list[dict]:
        return [
            o
            for o in self._objects
            if (kind is None or o.get("kind") == kind)
            and (scope is None or o.get("scope") == scope)
        ]


def _span_bounds(span: str) -> tuple[int, int] | None:
    """(start, end) line numbers from an `L12-L18` span."""
    parts = [part.strip().lstrip("Ll") for part in span.split("-") if part.strip()]
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[-1])


def _changed_in_edges(
    root: Path, snap, extractions: list[Extraction], *, scope: str
) -> list[CandidateRelationship]:
    """Link the commit to every symbol whose lines it touched."""
    ranges = changed_line_ranges(root, snap.commit_sha)
    if not ranges:
        return []

    commit_id = _make_id(snap.repo_id, snap.commit_sha)
    edges: list[CandidateRelationship] = []
    seen: set[str] = set()
    for extraction in extractions:
        for node in extraction.nodes:
            if node.kind not in ("symbol", "type") or node.id in seen:
                continue
            try:
                relative = Path(node.source_file).resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            touched = ranges.get(relative.as_posix())
            bounds = _span_bounds(node.source_location)
            if not touched or bounds is None:
                continue
            start, end = bounds
            if not any(hunk_end >= start and hunk_start <= end for hunk_start, hunk_end in touched):
                continue
            seen.add(node.id)
            edges.append(
                CandidateRelationship(
                    id=_make_id(node.id, commit_id, "changed_in"),
                    src_id=node.id,
                    dst_id=commit_id,
                    rel_type="changed_in",
                    scope=scope,
                    confidence=1.0,
                    extraction_method="EXTRACTED",
                    provenance=[
                        Provenance(
                            chunk_id=node.source_file,
                            span=node.source_location,
                            extraction_method="EXTRACTED",
                        )
                    ],
                )
            )
    return edges


def changed_files(root: Path, since_sha: str | None) -> tuple[list[Path], list[Path]]:
    """Return (changed_or_added, deleted) absolute Paths, code files only.

    Falls back to (collect_files(root), []) when since_sha is None,
    root is not a git repo, or any subprocess error occurs.
    """
    if since_sha is None:
        return collect_files(root), []

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-status", f"{since_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return collect_files(root), []
    except (FileNotFoundError, subprocess.SubprocessError):
        return collect_files(root), []

    changed: list[Path] = []
    deleted: list[Path] = []

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        rel_path = parts[1].strip()
        # Rename lines look like "R100\told_name\tnew_name" — index the new
        # name and clean stale records for the old namespace.
        if status.startswith("R"):
            old_path = root / rel_path
            if old_path.suffix in CODE_SUFFIXES:
                deleted.append(old_path)
            rel_path = parts[2].strip() if len(parts) >= 3 else rel_path
            status = "R"

        abs_path = root / rel_path
        if abs_path.suffix not in CODE_SUFFIXES:
            continue

        if status == "D":
            deleted.append(abs_path)
        else:
            changed.append(abs_path)

    return changed, deleted


def prune_dangling(candidates: list, deleted_files: set[str]) -> tuple[list, int]:
    """Drop candidates whose provenance ALL cite a deleted file.

    A candidate is pruned only when it has at least one provenance entry
    AND every provenance chunk_id is in deleted_files.
    Candidates with no provenance are kept.
    Returns (kept, pruned_count).
    """
    kept: list = []
    pruned_count = 0

    for candidate in candidates:
        provenance: list[Provenance] = getattr(candidate, "provenance", [])
        if not provenance:
            kept.append(candidate)
            continue

        all_deleted = all(p.chunk_id in deleted_files for p in provenance)
        if all_deleted:
            pruned_count += 1
        else:
            kept.append(candidate)

    return kept, pruned_count


async def ingest_repo(
    root: Path,
    *,
    scope: str,
    cache_dir: Path,
    knowledge: KnowledgeRepository,
    lexical_conn: aiosqlite.Connection,
    update: bool = False,
    since_sha: str | None = None,
    related_scopes: set[str] | None = None,
) -> IngestReport:
    """Read a repository into canonical knowledge.

    `related_scopes` bounds which other scopes may take part in derived links.
    Bridging deliberately reads beyond this run so a symbol can find a decision
    recorded months ago — but unbounded, that same sweep would manufacture a
    link from this repository into another vault's memory. Callers pass the
    repository's own scope plus the vault that owns it; omitting it means no
    other scope participates.
    """
    # Step 1: snapshot repo and collect repo-level artifacts
    snap = snapshot_repo(root)
    repo_cands = repo_artifacts(snap, scope=scope)

    # Step 2: collect files (full or incremental) and extract (or load from cache)
    if update:
        files, deleted = changed_files(root, since_sha)
    else:
        files = collect_files(root)
        deleted = []

    all_extractions: list[Extraction] = []
    file_chunk_ids: list[tuple[Path, str]] = []
    cache_hits = 0

    for file in files:
        # Use str(file) as chunk_id so prune_dangling can match by file path
        chunk_id = str(file)
        cache_namespace = _file_namespace(file, root=root, scope=scope)
        ext = load_cached(file, cache_dir, namespace=cache_namespace)
        if ext is None:
            ext = extract_file(file, root=root, scope=scope)
            save_cached(file, ext, cache_dir, namespace=cache_namespace)
        else:
            cache_hits += 1
        all_extractions.append(ext)
        file_chunk_ids.append((file, chunk_id))

    # Step 3: resolve cross-file edges. Anchor each edge's provenance to ITS OWN
    # source file (the calling site), not a synthetic repo-level key — otherwise
    # a cross-file edge whose source file is later deleted would be un-prunable on
    # --update and dangle forever. Grouping by source_file keeps chunk_id file-
    # addressable so prune_dangling reaches it. Sorted for deterministic emission.
    inferred_edges = resolve_cross_file(all_extractions)
    edges_by_file: dict[str, list] = {}
    for edge in inferred_edges:
        edges_by_file.setdefault(edge.source_file, []).append(edge)
    for src_file in sorted(edges_by_file):
        all_extractions.append(Extraction(nodes=[], edges=edges_by_file[src_file], error=None))
        file_chunk_ids.append((Path(src_file), src_file))

    # Step 4: map all extractions into candidates
    all_candidates: list[object] = list(repo_cands)
    for (file_or_root, chunk_id), ext in zip(file_chunk_ids, all_extractions):
        mapped = map_extraction(ext, scope=scope, chunk_id=chunk_id)
        all_candidates.extend(mapped)

    # Step 4b: attribute the commit to the symbols whose lines it changed.
    # Attributing it to every symbol in a touched file would say a module moved;
    # this says which function did, which is what "what changed and why" needs.
    all_candidates.extend(
        _changed_in_edges(root, snap, all_extractions, scope=scope)
    )

    # Step 5 (incremental only): prune candidates from deleted files and remove
    # stale canonical records from every touched file before current upserts.
    if update and deleted:
        deleted_strs = {str(p) for p in deleted}
        all_candidates, _ = prune_dangling(all_candidates, deleted_strs)

    if update:
        touched_strs = {str(p) for p in files}
        touched_strs.update(str(p) for p in deleted)
        await knowledge.delete_records_with_only_citations_in(
            scope=scope, chunk_ids=touched_strs
        )

    # Step 6: persist canonical knowledge records and their provenance.
    #
    # A call site knows only the bare name it called, so an edge whose target
    # could not be resolved to a real symbol points at nothing. Those used to be
    # stored anyway, which inflated the edge count and left the graph full of
    # pointers to records that do not exist.
    known_ids = {
        candidate.id
        for candidate in all_candidates
        if isinstance(candidate, (CandidateEntity, CandidateArtifact))
    }
    resolved_candidates: list[object] = []
    for candidate in all_candidates:
        if isinstance(candidate, CandidateRelationship) and not (
            candidate.src_id in known_ids and candidate.dst_id in known_ids
        ):
            continue
        resolved_candidates.append(candidate)
    all_candidates = resolved_candidates

    for candidate in all_candidates:
        if isinstance(candidate, (CandidateEntity, CandidateArtifact)):
            await knowledge.upsert_object(candidate_to_knowledge_object(candidate))
        elif isinstance(candidate, CandidateRelationship):
            await knowledge.upsert_relationship(candidate_to_knowledge_relationship(candidate))
    accepted = all_candidates

    # Step 7: resolve derived relationships against everything the vault knows.
    # Reading the whole store rather than this run is what lets a symbol link to
    # a decision recorded months ago, and lets two repos recognise a shared type.
    canonical_objects = await knowledge.list_objects(limit=_L1_SCAN_LIMIT)
    linkable_scopes = {scope, *(related_scopes or set())}
    l1_view = _KnowledgeL1View(
        [object_ for object_ in canonical_objects if object_.scope in linkable_scopes]
    )
    cross_repo_rels = await resolve_cross_repo(l1_view)
    bridge_rels = await bridge_evidence(l1_view)
    extra_rels: list[object] = [*cross_repo_rels, *bridge_rels]
    if extra_rels:
        for relationship in extra_rels:
            await knowledge.upsert_relationship(candidate_to_knowledge_relationship(relationship))
        accepted.extend(extra_rels)

    # Step 8: rebuild lexical index from canonical code objects in this scope.
    # Incremental runs only extract touched files, but lexical is a full
    # projection and its builder clears existing rows before repopulating.
    code_nodes = [
        (object_.id, object_.label)
        for object_ in canonical_objects
        if object_.properties.get("source_scope") == scope
    ]
    await build_lexical_index(lexical_conn, code_nodes)

    # Count distinct records, not candidates. Two call sites for the same callee
    # produce two candidates and one relationship, so counting candidates made
    # the CLI report more edges than the store actually held.
    nodes_accepted = len(
        {c.id for c in accepted if isinstance(c, (CandidateEntity, CandidateArtifact))}
    )
    edges_accepted = len(
        {c.id for c in accepted if isinstance(c, CandidateRelationship)}
    )

    return IngestReport(
        files=len(files),
        nodes=nodes_accepted,
        edges=edges_accepted,
        rejected=0,
        cache_hits=cache_hits,
    )
