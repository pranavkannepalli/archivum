"""Persistence for agent-proposed memory edits awaiting human review."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field


SuggestionStatus = Literal[
    "pending",
    "accepted",
    "edited",
    "rejected",
    "merged",
    "replaced",
    "kept",
    "retired",
    "scope_changed",
    "visibility_changed",
    "expired",
]
SuggestionAction = Literal[
    "accept",
    "edit",
    "reject",
    "merge",
    "replace",
    "keep_both",
    "retire",
    "change_scope",
    "change_visibility",
    "expire",
]

ACTION_TO_STATUS: dict[SuggestionAction, SuggestionStatus] = {
    "accept": "accepted",
    "edit": "edited",
    "reject": "rejected",
    "merge": "merged",
    "replace": "replaced",
    "keep_both": "kept",
    "retire": "retired",
    "change_scope": "scope_changed",
    "change_visibility": "visibility_changed",
    "expire": "expired",
}

_STATUS_SQL = (
    "'pending', 'accepted', 'edited', 'rejected', 'merged', 'replaced', "
    "'kept', 'retired', 'scope_changed', 'visibility_changed', 'expired'"
)


def page_target_prefix(wiki_id: str) -> str:
    return f"page:{wiki_id}:"


def wiki_target_id(wiki_id: str) -> str:
    return f"wiki:{wiki_id}"


def wiki_target_prefix(wiki_id: str) -> str:
    return f"wiki:{wiki_id}:"


def wiki_scope(wiki_id: str) -> dict[str, list[str]]:
    """Target filters that restrict a suggestion query to one wiki.

    Suggestions are keyed by a target id that embeds the wiki, and
    `list_suggestions` only applies tenancy when target filters are supplied —
    so an unfiltered call returns every wiki's rows. Any aggregate over
    suggestions must pass these filters.
    """
    return {
        "target_ids": [wiki_target_id(wiki_id)],
        "target_prefixes": [page_target_prefix(wiki_id), wiki_target_prefix(wiki_id)],
    }


class MemorySuggestion(BaseModel):
    id: str
    target_id: str
    suggestion_type: str
    proposed_markdown: str
    proposed_objects: list[Any]
    citations: list[Any]
    proposed_scopes: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    duplicates: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    retention_tier: str = "candidate"
    agent_visibility: str = "review_required"
    rationale: str = ""
    estimated_durability: str = ""
    expires_at: str | None = None
    # None for agent-authored suggestions; a share principal id when a
    # recipient proposed it.
    author_principal_id: str | None = None
    status: SuggestionStatus
    # Written by SQLite defaults on insert/update. Exposed so the activity
    # stream can order suggestions against page edits and ingest logs.
    created_at: str = ""
    updated_at: str = ""


async def init_suggestion_schema(conn: aiosqlite.Connection) -> None:
    """Create the review queue table on an open SQLite connection."""
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_suggestions (
            id                TEXT PRIMARY KEY,
            target_id         TEXT NOT NULL,
            suggestion_type   TEXT NOT NULL,
            proposed_markdown TEXT NOT NULL,
            proposed_objects  TEXT NOT NULL,
            citations         TEXT NOT NULL,
            proposed_scopes   TEXT NOT NULL DEFAULT '[]',
            scores            TEXT NOT NULL DEFAULT '{}',
            duplicates        TEXT NOT NULL DEFAULT '[]',
            conflicts         TEXT NOT NULL DEFAULT '[]',
            retention_tier    TEXT NOT NULL DEFAULT 'candidate',
            agent_visibility  TEXT NOT NULL DEFAULT 'review_required',
            rationale         TEXT NOT NULL DEFAULT '',
            estimated_durability TEXT NOT NULL DEFAULT '',
            expires_at        TEXT,
            status            TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'accepted', 'edited',
                                                'rejected', 'merged', 'replaced',
                                                'kept', 'retired', 'scope_changed',
                                                'visibility_changed', 'expired')),
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_memory_suggestions_target_status
            ON memory_suggestions(target_id, status);
        """
    )
    await _migrate_status_check(conn)
    await _migrate_review_card_columns(conn)
    await conn.commit()


async def _migrate_status_check(conn: aiosqlite.Connection) -> None:
    """Expand older suggestion tables whose CHECK only allowed accept/reject."""
    async with conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_suggestions'"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or "scope_changed" in row["sql"]:
        return
    await conn.executescript(
        f"""
        CREATE TABLE memory_suggestions_new (
            id                TEXT PRIMARY KEY,
            target_id         TEXT NOT NULL,
            suggestion_type   TEXT NOT NULL,
            proposed_markdown TEXT NOT NULL,
            proposed_objects  TEXT NOT NULL,
            citations         TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ({_STATUS_SQL})),
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        INSERT INTO memory_suggestions_new
            (id, target_id, suggestion_type, proposed_markdown,
             proposed_objects, citations, status, created_at, updated_at)
        SELECT id, target_id, suggestion_type, proposed_markdown,
               proposed_objects, citations, status, created_at, updated_at
        FROM memory_suggestions;

        DROP TABLE memory_suggestions;
        ALTER TABLE memory_suggestions_new RENAME TO memory_suggestions;
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_suggestions_target_status
            ON memory_suggestions(target_id, status)
        """
    )
    await conn.commit()


async def _migrate_review_card_columns(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(memory_suggestions)") as cursor:
        rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    additions = {
        "proposed_scopes": "TEXT NOT NULL DEFAULT '[]'",
        "scores": "TEXT NOT NULL DEFAULT '{}'",
        "duplicates": "TEXT NOT NULL DEFAULT '[]'",
        "conflicts": "TEXT NOT NULL DEFAULT '[]'",
        "retention_tier": "TEXT NOT NULL DEFAULT 'candidate'",
        "agent_visibility": "TEXT NOT NULL DEFAULT 'review_required'",
        "rationale": "TEXT NOT NULL DEFAULT ''",
        "estimated_durability": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "TEXT",
        # Set when a share recipient proposed the edit, so the review queue can
        # say "Alice suggested this" rather than attributing it to an agent.
        "author_principal_id": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            await conn.execute(
                f"ALTER TABLE memory_suggestions ADD COLUMN {column} {definition}"
            )
    await conn.commit()


class SuggestionRepository:
    """CRUD and review-state transitions for proposed memory edits."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create_suggestion(
        self,
        *,
        target_id: str,
        suggestion_type: str,
        proposed_markdown: str,
        proposed_objects: list[Any],
        citations: list[Any],
        proposed_scopes: list[str] | None = None,
        scores: dict[str, float] | None = None,
        duplicates: list[str] | None = None,
        conflicts: list[str] | None = None,
        retention_tier: str = "candidate",
        agent_visibility: str = "review_required",
        rationale: str = "",
        estimated_durability: str = "",
        expires_at: str | None = None,
        author_principal_id: str | None = None,
    ) -> MemorySuggestion:
        suggestion = MemorySuggestion(
            id=f"suggestion:{uuid4()}",
            target_id=target_id,
            suggestion_type=suggestion_type,
            proposed_markdown=proposed_markdown,
            proposed_objects=proposed_objects,
            citations=citations,
            proposed_scopes=proposed_scopes or [],
            scores=scores or {},
            duplicates=duplicates or [],
            conflicts=conflicts or [],
            retention_tier=retention_tier,
            agent_visibility=agent_visibility,
            rationale=rationale,
            estimated_durability=estimated_durability,
            expires_at=expires_at,
            author_principal_id=author_principal_id,
            status="pending",
        )
        await self._conn.execute(
            """
            INSERT INTO memory_suggestions
                (id, target_id, suggestion_type, proposed_markdown,
                 proposed_objects, citations, proposed_scopes, scores, duplicates,
                 conflicts, retention_tier, agent_visibility, rationale,
                 estimated_durability, expires_at, author_principal_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                suggestion.id,
                suggestion.target_id,
                suggestion.suggestion_type,
                suggestion.proposed_markdown,
                json.dumps(suggestion.proposed_objects),
                json.dumps(suggestion.citations),
                json.dumps(suggestion.proposed_scopes),
                json.dumps(suggestion.scores),
                json.dumps(suggestion.duplicates),
                json.dumps(suggestion.conflicts),
                suggestion.retention_tier,
                suggestion.agent_visibility,
                suggestion.rationale,
                suggestion.estimated_durability,
                suggestion.expires_at,
                suggestion.author_principal_id,
                suggestion.status,
            ),
        )
        await self._conn.commit()
        # Re-read so created_at/updated_at come back with the values SQLite
        # defaulted them to, rather than the empty strings on the draft above.
        return await self.get_suggestion(suggestion.id) or suggestion

    async def get_suggestion(self, suggestion_id: str) -> MemorySuggestion | None:
        async with self._conn.execute(
            "SELECT * FROM memory_suggestions WHERE id=?", (suggestion_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_suggestion(row) if row else None

    async def list_suggestions(
        self,
        *,
        target_id: str | None = None,
        target_ids: list[str] | None = None,
        target_prefixes: list[str] | None = None,
        status: SuggestionStatus | None = "pending",
        before_inclusive: str | None = None,
        before_id: str | None = None,
        exclude_tied: bool = False,
    ) -> list[MemorySuggestion]:
        clauses: list[str] = []
        params: list[str] = []
        if target_id is not None:
            clauses.append("target_id=?")
            params.append(target_id)
        target_clauses: list[str] = []
        target_params: list[str] = []
        if target_ids:
            placeholders = ", ".join("?" for _ in target_ids)
            target_clauses.append(f"target_id IN ({placeholders})")
            target_params.extend(target_ids)
        if target_prefixes:
            for prefix in target_prefixes:
                target_clauses.append("target_id LIKE ?")
                target_params.append(f"{prefix}%")
        if target_clauses:
            clauses.append("(" + " OR ".join(target_clauses) + ")")
            params.extend(target_params)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if before_inclusive:
            # Row-value comparison so a capped slice can walk a run of records
            # sharing updated_at rather than repeating its top rows.
            if before_id:
                clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                params.extend([before_inclusive, before_inclusive, before_id])
            elif exclude_tied:
                clauses.append("updated_at < ?")
                params.append(before_inclusive)
            else:
                clauses.append("updated_at <= ?")
                params.append(before_inclusive)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._conn.execute(
            f"SELECT * FROM memory_suggestions {where} "
            # Descending tie-break, matching the activity feed's merge order.
            "ORDER BY updated_at DESC, created_at DESC, id DESC",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_suggestion(row) for row in rows]

    async def repoint_page(
        self, *, wiki_id: str, old_slug: str, new_slug: str
    ) -> int:
        """Follow a renamed page so pending review items stay actionable."""
        old_target = f"{page_target_prefix(wiki_id)}{old_slug}"
        new_target = f"{page_target_prefix(wiki_id)}{new_slug}"
        async with self._conn.execute(
            "UPDATE memory_suggestions SET target_id=?, updated_at=datetime('now') "
            "WHERE target_id=?",
            (new_target, old_target),
        ) as cursor:
            moved = cursor.rowcount or 0
        moved += await self._repoint_citations(
            wiki_id=wiki_id, old_slug=old_slug, new_slug=new_slug
        )
        await self._conn.commit()
        return moved

    async def _repoint_citations(
        self, *, wiki_id: str, old_slug: str, new_slug: str
    ) -> int:
        """Follow the rename into the citations that name the page.

        Only `target_id` used to move, which misses everything distillation
        produces: those target `wiki:<id>` and record the page they came from in
        their citations. Left behind, a pending item cites a slug that no longer
        resolves, so it is not attributable to any page and disappears from
        review rather than showing up against the moved one.

        Source ids are compared whole, never by prefix — `topics/old` must not
        drag `topics/old-but-different` along with it.
        """
        old_ids = {f"page:{old_slug}", f"page:{wiki_id}:{old_slug}"}
        async with self._conn.execute(
            "SELECT id, citations FROM memory_suggestions WHERE citations LIKE ?",
            (f"%{old_slug}%",),
        ) as cursor:
            rows = await cursor.fetchall()

        touched = 0
        for row in rows:
            try:
                citations = json.loads(row["citations"])
            except (TypeError, ValueError):
                continue
            if not isinstance(citations, list):
                continue
            changed = False
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                source_id = citation.get("source_id")
                if source_id not in old_ids:
                    continue
                prefix = "page:" if source_id == f"page:{old_slug}" else f"page:{wiki_id}:"
                citation["source_id"] = f"{prefix}{new_slug}"
                changed = True
            if not changed:
                continue
            await self._conn.execute(
                "UPDATE memory_suggestions SET citations=?, updated_at=datetime('now') "
                "WHERE id=?",
                (json.dumps(citations), row["id"]),
            )
            touched += 1
        return touched

    async def expire_for_page(self, *, wiki_id: str, slug: str) -> int:
        """Retire suggestions against a page that no longer exists.

        A pending suggestion targeting a deleted page can never be accepted, so
        leaving it pending would keep an un-actionable item in the review queue
        forever.
        """
        target = f"{page_target_prefix(wiki_id)}{slug}"
        async with self._conn.execute(
            "UPDATE memory_suggestions SET status='expired', updated_at=datetime('now') "
            "WHERE target_id=? AND status='pending'",
            (target,),
        ) as cursor:
            expired = cursor.rowcount or 0
        await self._conn.commit()
        return expired

    async def suggestion_counts(self, *, wiki_id: str) -> dict[str, int]:
        """How many suggestions sit in each review state, for one wiki."""
        scope = wiki_scope(wiki_id)
        clauses = ["target_id IN ({})".format(
            ", ".join("?" for _ in scope["target_ids"])
        )]
        params: list[str] = list(scope["target_ids"])
        for prefix in scope["target_prefixes"]:
            clauses.append("target_id LIKE ?")
            params.append(f"{prefix}%")

        counts: dict[str, int] = {}
        async with self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_suggestions "
            f"WHERE {' OR '.join(clauses)} GROUP BY status",
            params,
        ) as cursor:
            for row in await cursor.fetchall():
                counts[row["status"]] = row["n"]
        return counts

    async def accept_suggestion(self, suggestion_id: str) -> None:
        await self._transition(suggestion_id, "accepted")

    async def reject_suggestion(self, suggestion_id: str) -> None:
        await self._transition(suggestion_id, "rejected")

    async def transition_suggestion(
        self,
        suggestion_id: str,
        action: SuggestionAction,
        *,
        commit: bool = True,
    ) -> None:
        await self._transition(suggestion_id, ACTION_TO_STATUS[action], commit=commit)

    async def expire_due_candidates(self, now: str) -> list[MemorySuggestion]:
        async with self._conn.execute(
            """
            SELECT id FROM memory_suggestions
            WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?
            ORDER BY expires_at ASC, id ASC
            """,
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
        return await self._expire_rows(rows)

    async def expire_stale_candidates(
        self,
        now: str,
        *,
        ttl_days: int,
        wiki_id: str | None = None,
        exclude_wiki_ids: list[str] | None = None,
    ) -> list[MemorySuggestion]:
        """Expire pending candidates with no explicit expiry past the scope TTL.

        Retention policy is per-wiki: pass `wiki_id` to expire only that
        wiki's candidates, or `exclude_wiki_ids` to sweep the remainder with
        a default TTL without touching wikis that configured their own.
        """
        clauses = [
            "status='pending'",
            "expires_at IS NULL",
            "datetime(created_at) <= datetime(?, ?)",
        ]
        params: list[str] = [now, f"-{int(ttl_days)} days"]
        if wiki_id is not None:
            clauses.append(
                "(target_id = ? OR target_id LIKE ? OR target_id LIKE ?)"
            )
            params.extend(self._wiki_target_patterns(wiki_id))
        for excluded in exclude_wiki_ids or []:
            clauses.append(
                "NOT (target_id = ? OR target_id LIKE ? OR target_id LIKE ?)"
            )
            params.extend(self._wiki_target_patterns(excluded))
        async with self._conn.execute(
            f"""
            SELECT id FROM memory_suggestions
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return await self._expire_rows(rows)

    @staticmethod
    def _wiki_target_patterns(wiki_id: str) -> tuple[str, str, str]:
        return (f"wiki:{wiki_id}", f"wiki:{wiki_id}:%", f"page:{wiki_id}:%")

    async def _expire_rows(self, rows) -> list[MemorySuggestion]:
        expired: list[MemorySuggestion] = []
        for row in rows:
            await self._transition(row["id"], "expired")
            loaded = await self.get_suggestion(row["id"])
            if loaded is not None:
                expired.append(loaded)
        return expired

    async def _transition(
        self,
        suggestion_id: str,
        target_status: SuggestionStatus,
        *,
        commit: bool = True,
    ) -> None:
        if commit:
            await self._conn.execute("BEGIN")
        try:
            cursor = await self._conn.execute(
                """
                UPDATE memory_suggestions
                SET status=?, updated_at=datetime('now')
                WHERE id=? AND status='pending'
                """,
                (target_status, suggestion_id),
            )
            if cursor.rowcount == 0:
                async with self._conn.execute(
                    "SELECT status FROM memory_suggestions WHERE id=?", (suggestion_id,)
                ) as status_cursor:
                    row = await status_cursor.fetchone()
                if row is None:
                    raise KeyError(f"Suggestion '{suggestion_id}' not found")
                if row["status"] != target_status:
                    raise ValueError(
                        f"Suggestion '{suggestion_id}' is already {row['status']}"
                    )
            if commit:
                await self._conn.commit()
        except Exception:
            if commit:
                await self._conn.rollback()
            raise

    @staticmethod
    def _row_to_suggestion(row: aiosqlite.Row) -> MemorySuggestion:
        return MemorySuggestion(
            id=row["id"],
            target_id=row["target_id"],
            suggestion_type=row["suggestion_type"],
            proposed_markdown=row["proposed_markdown"],
            proposed_objects=json.loads(row["proposed_objects"]),
            citations=json.loads(row["citations"]),
            proposed_scopes=json.loads(row["proposed_scopes"]),
            scores=json.loads(row["scores"]),
            duplicates=json.loads(row["duplicates"]),
            conflicts=json.loads(row["conflicts"]),
            retention_tier=row["retention_tier"],
            agent_visibility=row["agent_visibility"],
            rationale=row["rationale"],
            estimated_durability=row["estimated_durability"],
            expires_at=row["expires_at"],
            author_principal_id=row["author_principal_id"],
            status=row["status"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )
