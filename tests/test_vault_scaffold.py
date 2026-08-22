"""A vault you open for the first time should not be an empty box.

Default folders are a small thing that decides whether capture has somewhere
obvious to go. They are created once, never re-created if you delete one, and
never imposed on a vault that already has its own shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings
from archivum.vault_scaffold import DEFAULT_FOLDERS, ensure_default_folders


@pytest.fixture
def vault(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    return settings


async def test_a_fresh_vault_gets_the_default_folders(vault):
    await sqlite_mod.init_db(vault)

    created = await ensure_default_folders(wiki_id="default", settings=vault)

    assert set(created) == set(DEFAULT_FOLDERS)
    folders = {row["path"] for row in await sqlite_mod.list_folders("default")}
    assert set(DEFAULT_FOLDERS) <= folders


async def test_running_it_twice_creates_nothing_the_second_time(vault):
    await sqlite_mod.init_db(vault)
    await ensure_default_folders(wiki_id="default", settings=vault)

    assert await ensure_default_folders(wiki_id="default", settings=vault) == []


async def test_a_folder_you_deleted_stays_deleted(vault):
    """Scaffolding is a starting point, not a policy about what must exist."""
    await sqlite_mod.init_db(vault)
    await ensure_default_folders(wiki_id="default", settings=vault)
    await sqlite_mod.delete_folders_under("reading", "default")

    created = await ensure_default_folders(wiki_id="default", settings=vault)

    assert "reading" not in created, "re-creating it would override your choice"


async def test_a_vault_that_already_has_folders_is_left_alone(vault):
    """Someone with their own structure should not be handed a second one."""
    await sqlite_mod.init_db(vault)
    await sqlite_mod.create_folder("zettel", "default")

    created = await ensure_default_folders(wiki_id="default", settings=vault)

    assert created == [], "an established vault keeps its own shape"
