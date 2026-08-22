"""Renaming or deleting a page used to leave memory pointing at a ghost.

The rename path fixed the row, knowledge, shares, embeddings and inbound
wikilinks — but not `memory_assets.page_slug` or the suggestion target, both of
which embed the slug.
"""

import pytest

from archivum.config import Settings
from archivum.db import sqlite as sqlite_mod
from archivum.indexing import forget_page, repoint_page
from archivum.knowledge.suggestions import SuggestionRepository, init_suggestion_schema
from archivum.memory.registry import MemoryAssetRegistry


@pytest.fixture
async def settings(tmp_path):
    s = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
    )
    s.wiki_dir.mkdir(parents=True, exist_ok=True)
    await sqlite_mod.init_db(s)
    return s


async def seed(slug: str) -> None:
    async with sqlite_mod.get_db() as conn:
        await init_suggestion_schema(conn)
        await MemoryAssetRegistry(conn).register_asset(
            id=f"memory:atom:{slug}",
            wiki_id="default",
            asset_type="wiki",
            layer="L1",
            name="An atom",
            scope="person:self",
            page_slug=slug,
        )
        await SuggestionRepository(conn).create_suggestion(
            target_id=f"page:default:{slug}",
            suggestion_type="edit",
            proposed_markdown="Change something.",
            proposed_objects=[],
            citations=[],
        )


async def assets_for(slug: str | None):
    async with sqlite_mod.get_db() as conn:
        return await MemoryAssetRegistry(conn).list_assets(
            wiki_id="default", page_slug=slug
        )


async def suggestions_for(slug: str):
    async with sqlite_mod.get_db() as conn:
        return await SuggestionRepository(conn).list_suggestions(
            target_id=f"page:default:{slug}", status=None
        )


async def test_rename_carries_memory_and_review_items(settings):
    await seed("topics/old")

    moved = await repoint_page(
        old_slug="topics/old", new_slug="topics/new", wiki_id="default"
    )

    assert moved == {"assets": 1, "suggestions": 1}
    assert [a.id for a in await assets_for("topics/new")] == ["memory:atom:topics/old"]
    assert await assets_for("topics/old") == []

    followed = await suggestions_for("topics/new")
    assert len(followed) == 1 and followed[0].status == "pending"
    assert await suggestions_for("topics/old") == []


async def test_delete_detaches_memory_but_keeps_it(settings):
    """What was learned outlives the page; it just stops claiming to live there."""
    await seed("topics/doomed")

    await forget_page("topics/doomed", wiki_id="default", settings=settings)

    assert await assets_for("topics/doomed") == []
    surviving = await assets_for(None)
    assert any(a.id == "memory:atom:topics/doomed" for a in surviving)


async def test_delete_retires_review_items_that_can_never_be_accepted(settings):
    await seed("topics/doomed")

    await forget_page("topics/doomed", wiki_id="default", settings=settings)

    remaining = await suggestions_for("topics/doomed")
    assert len(remaining) == 1
    assert remaining[0].status == "expired", (
        "a suggestion against a deleted page would sit in the review queue forever"
    )


async def test_rename_carries_the_citations_that_name_the_page(settings):
    """A wiki-scoped suggestion says which page it is about in its citations.

    Distillation files everything against `wiki:<id>`, so a rename that only
    rewrote `target_id` left those citing a slug that no longer resolves. The
    review item stayed pending and stopped being attributable to any page —
    invisible in "Needs you" rather than merely misfiled.
    """
    async with sqlite_mod.get_db() as conn:
        await init_suggestion_schema(conn)
        await SuggestionRepository(conn).create_suggestion(
            target_id="wiki:default",
            suggestion_type="memory_atom",
            proposed_markdown="- A constraint.",
            proposed_objects=[],
            citations=[
                {"source_id": "page:topics/old", "quote": "A constraint."},
                {"source_id": "page:topics/old-but-different", "quote": "Untouched."},
            ],
        )

    await repoint_page(old_slug="topics/old", new_slug="filed/new", wiki_id="default")

    async with sqlite_mod.get_db() as conn:
        pending = await SuggestionRepository(conn).list_suggestions(
            target_id="wiki:default", status=None
        )
    sources = [citation["source_id"] for citation in pending[0].citations]
    assert sources == ["page:filed/new", "page:topics/old-but-different"], (
        "the renamed page follows; a slug that merely shares a prefix does not"
    )
