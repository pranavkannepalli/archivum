import json

import aiosqlite
import pytest

from archivum.sharing.migration import migrate_share_links
from archivum.sharing.models import Subject
from archivum.sharing.repository import SharingRepository, init_sharing_schema
from archivum.sharing.resolver import resolve

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    token       TEXT    NOT NULL UNIQUE,
    type        TEXT    NOT NULL DEFAULT 'page',
    target_id   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);
"""


async def _prepare(conn, rows):
    await conn.executescript(LEGACY_SCHEMA)
    await init_sharing_schema(conn)
    for row in rows:
        await conn.execute(
            "INSERT INTO share_links (token, type, target_id, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?)",
            row,
        )
    await conn.commit()
    return SharingRepository(conn)


@pytest.mark.asyncio
async def test_a_legacy_page_link_still_opens_after_migration():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _prepare(conn, [("tok_page", "page", "shared-page", None, 0)])
        await migrate_share_links(conn)

        access = await resolve(
            repo, Subject.link_from_token("tok_page"), "entry:default:shared-page"
        )
        assert access is not None
        assert access.role == "viewer"


@pytest.mark.asyncio
async def test_a_legacy_query_link_becomes_a_frozen_view():
    payload = {
        "question": "What did we decide about deploys?",
        "answer": "Ship on green.",
        "citations": [{"slug": "decisions/deploy", "title": "Deploy policy"}],
    }
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _prepare(
            conn, [("tok_query", "query", json.dumps(payload), None, 0)]
        )
        await migrate_share_links(conn)

        grants = await repo.list_grants_for_subject(Subject.link_from_token("tok_query"))
        assert len(grants) == 1
        assert grants[0].resource_urn.startswith("view:default:")

        view = await repo.get_view(grants[0].resource_urn)
        assert view is not None
        assert view["kind"] == "query_snapshot"
        assert view["payload"]["answer"] == "Ship on green."
        assert view["title"] == "What did we decide about deploys?"


@pytest.mark.asyncio
async def test_a_revoked_legacy_link_stays_revoked():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _prepare(conn, [("tok_dead", "page", "old-page", None, 1)])
        await migrate_share_links(conn)

        assert (
            await resolve(
                repo, Subject.link_from_token("tok_dead"), "entry:default:old-page"
            )
            is None
        )


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _prepare(conn, [("tok_page", "page", "shared-page", None, 0)])
        await migrate_share_links(conn)
        await migrate_share_links(conn)

        grants = await repo.list_grants_for_subject(Subject.link_from_token("tok_page"))
        assert len(grants) == 1


@pytest.mark.asyncio
async def test_migration_skips_rows_it_cannot_address():
    # A page row with no target, or a query row holding unparseable JSON, has
    # nothing to point a grant at. Skipping beats inventing a urn.
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _prepare(
            conn,
            [
                ("tok_empty", "page", None, None, 0),
                ("tok_bad", "query", "not json", None, 0),
            ],
        )
        await migrate_share_links(conn)

        assert await repo.list_grants_for_subject(Subject.link_from_token("tok_empty")) == []
        assert await repo.list_grants_for_subject(Subject.link_from_token("tok_bad")) == []


@pytest.mark.asyncio
async def test_migration_is_a_no_op_when_there_is_no_legacy_table():
    async with aiosqlite.connect(":memory:") as conn:
        await init_sharing_schema(conn)
        await migrate_share_links(conn)  # must not raise
