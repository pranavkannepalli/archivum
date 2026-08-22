"""Tasks as lines in pages, not rows in a table.

There used to be a `life_tasks` table. It was deleted because the tool wrote to
a store no screen ever read — two models of the same noun, one of them invisible.

So a task is what it looks like: a checkbox line in a markdown page you own. The
file stays the source of truth, which means you can write one in any editor, and
checking it off in Archivum edits the line rather than updating a shadow record.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings
from archivum.tasks import list_open_tasks, parse_tasks, set_task_done


@pytest.fixture
def vault(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _offline():
    return (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    )


# ── Reading them out of markdown ──────────────────────────────────────────


def test_an_unchecked_box_is_an_open_task():
    tasks = parse_tasks("- [ ] Call the bank\n")
    assert len(tasks) == 1
    assert tasks[0].text == "Call the bank"
    assert tasks[0].done is False
    assert tasks[0].line == 1


def test_a_checked_box_is_a_done_task():
    tasks = parse_tasks("- [x] Call the bank\n")
    assert tasks[0].done is True


def test_capital_x_counts_as_done():
    assert parse_tasks("- [X] Done\n")[0].done is True


def test_indented_and_asterisk_bullets_are_tasks_too():
    body = "  - [ ] Nested\n* [ ] Asterisk\n"
    assert [task.text for task in parse_tasks(body)] == ["Nested", "Asterisk"]


def test_a_line_that_merely_mentions_brackets_is_not_a_task():
    assert parse_tasks("The array is [ ] by default\n") == []


def test_the_line_number_is_where_the_task_actually_is():
    body = "# Notes\n\nSome prose.\n\n- [ ] The task\n"
    assert parse_tasks(body)[0].line == 5


# ── Collecting them across the vault ──────────────────────────────────────


async def test_open_tasks_are_collected_from_every_page(vault):
    await sqlite_mod.init_db(vault)
    await sqlite_mod.upsert_page(
        "daily/today", "Today", "- [ ] Ship the thing\n- [x] Already done\n", [], "user", "default"
    )
    await sqlite_mod.upsert_page(
        "projects/atlas", "Atlas", "- [ ] Write the migration\n", [], "user", "default"
    )

    tasks = await list_open_tasks(wiki_id="default")

    assert {task.text for task in tasks} == {"Ship the thing", "Write the migration"}
    assert all(task.slug for task in tasks)


async def test_a_done_task_is_not_open(vault):
    await sqlite_mod.init_db(vault)
    await sqlite_mod.upsert_page("a", "A", "- [x] Finished\n", [], "user", "default")

    assert await list_open_tasks(wiki_id="default") == []


async def test_code_pages_do_not_contribute_tasks(vault):
    """Generated pages are not somewhere you keep a todo list."""
    await sqlite_mod.init_db(vault)
    await sqlite_mod.upsert_page(
        "code/atlas/index", "atlas", "- [ ] Not a real task\n", ["code"], "agent", "default"
    )

    assert await list_open_tasks(wiki_id="default") == []


# ── Checking one off ──────────────────────────────────────────────────────


async def test_checking_a_task_rewrites_the_line_in_the_file(vault):
    """The file is the source of truth, so the file is what changes."""
    await sqlite_mod.init_db(vault)
    path = vault.wiki_dir / "daily" / "today.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Today\n\n- [ ] Ship the thing\n", encoding="utf-8")
    await sqlite_mod.upsert_page(
        "daily/today", "Today", path.read_text(encoding="utf-8"), [], "user", "default"
    )

    with _offline()[0], _offline()[1], _offline()[2], _offline()[3]:
        await set_task_done(
            slug="daily/today", line=3, done=True, wiki_id="default", settings=vault
        )

    assert "- [x] Ship the thing" in path.read_text(encoding="utf-8")
    assert await list_open_tasks(wiki_id="default") == []


async def test_unchecking_a_task_puts_it_back(vault):
    await sqlite_mod.init_db(vault)
    path = vault.wiki_dir / "a.md"
    path.write_text("- [x] Ship the thing\n", encoding="utf-8")
    await sqlite_mod.upsert_page("a", "A", path.read_text(encoding="utf-8"), [], "user", "default")

    with _offline()[0], _offline()[1], _offline()[2], _offline()[3]:
        await set_task_done(slug="a", line=1, done=False, wiki_id="default", settings=vault)

    assert "- [ ] Ship the thing" in path.read_text(encoding="utf-8")


async def test_checking_a_line_that_is_not_a_task_is_refused(vault):
    await sqlite_mod.init_db(vault)
    path = vault.wiki_dir / "a.md"
    path.write_text("Just prose.\n", encoding="utf-8")
    await sqlite_mod.upsert_page("a", "A", path.read_text(encoding="utf-8"), [], "user", "default")

    with pytest.raises(ValueError):
        await set_task_done(slug="a", line=1, done=True, wiki_id="default", settings=vault)
