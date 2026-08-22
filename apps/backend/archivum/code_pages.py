"""Turn a repository's code graph into pages you own.

Everything else in Archivum ends up as markdown in the vault. Code was the one
exception: it produced canonical records and a lexical index and no files at
all, which meant the only part of a developer's memory that could not be read,
edited, linked or exported was the part about their own work.

So the graph is written down the way a person would want to read it — one page
per community, plus an index — and then handed to the same `reindex_page` path
that every other write uses. From that moment the code graph is ordinary vault
content: backlinked, searchable, editable, and portable if Archivum goes away.

Communities come from the existing deterministic graph audit rather than a
model, so the same repository always produces the same pages, and rewriting
them is a no-op when nothing changed.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from archivum.config import Settings
from archivum.db import sqlite
from archivum.indexing import ensure_frontmatter, reindex_page
from archivum.knowledge.graph_audit import (
    Community,
    GraphReport,
    audit_knowledge_graph,
)
from archivum.knowledge.models import KnowledgeObject
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from archivum.code_repos import CodeRepo

logger = logging.getLogger(__name__)

# A community with one member is a fact about that member, not a cluster worth
# its own page; it stays listed on the index instead.
MIN_COMMUNITY_SIZE = 2

# The repository node and its commit cluster together and describe nothing about
# the code. A page titled after the repository holding only "this repo, that
# commit" would compete with the index for the same name and the same reader.
_BOOKKEEPING_KINDS = frozenset({"repo", "commit"})

# Enough to show the shape of a cluster without pasting the repository into the
# vault. The canonical records remain the complete answer.
MAX_MEMBERS_PER_PAGE = 60
MAX_SURPRISING = 10

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-") or "untitled"


def repo_folder(name: str) -> str:
    return f"code/{slugify(name)}"


def _lines(span: str) -> str:
    """`L12-L18` as the reader would write it: `12-18`."""
    parts = [part.strip().lstrip("Ll") for part in span.split("-") if part.strip()]
    if not all(part.isdigit() for part in parts) or not parts:
        return ""
    # A single-line span reads better as one number than as `12-12`.
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    return "-".join(parts)


def _location(object_: KnowledgeObject, *, repo_root: str = "") -> str:
    """Where this record came from, as `path:lines`, relative to the repository.

    Citations store the absolute path the file had on the machine that indexed
    it. Printing that verbatim leaks the deployment's filesystem layout into the
    vault and buries the only part a reader wants — which file, which lines.
    """
    if not object_.citations:
        return ""
    citation = object_.citations[0]
    path = citation.chunk_id or ""
    if repo_root and path.startswith(repo_root):
        path = path[len(repo_root) :].lstrip("/")
    lines = _lines(citation.quote or "")
    return f"{path}:{lines}" if path and lines else path


def render_community_page(
    community: Community,
    *,
    repo_name: str,
    objects_by_id: dict[str, KnowledgeObject],
    edges: list,
    repo_root: str = "",
) -> str:
    members = [objects_by_id[mid] for mid in community.member_ids if mid in objects_by_id]
    members.sort(key=lambda object_: (object_.kind, object_.label))
    shown = members[:MAX_MEMBERS_PER_PAGE]

    lines = [
        f"# {community.label}",
        "",
        f"Part of [[{repo_folder(repo_name)}/index|{repo_name}]]. "
        f"{len(members)} record{'s' if len(members) != 1 else ''} that the graph "
        "found to be more connected to each other than to the rest of the repository.",
        "",
        "## What is in here",
        "",
    ]

    for object_ in shown:
        where = _location(object_, repo_root=repo_root)
        suffix = f" — `{where}`" if where else ""
        lines.append(f"- **{object_.label}** ({object_.kind}){suffix}")
    if len(members) > len(shown):
        lines.append(f"- …and {len(members) - len(shown)} more.")

    member_ids = set(community.member_ids)
    internal = [
        edge
        for edge in edges
        if edge.src_id in member_ids and edge.dst_id in member_ids
    ]
    if internal:
        lines += ["", "## How they connect", ""]
        for edge in sorted(internal, key=lambda e: (e.rel_type, e.src_id, e.dst_id))[:MAX_MEMBERS_PER_PAGE]:
            src = objects_by_id.get(edge.src_id)
            dst = objects_by_id.get(edge.dst_id)
            if src is None or dst is None:
                continue
            lines.append(
                f"- {src.label} → *{edge.rel_type}* → {dst.label} "
                f"({edge.extraction_method.lower()})"
            )

    # Edges that leave the cluster are the interesting part: they are where this
    # piece of the repository touches everything else, including decisions.
    outward = [
        edge
        for edge in edges
        if (edge.src_id in member_ids) != (edge.dst_id in member_ids)
    ]
    if outward:
        lines += ["", "## What it reaches", ""]
        for edge in sorted(outward, key=lambda e: (e.rel_type, e.src_id, e.dst_id))[:MAX_SURPRISING]:
            src = objects_by_id.get(edge.src_id)
            dst = objects_by_id.get(edge.dst_id)
            src_label = src.label if src else edge.src_id
            dst_label = dst.label if dst else edge.dst_id
            lines.append(f"- {src_label} → *{edge.rel_type}* → {dst_label}")

    lines += [
        "",
        "---",
        "",
        "Generated from the code graph. Edit freely — your words are kept, and "
        "reindexing will not overwrite a page you have taken over.",
        "",
    ]
    return "\n".join(lines)


def render_index_page(
    report: GraphReport,
    *,
    repo_name: str,
    communities: list[Community],
    fixes: list[KnowledgeObject] | None = None,
) -> str:
    lines = [
        f"# {repo_name}",
        "",
        # Deliberately not the absolute path. Pages are portable and shareable,
        # and where the repository happens to sit on this server is a fact about
        # the deployment, not about the code. It stays in the register.
        f"Code memory for the `{repo_name}` repository.",
        "",
        f"- {report.node_count} records, {report.edge_count} relationships",
        f"- {len(report.communities)} clusters",
    ]
    for method, count in sorted(report.by_extraction_method.items()):
        lines.append(f"- {count} {method.lower()}")
    lines += ["", "## Clusters", ""]

    if communities:
        for community in communities:
            slug = f"{repo_folder(repo_name)}/{slugify(community.label)}"
            lines.append(f"- [[{slug}|{community.label}]] — {community.size} records")
    else:
        lines.append("- Nothing clustered yet. Index a larger repository, or check the ingest log.")

    if fixes:
        lines += [
            "",
            "## What broke before",
            "",
            "Trouble this repository has already had, and what settled it.",
            "",
        ]
        for fix in fixes:
            symptom = str(fix.properties.get("symptom", "")) or fix.label
            diagnosis = str(fix.properties.get("diagnosis", ""))
            verified = str(fix.properties.get("verified_by", ""))
            lines.append(f"- **{symptom}**")
            if diagnosis:
                lines.append(f"  - {diagnosis}")
            changed = fix.properties.get("changed_paths") or []
            if changed:
                lines.append(f"  - Changed: {', '.join(str(path) for path in changed)}")
            # Say plainly whether this was checked. An unverified fix is still
            # worth keeping; presenting it as proven would not be.
            lines.append(
                f"  - Verified by `{verified}`" if verified else "  - Not verified by a check"
            )

    if report.surprising_links:
        lines += [
            "",
            "## Connections you would not have predicted",
            "",
            "Scored by how little the two records share with the rest of the graph.",
            "",
        ]
        for link in report.surprising_links[:MAX_SURPRISING]:
            lines.append(
                f"- **{link.src_label}** → *{link.rel_type}* → **{link.dst_label}** "
                f"({link.score:.2f}) — {link.reason}"
            )

    if report.narrative:
        lines += ["", "## In plain language", ""]
        lines.extend(str(line) for line in report.narrative)

    lines += [
        "",
        "---",
        "",
        "Generated from the code graph, deterministically — no model was asked. "
        "Every record above is backed by a citation into a file and a line.",
        "",
    ]
    return "\n".join(lines)


def _is_bookkeeping(
    community: Community, objects_by_id: dict[str, KnowledgeObject]
) -> bool:
    """True when a cluster is only repository metadata, not code."""
    kinds = {
        objects_by_id[member].kind
        for member in community.member_ids
        if member in objects_by_id
    }
    return bool(kinds) and kinds <= _BOOKKEEPING_KINDS


async def _fixes_for(repo: "CodeRepo") -> list[KnowledgeObject]:
    """Remembered repairs that reached this repository's code.

    Fixes live in the vault's scope and reach code through a `bridge` edge, so
    they are found by walking that edge rather than by scope.
    """
    async with sqlite.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        symbol_ids = {
            object_.id
            for object_ in await knowledge.list_objects(scope=repo.scope, limit=10_000)
        }
        edges = await knowledge.list_relationships(scope="bridge")
        fix_ids = sorted(
            {
                edge.src_id
                for edge in edges
                if edge.rel_type == "fixes" and edge.dst_id in symbol_ids
            }
        )
        found = [await knowledge.get_object(fix_id) for fix_id in fix_ids]
    return [fix for fix in found if fix is not None][:MAX_SURPRISING]


async def write_repo_pages(repo: "CodeRepo", *, settings: Settings) -> int:
    """Write the index and one page per cluster. Returns how many pages landed.

    Page writing never fails the indexing run: the canonical graph is the record
    that matters, and a vault that could not be written is a problem to report,
    not a reason to throw away work that already succeeded.
    """
    async with sqlite.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        # This repository, the vault that owns it, and the owner root: enough
        # for a decision link to show on the page, and nothing from elsewhere.
        report = await audit_knowledge_graph(
            knowledge,
            scope=repo.scope,
            allowed_scopes={repo.scope, f"wiki:{repo.wiki_id}", SELF_ID},
        )
        objects = await knowledge.list_objects(scope=repo.scope, limit=100_000)
        edges = await knowledge.list_relationships(scope=repo.scope)

    objects_by_id = {object_.id: object_ for object_ in objects}
    fixes = await _fixes_for(repo)
    communities = [
        community
        for community in report.communities
        if community.size >= MIN_COMMUNITY_SIZE
        and not _is_bookkeeping(community, objects_by_id)
    ]

    folder = repo_folder(repo.name)
    pages: list[tuple[str, str, str, list[str]]] = [
        (
            f"{folder}/index",
            repo.name,
            render_index_page(
                report,
                repo_name=repo.name,
                communities=communities,
                fixes=fixes,
            ),
            ["code", "repo"],
        )
    ]
    for community in communities:
        pages.append(
            (
                f"{folder}/{slugify(community.label)}",
                community.label,
                render_community_page(
                    community,
                    repo_name=repo.name,
                    objects_by_id=objects_by_id,
                    edges=edges,
                    repo_root=repo.path,
                ),
                ["code", "cluster"],
            )
        )

    written = 0
    for slug, title, markdown, tags in pages:
        try:
            path = settings.wiki_dir / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                ensure_frontmatter(markdown, title=title, tags=tags), encoding="utf-8"
            )
            # Same indexing path as a page written by hand. No distillation:
            # this page came out of the graph, and feeding it back in would
            # propose the repository's own structure as things to remember.
            await reindex_page(
                slug,
                wiki_id=repo.wiki_id,
                settings=settings,
                force=True,
                authored_by="agent",
                reason="code-graph",
                distill=False,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - the canonical graph is already saved
            logger.warning("Could not write code page %s: %s", slug, exc)

    return written
