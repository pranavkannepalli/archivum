"""Rebuild Kuzu and Qdrant projections from canonical knowledge records."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from archivum.db.graph import (
    add_knowledge_relationship,
    add_mention,
    add_reference,
    clear_knowledge_projection,
    clear_legacy_projection,
    upsert_entity,
    upsert_knowledge_node,
    upsert_page,
)
from archivum.db.qdrant_client import clear_projection_index, index_page
from archivum.knowledge.graph_audit import LINK_SCOPES
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository


_QDRANT_KINDS = frozenset({"page", "source", "claim"})
_OBJECT_REBUILD_LIMIT = 100_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionReport:
    objects: int = 0
    relationships: int = 0
    kuzu_nodes: int = 0
    kuzu_edges: int = 0
    qdrant_indexed: int = 0


def _citations_payload(citations: list[Citation]) -> list[dict[str, Any]]:
    return [citation.model_dump() for citation in citations]


def _page_slug(object_: KnowledgeObject) -> str:
    return str(object_.properties.get("slug") or object_.id)


def _qdrant_content(object_: KnowledgeObject) -> str:
    for key in ("markdown", "content", "text"):
        value = object_.properties.get(key)
        if isinstance(value, str) and value.strip():
            return value
    evidence = "\n".join(citation.quote or "" for citation in object_.citations).strip()
    return evidence or object_.label


async def _project_kuzu(operation, *args) -> bool:
    """Keep canonical data usable when the rebuildable graph is unavailable."""
    try:
        await operation(*args)
        return True
    except Exception as exc:
        logger.warning("Kuzu projection operation failed: %s", exc)
        return False


async def project_page(
    repo: KnowledgeRepository, *, page_id: str, wiki_id: str
) -> ProjectionReport:
    """Project one page's canonical record into the graph, incrementally.

    The full rebuild is the repair path; this is what keeps the projection
    current between rebuilds. It writes the same two views the rebuild does —
    the knowledge node and the page-centric legacy node — so an incrementally
    maintained graph and a rebuilt one agree. They disagreed before precisely
    because writes only ever updated the legacy half.
    """
    object_ = await repo.get_object(page_id)
    if object_ is None:
        return ProjectionReport()

    # The report is frozen, so tally first and construct once.
    kuzu_nodes = 0
    kuzu_edges = 0
    projected = await _project_kuzu(
        upsert_knowledge_node,
        object_.id,
        object_.label,
        object_.kind,
        object_.scope,
        object_.confidence,
        object_.extraction_method,
        _citations_payload(object_.citations),
        wiki_id,
    )
    kuzu_nodes += int(projected)
    await _project_kuzu(upsert_page, _page_slug(object_), object_.label, wiki_id)

    relationships = [
        relationship
        for relationship in await repo.list_relationships(scope=object_.scope)
        if relationship.src_id == page_id
    ]
    for relationship in relationships:
        edge_projected = await _project_kuzu(
            add_knowledge_relationship,
            relationship.src_id,
            relationship.dst_id,
            relationship.id,
            relationship.rel_type,
            relationship.scope,
            relationship.confidence,
            relationship.extraction_method,
            _citations_payload(relationship.citations),
        )
        kuzu_edges += int(edge_projected)

    return ProjectionReport(
        objects=1,
        relationships=len(relationships),
        kuzu_nodes=kuzu_nodes,
        kuzu_edges=kuzu_edges,
    )


async def rebuild_knowledge_projections(
    repo: KnowledgeRepository, wiki_id: str
) -> ProjectionReport:
    """Project canonical knowledge into rebuildable graph and semantic indexes."""
    target_scope = f"wiki:{wiki_id}"
    all_objects = await repo.list_objects(limit=_OBJECT_REBUILD_LIMIT)
    if len(all_objects) == _OBJECT_REBUILD_LIMIT:
        raise RuntimeError(
            "Knowledge projection rebuild reached its object limit; "
            "paginate canonical objects before retrying."
        )
    # Repositories registered to this wiki are part of its graph. Filtering on
    # `wiki:` alone meant a repository could be fully indexed into canonical
    # knowledge and still never appear as one node in the graph view.
    from archivum.code_repos import owned_repo_scopes  # local: avoids an import cycle

    try:
        repo_scopes = await owned_repo_scopes(wiki_id=wiki_id)
    except Exception as exc:  # noqa: BLE001 - a missing register is not a failed rebuild
        logger.warning("Could not read the repository register: %s", exc)
        repo_scopes = set()

    included_scopes = {target_scope, *repo_scopes}
    objects = [
        object_
        for object_ in all_objects
        if object_.scope in included_scopes or object_.id == SELF_ID
    ]
    objects_by_id = {object_.id: object_ for object_ in objects}
    relationships = [
        relationship
        for scope in sorted(included_scopes) + list(LINK_SCOPES)
        for relationship in await repo.list_relationships(scope=scope)
        if relationship.src_id in objects_by_id and relationship.dst_id in objects_by_id
    ]

    await clear_projection_index(wiki_id)
    await _project_kuzu(clear_knowledge_projection, wiki_id)
    await _project_kuzu(clear_legacy_projection, wiki_id)
    kuzu_nodes = 0
    for object_ in objects:
        node_projected = await _project_kuzu(
            upsert_knowledge_node,
            object_.id,
            object_.label,
            object_.kind,
            object_.scope,
            object_.confidence,
            object_.extraction_method,
            _citations_payload(object_.citations),
            wiki_id,
        )
        kuzu_nodes += int(node_projected)
        if object_.kind == "page":
            await _project_kuzu(upsert_page, _page_slug(object_), object_.label, wiki_id)
        else:
            await _project_kuzu(upsert_entity, object_.id, object_.kind, wiki_id)

    qdrant_indexed = 0
    for object_ in objects:
        if object_.kind not in _QDRANT_KINDS:
            continue
        indexed = await index_page(
            object_.id,
            object_.label,
            _qdrant_content(object_),
            wiki_id,
        )
        qdrant_indexed += indexed if isinstance(indexed, int) else 1

    kuzu_edges = 0
    for relationship in relationships:
        relationship_projected = await _project_kuzu(
            add_knowledge_relationship,
            relationship.src_id,
            relationship.dst_id,
            relationship.id,
            relationship.rel_type,
            relationship.scope,
            relationship.confidence,
            relationship.extraction_method,
            _citations_payload(relationship.citations),
        )
        kuzu_edges += int(relationship_projected)

        source = objects_by_id.get(relationship.src_id)
        destination = objects_by_id.get(relationship.dst_id)
        if source is None or destination is None:
            continue
        if source.kind == "page" and destination.kind == "page" and relationship.rel_type == "references":
            await _project_kuzu(add_reference, _page_slug(source), _page_slug(destination), wiki_id)
        elif source.kind == "page" and destination.kind != "page" and relationship.rel_type == "mentions":
            await _project_kuzu(add_mention, _page_slug(source), destination.id, wiki_id)

    return ProjectionReport(
        objects=len(objects),
        relationships=len(relationships),
        kuzu_nodes=kuzu_nodes,
        kuzu_edges=kuzu_edges,
        qdrant_indexed=qdrant_indexed,
    )
