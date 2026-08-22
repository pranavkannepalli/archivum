"""Repairs for graphs written before a link existed.

Ingest now writes a `mentions` edge from a page to every entity extracted from
it, but records written before that have no such edge, and re-reading the vault
does not recreate them: reconciliation reads markdown from disk, and entity
extraction is an ingest-time step over the original document, not something a
page re-read repeats.

The join survives anyway. Everything ingest derives from one document cites that
document, so a page and an entity carrying the same source id came out of the
same file. That is enough to rebuild the edge without re-running any model.
"""

from __future__ import annotations

import logging

from archivum.knowledge.models import KnowledgeRelationship
from archivum.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

# Large enough for a personal vault, bounded so a repair cannot walk a graph
# that has grown past what one pass should try to hold.
_SCAN_LIMIT = 20_000


def _source_ids(obj) -> set[str]:
    return {citation.source_id for citation in obj.citations if citation.source_id}


async def link_entities_to_their_pages(
    repo: KnowledgeRepository, *, wiki_id: str
) -> int:
    """Join entities to pages they were extracted alongside. Returns edges added.

    An entity that shares no source with any page is left disconnected: an edge
    asserting a provenance the records do not support would be worse than the
    orphan it replaced.
    """
    scope = f"wiki:{wiki_id}"
    objects = await repo.list_objects(scope=scope, limit=_SCAN_LIMIT)

    pages_by_source: dict[str, list[str]] = {}
    for obj in objects:
        if obj.kind != "page":
            continue
        for source_id in _source_ids(obj):
            pages_by_source.setdefault(source_id, []).append(obj.id)

    existing = {
        (rel.src_id, rel.dst_id)
        for rel in await repo.list_relationships(scope=scope)
        if rel.rel_type == "mentions"
    }

    added = 0
    for obj in objects:
        if obj.kind != "entity":
            continue
        for source_id in _source_ids(obj):
            for page_id in pages_by_source.get(source_id, ()):
                if (page_id, obj.id) in existing:
                    continue
                await repo.upsert_relationship(
                    KnowledgeRelationship(
                        id=f"rel:{page_id}:mentions:{obj.id}",
                        src_id=page_id,
                        dst_id=obj.id,
                        rel_type="mentions",
                        scope=scope,
                        confidence=0.6,
                        # The page and the entity are both extracted, but that
                        # they belong together is read off shared provenance
                        # rather than off the document.
                        extraction_method="INFERRED",
                        citations=obj.citations[:1],
                        properties={"backfilled_from": source_id},
                    )
                )
                existing.add((page_id, obj.id))
                added += 1

    if added:
        logger.info("Rejoined %d entities to the pages they came from", added)
    return added
