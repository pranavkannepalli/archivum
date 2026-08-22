"""Pages link to the entities they name, recomputed from the page every index.

Ingest writes these edges when it extracts, but that only covers pages that came
from an ingested document, and reindexing rewrites a page's citations — so a
join recovered from shared provenance does not survive a reconcile. Reading the
page text does: it is the same answer every time, from the file on disk, with no
model involved.
"""

import aiosqlite
import pytest

from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema
from archivum.pages_to_knowledge import sync_page_to_knowledge


def _entity(label: str) -> KnowledgeObject:
    entity_id = f"entity:default:{label.lower().replace(' ', '-')}"
    return KnowledgeObject(
        id=entity_id,
        kind="entity",
        label=label,
        scope="wiki:default",
        confidence=0.7,
        extraction_method="EXTRACTED",
        citations=[Citation(source_id="s", chunk_id="s:0", span_start=None, span_end=None, quote=label)],
        properties={},
    )


@pytest.fixture
async def repo():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        yield KnowledgeRepository(conn)


async def _mentions(repo) -> set[str]:
    return {
        rel.dst_id
        for rel in await repo.list_relationships(scope="wiki:default")
        if rel.rel_type == "mentions" and rel.src_id == "page:default:notes"
    }


async def test_a_page_links_to_the_entities_it_names(repo):
    for label in ("Archivum", "Archfleet"):
        await repo.upsert_object(_entity(label))

    await sync_page_to_knowledge(
        repo,
        slug="notes",
        title="Notes",
        markdown="Today I wired archivum into the deploy. Archfleet is next.",
        wiki_id="default",
    )

    assert await _mentions(repo) == {"entity:default:archivum", "entity:default:archfleet"}


async def test_a_name_inside_a_longer_word_is_not_a_mention(repo):
    await repo.upsert_object(_entity("Make"))

    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="I am makeshift about this.",
        wiki_id="default",
    )

    assert await _mentions(repo) == set()


async def test_very_short_names_are_not_matched(repo):
    """"CLI" and "AUR" appear inside prose constantly; matching them is noise."""
    await repo.upsert_object(_entity("Go"))

    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="Go and see. Go now.", wiki_id="default",
    )

    assert await _mentions(repo) == set()


async def test_editing_a_page_drops_mentions_it_no_longer_makes(repo):
    await repo.upsert_object(_entity("Archivum"))
    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="About Archivum.", wiki_id="default",
    )
    assert await _mentions(repo)

    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="About nothing now.", wiki_id="default",
    )

    assert await _mentions(repo) == set(), "a stale edge is a claim the page no longer makes"


async def test_an_acronym_is_matched_when_it_is_written_as_one(repo):
    """"CLI" and "JWT" are distinctive; "cli" inside prose is not.

    Skipping every short label left real entities — AUR, GTK, JWT, SSE, XDG —
    permanently disconnected from the vault. They are safe to match as long as
    the case has to agree, which is what makes them acronyms rather than words.
    """
    await repo.upsert_object(_entity("JWT"))

    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="Auth uses a JWT cookie.", wiki_id="default",
    )

    assert await _mentions(repo) == {"entity:default:jwt"}


async def test_a_lowercase_lookalike_is_not_an_acronym_mention(repo):
    await repo.upsert_object(_entity("JWT"))

    await sync_page_to_knowledge(
        repo, slug="notes", title="Notes", markdown="we jwt around the topic", wiki_id="default",
    )

    assert await _mentions(repo) == set()
