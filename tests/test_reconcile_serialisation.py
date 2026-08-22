"""A vault reconcile must not run on top of another one.

Startup runs a reconcile when `vault_reconcile_on_start` is set, and the reindex
endpoint runs the same pass. Triggering a reindex while the startup sweep is
still going put two full passes over the same pages at once, and the second one
died on `database is locked` — not because anything waited too long, but because
the work should never have overlapped.
"""

import asyncio

import pytest

import archivum.indexing as indexing
from archivum.config import Settings
from archivum.db import sqlite as sqlite_mod


@pytest.fixture
async def settings(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    await sqlite_mod.init_db(settings)
    return settings


async def test_two_reconciles_do_not_overlap(settings, monkeypatch):
    overlapping = False
    running = 0

    async def slow_reindex(slug, **kwargs):
        nonlocal overlapping, running
        running += 1
        if running > 1:
            overlapping = True
        await asyncio.sleep(0.02)
        running -= 1
        return indexing.ReindexResult(slug=slug, action="indexed")

    for name in ("a", "b", "c"):
        (settings.wiki_dir / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr(indexing, "reindex_page", slow_reindex)

    await asyncio.gather(
        indexing.reconcile_vault(wiki_id="default", settings=settings),
        indexing.reconcile_vault(wiki_id="default", settings=settings),
    )

    assert not overlapping, "a second pass must wait rather than race the first"
