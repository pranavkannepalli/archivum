"""Two things writing at once should queue, not fail.

The vault runs background workers — distillation, summaries, page writes,
transcript capture — alongside whatever the user is doing. SQLite serialises
writers, which is fine; what is not fine is that with no `busy_timeout` the
loser of a race fails immediately with "database is locked" rather than waiting
its turn. A forced reindex on a live vault hit exactly this and returned a 500.
"""

import asyncio

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings


@pytest.fixture
async def settings(tmp_path):
    settings = Settings(db_path=tmp_path / "archivum.db", blob_dir=tmp_path / "blobs")
    await sqlite_mod.init_db(settings)
    return settings


async def test_connections_wait_for_a_busy_database(settings):
    async with sqlite_mod.get_db() as conn:
        async with conn.execute("PRAGMA busy_timeout") as cursor:
            timeout = (await cursor.fetchone())[0]
    # The driver default is 5s, which a reindex holding a connection across
    # Kuzu and Qdrant projection can exceed. Waiting must be deliberate.
    assert timeout >= 30_000, "a writer that gives up early turns contention into a 500"


async def test_concurrent_writers_all_land(settings):
    async def write(index: int) -> None:
        await sqlite_mod.upsert_page(
            f"topics/{index}", f"Page {index}", "body", [], "user", "default"
        )

    await asyncio.gather(*(write(i) for i in range(24)))

    pages = await sqlite_mod.list_pages("default")
    assert len(pages) == 24
