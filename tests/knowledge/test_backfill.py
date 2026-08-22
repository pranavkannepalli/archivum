"""Reconnecting a graph that was written before pages linked to their entities.

The ingest fix only helps records written after it. A vault that already has a
floating entity cloud needs the edges rebuilt from what is on disk, and the join
is recoverable: every record ingest derived from one source cites that source,
so a page and an entity that share a source id came out of the same document.
"""

import aiosqlite
import pytest

from archivum.knowledge.backfill import link_entities_to_their_pages
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema


def _cite(source_id: str) -> Citation:
    return Citation(
        source_id=source_id, chunk_id=f"{source_id}:0", span_start=None, span_end=None, quote="q"
    )


def _obj(object_id: str, kind: str, source_id: str) -> KnowledgeObject:
    return KnowledgeObject(
        id=object_id,
        kind=kind,
        label=object_id,
        scope="wiki:default",
        confidence=0.8,
        extraction_method="EXTRACTED",
        citations=[_cite(source_id)],
        properties={},
    )


@pytest.fixture
async def repo():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        yield KnowledgeRepository(conn)


async def test_entities_are_rejoined_to_the_page_from_the_same_source(repo):
    await repo.upsert_object(_obj("page:default:notes", "page", "src-1"))
    await repo.upsert_object(_obj("entity:default:kuzu", "entity", "src-1"))
    await repo.upsert_object(_obj("entity:default:other", "entity", "src-2"))

    linked = await link_entities_to_their_pages(repo, wiki_id="default")

    relationships = await repo.list_relationships(scope="wiki:default")
    mentioned = {
        rel.dst_id for rel in relationships
        if rel.src_id == "page:default:notes" and rel.rel_type == "mentions"
    }
    assert mentioned == {"entity:default:kuzu"}, "only what came from the same document"
    assert linked == 1


async def test_running_it_again_adds_nothing(repo):
    await repo.upsert_object(_obj("page:default:notes", "page", "src-1"))
    await repo.upsert_object(_obj("entity:default:kuzu", "entity", "src-1"))

    await link_entities_to_their_pages(repo, wiki_id="default")
    before = len(await repo.list_relationships(scope="wiki:default"))
    await link_entities_to_their_pages(repo, wiki_id="default")

    assert len(await repo.list_relationships(scope="wiki:default")) == before


async def test_an_entity_with_no_shared_source_is_left_alone(repo):
    """Better an orphan than an edge asserting a provenance that is not there."""
    await repo.upsert_object(_obj("page:default:notes", "page", "src-1"))
    await repo.upsert_object(_obj("entity:default:loose", "entity", "src-9"))

    assert await link_entities_to_their_pages(repo, wiki_id="default") == 0
