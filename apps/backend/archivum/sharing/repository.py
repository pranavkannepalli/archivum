"""Storage for principals, grants, and holds.

Takes an open connection rather than opening its own, matching
`MemoryAssetRegistry` and `SuggestionRepository`, so a caller can compose a
grant write with the write that caused it inside one transaction.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import aiosqlite

from archivum.sharing.models import (
    SHARE_ROLES,
    SUBJECT_KINDS,
    Grant,
    Hold,
    Principal,
    Subject,
    hash_token,
)
from archivum.sharing.schema import SHARING_SCHEMA
from archivum.sharing.urn import build, parse


async def init_sharing_schema(conn: aiosqlite.Connection) -> None:
    """Create the sharing tables on an open connection."""
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SHARING_SCHEMA)
    await conn.commit()


def _now() -> datetime:
    return datetime.now(UTC)


def is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """Return True when *expires_at* is in the past.

    An unparseable timestamp counts as expired. A share whose expiry cannot be
    read is a share nobody can reason about, and failing closed is the only
    safe reading of an ambiguous credential.
    """
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= (now or _now())


def _principal_from_row(row: aiosqlite.Row) -> Principal:
    return Principal(
        id=row["id"],
        wiki_id=row["wiki_id"],
        display_name=row["display_name"],
        claimed_at=row["claimed_at"],
        revoked=bool(row["revoked"]),
        created_at=row["created_at"] or "",
    )


def _grant_from_row(row: aiosqlite.Row) -> Grant:
    return Grant(
        id=row["id"],
        wiki_id=row["wiki_id"],
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        resource_urn=row["resource_urn"],
        role=row["role"],
        include_cited=bool(row["include_cited"]),
        created_by=row["created_by"],
        created_at=row["created_at"] or "",
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
    )


class SharingRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = aiosqlite.Row

    # ── Principals ────────────────────────────────────────────────────────────

    async def create_principal(
        self, wiki_id: str, display_name: str
    ) -> tuple[Principal, str]:
        """Create a recipient and return it with its one-time claim token."""
        name = display_name.strip()
        if not name:
            raise ValueError("A principal needs a display name")

        principal_id = f"prn_{secrets.token_urlsafe(12)}"
        claim_token = secrets.token_urlsafe(32)

        await self._conn.execute(
            "INSERT INTO share_principals (id, wiki_id, display_name, claim_hash) "
            "VALUES (?, ?, ?, ?)",
            (principal_id, wiki_id, name, hash_token(claim_token)),
        )
        await self._conn.commit()

        principal = await self.get_principal(principal_id)
        assert principal is not None
        return principal, claim_token

    async def get_principal(self, principal_id: str) -> Principal | None:
        async with self._conn.execute(
            "SELECT * FROM share_principals WHERE id = ?", (principal_id,)
        ) as cur:
            row = await cur.fetchone()
        return _principal_from_row(row) if row else None

    async def list_principals(self, wiki_id: str) -> list[Principal]:
        async with self._conn.execute(
            "SELECT * FROM share_principals WHERE wiki_id = ? AND revoked = 0 "
            "ORDER BY created_at DESC",
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_principal_from_row(row) for row in rows]

    async def claim_principal(self, claim_token: str) -> Principal | None:
        """Consume a claim token, binding the principal to its holder."""
        async with self._conn.execute(
            "SELECT * FROM share_principals "
            "WHERE claim_hash = ? AND revoked = 0 AND claimed_at IS NULL",
            (hash_token(claim_token),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None

        await self._conn.execute(
            "UPDATE share_principals "
            "SET claimed_at = ?, claim_hash = NULL WHERE id = ?",
            (_now().isoformat(), row["id"]),
        )
        await self._conn.commit()
        return await self.get_principal(row["id"])

    async def revoke_principal(self, principal_id: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE share_principals SET revoked = 1 WHERE id = ? AND revoked = 0",
            (principal_id,),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    # ── Grants ────────────────────────────────────────────────────────────────

    async def create_grant(
        self,
        *,
        wiki_id: str,
        subject: Subject,
        resource_urn: str,
        role: str,
        created_by: str,
        expires_in_days: int | None = None,
        include_cited: bool = False,
    ) -> Grant:
        if subject.kind not in SUBJECT_KINDS:
            raise ValueError(f"Unsupported share subject kind: {subject.kind}")
        if role not in SHARE_ROLES:
            raise ValueError(f"Unsupported share role: {role}")

        resource = parse(resource_urn)
        if resource.wiki_id != wiki_id:
            raise ValueError(
                f"Resource {resource_urn!r} does not belong to wiki {wiki_id!r}"
            )

        expires_at: str | None = None
        if expires_in_days is not None:
            expires_at = (_now() + timedelta(days=expires_in_days)).isoformat()

        grant_id = f"grt_{secrets.token_urlsafe(12)}"
        await self._conn.execute(
            """
            INSERT INTO share_grants
                (id, wiki_id, subject_kind, subject_id, resource_urn, role,
                 include_cited, created_by, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_kind, subject_id, resource_urn) DO UPDATE SET
                role = excluded.role,
                include_cited = excluded.include_cited,
                expires_at = excluded.expires_at,
                revoked = 0
            """,
            (
                grant_id,
                wiki_id,
                subject.kind,
                subject.id,
                resource_urn,
                role,
                int(include_cited),
                created_by,
                expires_at,
            ),
        )
        await self._conn.commit()

        # The insert may have collapsed into an update of an existing row, so
        # read back by identity rather than trusting the id we generated.
        async with self._conn.execute(
            "SELECT * FROM share_grants "
            "WHERE subject_kind = ? AND subject_id = ? AND resource_urn = ?",
            (subject.kind, subject.id, resource_urn),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return _grant_from_row(row)

    async def create_link_grant(
        self,
        *,
        wiki_id: str,
        resource_urn: str,
        role: str,
        created_by: str,
        expires_in_days: int | None = None,
        include_cited: bool = False,
    ) -> tuple[Grant, str]:
        """Create an anyone-with-the-link grant, returning it with its token."""
        token = secrets.token_urlsafe(32)
        grant = await self.create_grant(
            wiki_id=wiki_id,
            subject=Subject.link_from_token(token),
            resource_urn=resource_urn,
            role=role,
            created_by=created_by,
            expires_in_days=expires_in_days,
            include_cited=include_cited,
        )
        return grant, token

    async def get_grant(self, grant_id: str) -> Grant | None:
        async with self._conn.execute(
            "SELECT * FROM share_grants WHERE id = ?", (grant_id,)
        ) as cur:
            row = await cur.fetchone()
        return _grant_from_row(row) if row else None

    async def list_grants_for_subject(self, subject: Subject) -> list[Grant]:
        """Every live grant held by *subject*, revoked principals excluded."""
        async with self._conn.execute(
            """
            SELECT g.* FROM share_grants g
            LEFT JOIN share_principals p
                   ON g.subject_kind = 'principal' AND g.subject_id = p.id
            WHERE g.subject_kind = ?
              AND g.subject_id = ?
              AND g.revoked = 0
              AND (g.subject_kind <> 'principal' OR COALESCE(p.revoked, 1) = 0)
            """,
            (subject.kind, subject.id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            grant
            for grant in (_grant_from_row(row) for row in rows)
            if not is_expired(grant.expires_at)
        ]

    async def list_grants_for_resource(self, resource_urn: str) -> list[Grant]:
        async with self._conn.execute(
            "SELECT * FROM share_grants WHERE resource_urn = ? AND revoked = 0 "
            "ORDER BY created_at DESC",
            (resource_urn,),
        ) as cur:
            rows = await cur.fetchall()
        return [_grant_from_row(row) for row in rows]

    async def revoke_grant(self, grant_id: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE share_grants SET revoked = 1 WHERE id = ? AND revoked = 0",
            (grant_id,),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    # ── Holds ─────────────────────────────────────────────────────────────────

    async def hold(
        self, grant_id: str, resource_urn: str, reason: str = "agent_authored"
    ) -> Hold:
        await self._conn.execute(
            "INSERT OR IGNORE INTO share_holds (grant_id, resource_urn, reason) "
            "VALUES (?, ?, ?)",
            (grant_id, resource_urn, reason),
        )
        await self._conn.commit()
        return Hold(grant_id=grant_id, resource_urn=resource_urn, reason=reason)

    async def release_hold(self, grant_id: str, resource_urn: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM share_holds WHERE grant_id = ? AND resource_urn = ?",
            (grant_id, resource_urn),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def held_urns(self, grant_ids: Iterable[str]) -> set[str]:
        ids = list(grant_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        async with self._conn.execute(
            f"SELECT resource_urn FROM share_holds WHERE grant_id IN ({placeholders})",
            tuple(ids),
        ) as cur:
            rows = await cur.fetchall()
        return {row["resource_urn"] for row in rows}

    # ── Views ─────────────────────────────────────────────────────────────────

    async def create_view(
        self,
        *,
        wiki_id: str,
        title: str,
        payload: dict[str, Any],
        kind: str = "query_snapshot",
        view_id: str | None = None,
    ) -> str:
        """Store a shareable view and return its urn."""
        identifier = view_id or f"vw_{secrets.token_urlsafe(12)}"
        await self._conn.execute(
            "INSERT OR IGNORE INTO share_views (id, wiki_id, kind, title, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (identifier, wiki_id, kind, title, json.dumps(payload)),
        )
        await self._conn.commit()
        return build("view", wiki_id, identifier)

    async def get_view(self, resource_urn: str) -> dict[str, Any] | None:
        resource = parse(resource_urn)
        if resource.kind != "view":
            return None
        async with self._conn.execute(
            "SELECT * FROM share_views WHERE id = ? AND wiki_id = ?",
            (resource.local_id, resource.wiki_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None

        record = dict(row)
        try:
            record["payload"] = json.loads(record["payload"])
        except (TypeError, json.JSONDecodeError):
            record["payload"] = {}
        return record

    async def list_holds(self, wiki_id: str) -> list[dict[str, Any]]:
        """Holds waiting on the owner, with the grant and principal behind them."""
        async with self._conn.execute(
            """
            SELECT h.grant_id, h.resource_urn, h.reason, h.created_at,
                   g.role, g.resource_urn AS grant_urn,
                   g.subject_kind, g.subject_id,
                   p.display_name
            FROM share_holds h
            JOIN share_grants g ON g.id = h.grant_id
            LEFT JOIN share_principals p ON p.id = g.subject_id
            WHERE g.wiki_id = ? AND g.revoked = 0
            ORDER BY h.created_at DESC
            """,
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
