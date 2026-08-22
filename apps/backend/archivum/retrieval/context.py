"""Build bounded, evidence-backed context packages from knowledge records."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from archivum.archgraph.retrieval import ScopedSubgraph, retrieve_code
from archivum.knowledge.models import (
    Citation,
    ContextEdge,
    ContextNode,
    ContextPackage,
    KnowledgeObject,
    KnowledgeRelationship,
)
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository


_OBJECT_SCAN_LIMIT = 10_000


@dataclass(frozen=True)
class ScopeBudget:
    """Token/item limits configured for a memory scope."""

    tokens: int | None
    items: int | None


@dataclass(frozen=True)
class ContextRequest:
    query: str
    scope: str | None
    # Budgets are per-wiki configuration; without a wiki there is no budget.
    wiki_id: str | None = None
    source_type: str | None = None
    depth: int = 2
    max_nodes: int = 10
    relations: list[str] | None = None
    seed_ids: list[str] | None = None
    code_connection: aiosqlite.Connection | None = None
    # Return the cited lines, not just the citation. Off by default: a caller
    # mapping a repository wants names, and paying for every body would blow the
    # budget on records it is only skimming.
    include_source: bool = False
    max_excerpt_lines: int = 40


async def build_context_package(
    repo: KnowledgeRepository, request: ContextRequest
) -> ContextPackage:
    """Return a bounded, scoped subgraph rooted in requested or matched knowledge.

    Context packs are built from accepted memory only: provisional records that
    are still pending human review are excluded, and per-scope token/item
    budgets from the memory scope registry bound the result.
    """
    budget = await _load_scope_budget(repo._conn, request.scope, request.wiki_id)
    all_objects = await repo.list_objects(
        scope=request.scope, limit=_OBJECT_SCAN_LIMIT
    )
    if request.scope is not None:
        root = await repo.get_object(SELF_ID)
        if root is not None and all(obj.id != SELF_ID for obj in all_objects):
            all_objects.append(root)
    pending_exclusions = {
        obj.id: "Excluded pending human review."
        for obj in all_objects
        if _is_pending_review(obj)
    }
    all_objects = [obj for obj in all_objects if obj.id not in pending_exclusions]
    relationships = await repo.list_relationships(scope=request.scope)
    if _is_code_request(request):
        return await _build_code_context_package(
            repo,
            all_objects,
            relationships,
            request,
            budget=budget,
            extra_exclusions=pending_exclusions,
        )

    objects = _filter_source_type(all_objects, request.source_type)
    objects_by_id = {obj.id: obj for obj in objects}
    seeds = _select_seeds(objects, request)
    if not seeds and request.source_type is None:
        root = next((obj for obj in all_objects if obj.id == SELF_ID), None)
        if root is not None:
            objects_by_id[root.id] = root
            seeds = [SELF_ID]

    visited, edges = _expand(objects_by_id, relationships, seeds, request)
    return _package_from_records(
        request.query,
        seeds,
        objects_by_id,
        visited,
        edges,
        budget=budget,
        extra_exclusions=pending_exclusions,
    )


async def _build_code_context_package(
    repo: KnowledgeRepository,
    objects: list[KnowledgeObject],
    relationships: list[KnowledgeRelationship],
    request: ContextRequest,
    *,
    budget: "ScopeBudget | None" = None,
    extra_exclusions: dict[str, str] | None = None,
) -> ContextPackage:
    objects_by_id = {obj.id: obj for obj in objects}
    explicit_seeds = [
        seed_id for seed_id in request.seed_ids or [] if seed_id in objects_by_id
    ]
    subgraph = await _retrieve_code_subgraph(repo, objects, relationships, request)
    if subgraph is None:
        return _build_canonical_code_fallback(
            objects_by_id,
            relationships,
            request,
            budget=budget,
            extra_exclusions=extra_exclusions,
        )

    seeds = _bounded_unique([*explicit_seeds, *subgraph.seeds], request.max_nodes)
    node_ids = _bounded_unique(
        [*explicit_seeds, *(node["id"] for node in subgraph.nodes)],
        request.max_nodes,
    )

    if not node_ids and request.source_type is None:
        root = objects_by_id.get(SELF_ID)
        if root is not None:
            node_ids = [SELF_ID]
            seeds = [SELF_ID]

    edges_by_key = {
        (relationship.src_id, relationship.dst_id, relationship.rel_type): relationship
        for relationship in relationships
    }
    edges = [
        edges_by_key[(edge["source"], edge["target"], edge["relation"])]
        for edge in subgraph.edges
        if (edge["source"], edge["target"], edge["relation"]) in edges_by_key
        and edge["source"] in node_ids
        and edge["target"] in node_ids
    ]
    return _package_from_records(
        request.query,
        seeds,
        objects_by_id,
        node_ids,
        edges,
        budget=budget,
        extra_exclusions=extra_exclusions,
        include_source=request.include_source,
        max_excerpt_lines=request.max_excerpt_lines,
    )


async def _retrieve_code_subgraph(
    repo: KnowledgeRepository,
    objects: list[KnowledgeObject],
    relationships: list[KnowledgeRelationship],
    request: ContextRequest,
) -> ScopedSubgraph | None:
    connection = request.code_connection or repo._conn
    if not await _has_lexical_index(connection):
        return None

    adjacency: dict[str, list[dict]] = {}
    node_meta = {
        obj.id: {
            "label": obj.label,
            "kind": obj.kind,
            "scope": obj.scope,
            "confidence": obj.confidence,
            "extraction_method": obj.extraction_method,
            "citation": obj.citations[0].chunk_id,
        }
        for obj in objects
    }
    for relationship in relationships:
        if relationship.src_id in node_meta and relationship.dst_id in node_meta:
            adjacency.setdefault(relationship.src_id, []).append(
                {
                    "target": relationship.dst_id,
                    "relation": relationship.rel_type,
                    "extraction_method": relationship.extraction_method,
                    "confidence": relationship.confidence,
                }
            )
    return await retrieve_code(
        connection,
        request.query,
        adjacency=adjacency,
        node_meta=node_meta,
        depth=request.depth,
        max_nodes=request.max_nodes,
        scope=request.scope,
        relations=frozenset(request.relations) if request.relations is not None else None,
    )


def _build_canonical_code_fallback(
    objects_by_id: dict[str, KnowledgeObject],
    relationships: list[KnowledgeRelationship],
    request: ContextRequest,
    *,
    budget: "ScopeBudget | None" = None,
    extra_exclusions: dict[str, str] | None = None,
) -> ContextPackage:
    seeds = _bounded_unique(
        _select_seeds(list(objects_by_id.values()), request), request.max_nodes
    )
    if not seeds and request.source_type is None and SELF_ID in objects_by_id:
        seeds = [SELF_ID]
    node_ids, edges = _expand(objects_by_id, relationships, seeds, request)
    return _package_from_records(
        request.query,
        seeds,
        objects_by_id,
        node_ids,
        edges,
        budget=budget,
        extra_exclusions=extra_exclusions,
        include_source=request.include_source,
        max_excerpt_lines=request.max_excerpt_lines,
    )


async def _load_scope_budget(
    connection: aiosqlite.Connection, scope: str | None, wiki_id: str | None
) -> ScopeBudget | None:
    """Read the wiki's configured budget for a scope; tolerate stores without
    the table. Budgets never cross wiki boundaries, so an anonymous request
    gets none."""
    if scope is None or wiki_id is None:
        return None
    async with connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_scopes'"
    ) as cursor:
        if await cursor.fetchone() is None:
            return None
    async with connection.execute(
        "SELECT budget_tokens, budget_items FROM memory_scopes "
        "WHERE id=? AND wiki_id=? LIMIT 1",
        (scope, wiki_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return ScopeBudget(tokens=row["budget_tokens"], items=row["budget_items"])


def _is_pending_review(obj: KnowledgeObject) -> bool:
    return obj.properties.get("review_state") == "pending"


def _estimate_tokens(obj: KnowledgeObject) -> int:
    text = " ".join([obj.label, *(str(value) for value in obj.properties.values())])
    return max(1, len(text) // 4)


def _apply_scope_budget(
    node_ids: list[str],
    objects_by_id: dict[str, KnowledgeObject],
    budget: ScopeBudget | None,
) -> tuple[list[str], dict[str, str]]:
    """Trim traversal output to the scope's budgets, explaining each drop.

    The first node always fits so a tight budget still yields a usable seed.
    """
    if budget is None or (budget.tokens is None and budget.items is None):
        return node_ids, {}
    kept: list[str] = []
    excluded: dict[str, str] = {}
    spent = 0
    for node_id in node_ids:
        obj = objects_by_id[node_id]
        if budget.items is not None and len(kept) >= budget.items:
            excluded[node_id] = "Excluded by scope item budget."
            continue
        cost = _estimate_tokens(obj)
        if budget.tokens is not None and kept and spent + cost > budget.tokens:
            excluded[node_id] = "Excluded by scope token budget."
            continue
        kept.append(node_id)
        spent += cost
    return kept, excluded


async def _has_lexical_index(connection: aiosqlite.Connection) -> bool:
    async with connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_node_text'"
    ) as cursor:
        return await cursor.fetchone() is not None


def _is_code_request(request: ContextRequest) -> bool:
    return request.source_type == "code" or (request.scope or "").startswith("repo:")


def _filter_source_type(
    objects: list[KnowledgeObject], source_type: str | None
) -> list[KnowledgeObject]:
    if source_type is None:
        return objects
    return [
        obj for obj in objects if obj.properties.get("source_type") == source_type
    ]


def _select_seeds(
    objects: list[KnowledgeObject], request: ContextRequest
) -> list[str]:
    available = {obj.id for obj in objects}
    seeds = [seed_id for seed_id in request.seed_ids or [] if seed_id in available]
    query = request.query.strip().casefold()
    if query:
        seeds.extend(
            obj.id
            for obj in objects
            if query in obj.label.casefold() and obj.id not in seeds
        )
    return seeds


def _expand(
    objects_by_id: dict[str, KnowledgeObject],
    relationships: list[KnowledgeRelationship],
    seeds: list[str],
    request: ContextRequest,
) -> tuple[list[str], list[KnowledgeRelationship]]:
    max_nodes = max(request.max_nodes, 0)
    if max_nodes == 0:
        return [], []

    allowed_relations = set(request.relations) if request.relations is not None else None
    visited: list[str] = []
    visited_ids: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for seed_id in seeds:
        if seed_id in objects_by_id and seed_id not in visited_ids and len(visited) < max_nodes:
            visited.append(seed_id)
            visited_ids.add(seed_id)
            queue.append((seed_id, 0))

    traversed: dict[str, KnowledgeRelationship] = {}
    while queue:
        node_id, distance = queue.popleft()
        if distance >= max(request.depth, 0):
            continue
        for relationship in relationships:
            if node_id not in (relationship.src_id, relationship.dst_id):
                continue
            if allowed_relations is not None and relationship.rel_type not in allowed_relations:
                continue
            other_id = relationship.dst_id if relationship.src_id == node_id else relationship.src_id
            if other_id not in objects_by_id:
                continue
            if other_id not in visited_ids and len(visited) < max_nodes:
                visited.append(other_id)
                visited_ids.add(other_id)
                queue.append((other_id, distance + 1))
            if other_id in visited_ids:
                traversed[relationship.id] = relationship

    edges = [
        relationship
        for relationship in traversed.values()
        if relationship.src_id in visited_ids and relationship.dst_id in visited_ids
    ]
    return visited, edges


def _package_from_records(
    query: str,
    seeds: list[str],
    objects_by_id: dict[str, KnowledgeObject],
    node_ids: list[str],
    edges: list[KnowledgeRelationship],
    *,
    budget: ScopeBudget | None = None,
    extra_exclusions: dict[str, str] | None = None,
    include_source: bool = False,
    max_excerpt_lines: int = 40,
) -> ContextPackage:
    node_ids, budget_exclusions = _apply_scope_budget(node_ids, objects_by_id, budget)
    kept_ids = set(node_ids)
    edges = [
        relationship
        for relationship in edges
        if relationship.src_id in kept_ids and relationship.dst_id in kept_ids
    ]
    nodes = [
        _context_node(
            objects_by_id[node_id],
            include_source=include_source,
            max_lines=max_excerpt_lines,
        )
        for node_id in node_ids
    ]
    context_edges = [_context_edge(relationship) for relationship in edges]
    citations = _unique_citations(
        citation for node in nodes for citation in node.citations
    )
    citations = _unique_citations(
        [*citations, *(citation for edge in context_edges for citation in edge.citations)]
    )
    insufficient_evidence = not any(node.citations for node in nodes)
    selected_ids = {node.id for node in nodes}
    inclusion_explanations = {
        node.id: (
            f"Included as a seed or graph neighbor in scope {node.scope}; "
            f"{len(node.citations)} citation{'s' if len(node.citations) != 1 else ''} available."
        )
        for node in nodes
    }
    exclusion_explanations = {
        object_id: "Excluded by max_nodes budget."
        for object_id in objects_by_id
        if object_id not in selected_ids
    }
    exclusion_explanations.update(budget_exclusions)
    exclusion_explanations.update(extra_exclusions or {})
    staleness_warnings = {
        object_id: "Marked stale by source metadata."
        for object_id, obj in objects_by_id.items()
        if object_id in selected_ids and obj.properties.get("stale")
    }
    return ContextPackage(
        query=query,
        seeds=seeds,
        nodes=nodes,
        edges=context_edges,
        citations=citations,
        insufficient_evidence=insufficient_evidence,
        reason=(
            "No cited knowledge objects matched the requested context."
            if insufficient_evidence
            else None
        ),
        inclusion_explanations=inclusion_explanations,
        exclusion_explanations=exclusion_explanations,
        staleness_warnings=staleness_warnings,
    )


def _bounded_unique(ids: Iterable[str], max_nodes: int) -> list[str]:
    bounded: list[str] = []
    for node_id in ids:
        if node_id not in bounded and len(bounded) < max(max_nodes, 0):
            bounded.append(node_id)
    return bounded


def _context_node(
    obj: KnowledgeObject, *, include_source: bool = False, max_lines: int = 40
) -> ContextNode:
    return ContextNode(
        id=obj.id,
        label=obj.label,
        node_type=obj.kind,
        scope=obj.scope,
        extraction_method=obj.extraction_method,
        confidence=obj.confidence,
        citations=obj.citations,
        signature=str(obj.properties.get("signature") or ""),
        summary=str(obj.properties.get("summary") or ""),
        excerpt=_excerpt(obj, max_lines) if include_source else "",
    )


def _excerpt(obj: KnowledgeObject, max_lines: int) -> str:
    """The lines a record cites, read from disk.

    Best-effort by design: the file may have moved or been edited since it was
    indexed, and a missing excerpt is a smaller problem than a failed retrieval.
    The citation remains the authoritative answer either way.
    """
    if not obj.citations:
        return ""
    citation = obj.citations[0]
    path = Path(citation.chunk_id or "")
    span = (citation.quote or "").strip()
    if not path.is_file() or not span.startswith("L"):
        return ""
    bounds = [part.lstrip("Ll") for part in span.split("-") if part.strip()]
    if not all(part.isdigit() for part in bounds):
        return ""
    start = int(bounds[0])
    end = int(bounds[-1]) if len(bounds) > 1 else start
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    selected = lines[max(start - 1, 0) : min(end, start - 1 + max_lines)]
    return "\n".join(selected)


def _context_edge(relationship: KnowledgeRelationship) -> ContextEdge:
    return ContextEdge(
        from_id=relationship.src_id,
        to_id=relationship.dst_id,
        relation=relationship.rel_type,
        scope=relationship.scope,
        extraction_method=relationship.extraction_method,
        confidence=relationship.confidence,
        citations=relationship.citations,
    )


def _unique_citations(citations: Iterable[Citation]) -> list[Citation]:
    unique: list[Citation] = []
    seen: set[tuple[str, str, int | None, int | None, str | None]] = set()
    for citation in citations:
        key = (
            citation.source_id,
            citation.chunk_id,
            citation.span_start,
            citation.span_end,
            citation.quote,
        )
        if key not in seen:
            seen.add(key)
            unique.append(citation)
    return unique
