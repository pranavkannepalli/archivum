"""Fold the legacy `share_links` table into grants.

Old links keep working: their token still hashes to the same subject, so a URL
someone was handed months ago resolves through the new resolver untouched.
Nothing is deleted — `share_links` stays as it is, and this reads from it.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from archivum.sharing.models import hash_token
from archivum.sharing.urn import UrnError, build

logger = logging.getLogger(__name__)


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


def _snapshot_payload(target_id: object) -> dict[str, object] | None:
    if not isinstance(target_id, str):
        return None
    try:
        payload = json.loads(target_id)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def migrate_share_links(conn: aiosqlite.Connection) -> int:
    """Create a grant for every legacy share link. Returns the number migrated."""
    if not await _table_exists(conn, "share_links"):
        return 0

    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT * FROM share_links") as cur:
        rows = await cur.fetchall()

    migrated = 0
    for row in rows:
        token = row["token"]
        wiki_id = row["wiki_id"] or "default"
        subject_id = hash_token(token)
        link_type = (row["type"] or "page").strip().lower()

        if link_type == "page":
            target = row["target_id"]
            if not target:
                continue
            try:
                resource_urn = build("entry", wiki_id, str(target))
            except UrnError:
                logger.warning("Skipping unaddressable legacy share link %s", row["id"])
                continue

        elif link_type == "query":
            payload = _snapshot_payload(row["target_id"])
            if payload is None:
                continue
            # Deriving the view id from the token hash makes this idempotent:
            # re-running produces the same id, and INSERT OR IGNORE no-ops.
            view_id = f"vw_{subject_id[:16]}"
            await conn.execute(
                "INSERT OR IGNORE INTO share_views (id, wiki_id, kind, title, payload) "
                "VALUES (?, ?, 'query_snapshot', ?, ?)",
                (
                    view_id,
                    wiki_id,
                    str(payload.get("question") or "Shared query"),
                    json.dumps(payload),
                ),
            )
            resource_urn = build("view", wiki_id, view_id)

        else:
            logger.warning("Skipping legacy share link of unknown type %r", link_type)
            continue

        # Written with raw SQL rather than through the repository so the
        # original revoked flag and expiry survive as they are; the repository's
        # create path deliberately un-revokes on conflict, which is right for a
        # re-share and wrong for a migration.
        await conn.execute(
            """
            INSERT OR IGNORE INTO share_grants
                (id, wiki_id, subject_kind, subject_id, resource_urn, role,
                 created_by, created_at, expires_at, revoked)
            VALUES (?, ?, 'link', ?, ?, 'viewer', 'migration', ?, ?, ?)
            """,
            (
                f"grt_legacy_{subject_id[:16]}",
                wiki_id,
                subject_id,
                resource_urn,
                row["created_at"],
                row["expires_at"],
                int(row["revoked"] or 0),
            ),
        )
        migrated += 1

    await conn.commit()
    return migrated
