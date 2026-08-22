"""SQLite persistence for governed memory assets, agents, and bindings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from archivum.knowledge.models import Citation
from archivum.memory.models import (
    ASSET_STATUSES,
    ASSET_TYPES,
    ASSET_VISIBILITIES,
    BINDING_MODES,
    MEMORY_LAYERS,
    AgentProfile,
    AssetBinding,
    MemoryAsset,
    MemoryAssetVersion,
    MemoryScope,
)
from archivum.memory.schema import MEMORY_SCHEMA

_ASSET_LIST_LIMIT = 500


async def init_memory_schema(conn: aiosqlite.Connection) -> None:
    """Create the memory registry tables on an open SQLite connection."""
    conn.row_factory = aiosqlite.Row
    await conn.executescript(MEMORY_SCHEMA)
    await _migrate_memory_schema(conn)
    await conn.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _citations_json(citations: list[Citation]) -> str:
    return _dumps([citation.model_dump() for citation in citations])


def _validate(asset_type: str, layer: str, status: str, visibility: str) -> None:
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"Unsupported memory asset type: {asset_type}")
    if layer not in MEMORY_LAYERS:
        raise ValueError(f"Unsupported memory layer: {layer}")
    if status not in ASSET_STATUSES:
        raise ValueError(f"Unsupported memory asset status: {status}")
    if visibility not in ASSET_VISIBILITIES:
        raise ValueError(f"Unsupported memory asset visibility: {visibility}")


async def _migrate_memory_schema(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(memory_assets)") as cursor:
        rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    additions = {
        "approved_by": "TEXT",
        "reviewed_at": "TEXT",
        "supersedes": "TEXT NOT NULL DEFAULT '[]'",
        "superseded_by": "TEXT NOT NULL DEFAULT '[]'",
        "conflict_lineage": "TEXT NOT NULL DEFAULT '[]'",
        "retired_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            await conn.execute(f"ALTER TABLE memory_assets ADD COLUMN {column} {definition}")


class MemoryAssetRegistry:
    """Register, version, govern, and equip memory assets."""

    def __init__(self, conn: aiosqlite.Connection, *, autocommit: bool = True) -> None:
        self._conn = conn
        self._autocommit = autocommit

    # ── Scopes ────────────────────────────────────────────────────────────

    async def upsert_scope(
        self,
        *,
        id: str,
        wiki_id: str,
        scope_type: str,
        name: str,
        parent_scope_id: str | None = None,
        budget_tokens: int = 4_000,
        budget_items: int = 20,
        retention_policy: dict[str, Any] | None = None,
    ) -> MemoryScope:
        if scope_type not in {"human", "topic", "project", "repo", "person", "org"}:
            raise ValueError(f"Unsupported memory scope type: {scope_type}")
        if budget_tokens < 0 or budget_items < 0:
            raise ValueError("Memory scope budgets must be non-negative")
        now = _now()
        await self._conn.execute(
            """
            INSERT INTO memory_scopes
                (id, wiki_id, scope_type, name, parent_scope_id, budget_tokens,
                 budget_items, retention_policy, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(wiki_id, id) DO UPDATE SET
                scope_type=excluded.scope_type,
                name=excluded.name,
                parent_scope_id=excluded.parent_scope_id,
                budget_tokens=excluded.budget_tokens,
                budget_items=excluded.budget_items,
                retention_policy=excluded.retention_policy,
                updated_at=excluded.updated_at
            """,
            (
                id,
                wiki_id,
                scope_type,
                name,
                parent_scope_id,
                budget_tokens,
                budget_items,
                _dumps(retention_policy or {}),
                now,
                now,
            ),
        )
        await self._conn.commit()
        scope = await self.get_scope(id, wiki_id)
        assert scope is not None
        return scope

    async def get_scope(self, scope_id: str, wiki_id: str) -> MemoryScope | None:
        async with self._conn.execute(
            "SELECT * FROM memory_scopes WHERE wiki_id=? AND id=?",
            (wiki_id, scope_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_scope(row) if row else None

    async def list_scopes(
        self, wiki_id: str, scope_type: str | None = None
    ) -> list[MemoryScope]:
        clauses = ["wiki_id=?"]
        params: list[Any] = [wiki_id]
        if scope_type is not None:
            clauses.append("scope_type=?")
            params.append(scope_type)
        async with self._conn.execute(
            f"SELECT * FROM memory_scopes WHERE {' AND '.join(clauses)} "
            "ORDER BY scope_type ASC, name ASC, id ASC",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_scope(row) for row in rows]

    # ── Assets ────────────────────────────────────────────────────────────

    async def register_asset(
        self,
        *,
        id: str,
        wiki_id: str,
        asset_type: str,
        layer: str,
        name: str,
        scope: str,
        owner: str = "person:self",
        status: str = "draft",
        visibility: str = "private",
        page_slug: str | None = None,
        summary: str = "",
        body: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        citations: list[Citation] | None = None,
        approved_by: str | None = None,
        reviewed_at: str | None = None,
        supersedes: list[str] | None = None,
        superseded_by: list[str] | None = None,
        conflict_lineage: list[str] | None = None,
        retired_at: str | None = None,
        change_note: str = "",
    ) -> MemoryAsset:
        """Create or update an asset, bumping the version only when content changes.

        Status and visibility are governance state, not content: changing them
        alone does not create a new version. Use `set_status`/`set_visibility`.
        """
        _validate(asset_type, layer, status, visibility)
        tags = tags or []
        metadata = metadata or {}
        citations = citations or []
        supersedes = supersedes or []
        superseded_by = superseded_by or []
        conflict_lineage = conflict_lineage or []
        now = _now()
        effective_reviewed_at = reviewed_at or (now if approved_by else None)

        existing = await self.get_asset(id)
        if existing is None:
            version = 1
            created_at = now
            effective_status = status
            effective_visibility = visibility
        else:
            changed = (
                existing.name != name
                or existing.summary != summary
                or existing.body != body
                or existing.tags != tags
                or existing.metadata != metadata
                or [c.model_dump() for c in existing.citations]
                != [c.model_dump() for c in citations]
                or existing.approved_by != approved_by
                or existing.reviewed_at != effective_reviewed_at
                or existing.supersedes != supersedes
                or existing.superseded_by != superseded_by
                or existing.conflict_lineage != conflict_lineage
                or existing.retired_at != retired_at
            )
            version = existing.version + 1 if changed else existing.version
            created_at = existing.created_at
            # Governance state survives content edits.
            effective_status = existing.status
            effective_visibility = existing.visibility

        if self._autocommit:
            await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                """
                INSERT INTO memory_assets
                    (id, wiki_id, asset_type, layer, name, owner, scope, status,
                     visibility, version, page_slug, summary, body, tags, metadata,
                     citations, approved_by, reviewed_at, supersedes, superseded_by,
                     conflict_lineage, retired_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    wiki_id=excluded.wiki_id,
                    asset_type=excluded.asset_type,
                    layer=excluded.layer,
                    name=excluded.name,
                    owner=excluded.owner,
                    scope=excluded.scope,
                    version=excluded.version,
                    page_slug=excluded.page_slug,
                    summary=excluded.summary,
                    body=excluded.body,
                    tags=excluded.tags,
                    metadata=excluded.metadata,
                    citations=excluded.citations,
                    approved_by=excluded.approved_by,
                    reviewed_at=excluded.reviewed_at,
                    supersedes=excluded.supersedes,
                    superseded_by=excluded.superseded_by,
                    conflict_lineage=excluded.conflict_lineage,
                    retired_at=excluded.retired_at,
                    updated_at=excluded.updated_at
                """,
                (
                    id,
                    wiki_id,
                    asset_type,
                    layer,
                    name,
                    owner,
                    scope,
                    effective_status,
                    effective_visibility,
                    version,
                    page_slug,
                    summary,
                    body,
                    _dumps(tags),
                    _dumps(metadata),
                    _citations_json(citations),
                    approved_by,
                    effective_reviewed_at,
                    _dumps(supersedes),
                    _dumps(superseded_by),
                    _dumps(conflict_lineage),
                    retired_at,
                    created_at,
                    now,
                ),
            )
            for superseded_id in supersedes:
                await self._append_asset_list_field(
                    superseded_id, "superseded_by", id, updated_at=now
                )
            await self._conn.execute(
                """
                INSERT INTO memory_asset_versions
                    (asset_id, version, name, summary, body, status, metadata,
                     citations, change_note, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_id, version) DO UPDATE SET
                    name=excluded.name,
                    summary=excluded.summary,
                    body=excluded.body,
                    status=excluded.status,
                    metadata=excluded.metadata,
                    citations=excluded.citations,
                    change_note=excluded.change_note
                """,
                (
                    id,
                    version,
                    name,
                    summary,
                    body,
                    effective_status,
                    _dumps(metadata),
                    _citations_json(citations),
                    change_note,
                    now,
                ),
            )
            if self._autocommit:
                await self._conn.commit()
        except Exception:
            if self._autocommit:
                await self._conn.rollback()
            raise

        loaded = await self.get_asset(id)
        assert loaded is not None
        return loaded

    async def get_asset(self, asset_id: str) -> MemoryAsset | None:
        async with self._conn.execute(
            "SELECT * FROM memory_assets WHERE id=?", (asset_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_asset(row) if row else None

    async def get_asset_for_wiki(
        self, asset_id: str, wiki_id: str
    ) -> MemoryAsset | None:
        async with self._conn.execute(
            "SELECT * FROM memory_assets WHERE id=? AND wiki_id=?",
            (asset_id, wiki_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_asset(row) if row else None

    async def repoint_page(
        self, *, wiki_id: str, old_slug: str, new_slug: str
    ) -> int:
        """Follow a renamed page. Returns how many assets moved."""
        async with self._conn.execute(
            "UPDATE memory_assets SET page_slug=?, updated_at=? "
            "WHERE wiki_id=? AND page_slug=?",
            (new_slug, _now(), wiki_id, old_slug),
        ) as cursor:
            moved = cursor.rowcount or 0
        if self._autocommit:
            await self._conn.commit()
        return moved

    async def detach_page(self, *, wiki_id: str, slug: str) -> int:
        """Unhook memory from a deleted page without deleting the memory.

        What was learned survives the page it was learned from — the citation
        would be a lie either way, but silently dropping reviewed memory because
        a file moved would be worse.
        """
        async with self._conn.execute(
            "UPDATE memory_assets SET page_slug=NULL, updated_at=? "
            "WHERE wiki_id=? AND page_slug=?",
            (_now(), wiki_id, slug),
        ) as cursor:
            detached = cursor.rowcount or 0
        if self._autocommit:
            await self._conn.commit()
        return detached

    async def asset_counts(self, *, wiki_id: str) -> dict[str, Any]:
        """Aggregate asset counts for the memory pipeline view.

        Returned as raw counts rather than percentages so the caller decides how
        to present the funnel.
        """
        counts: dict[str, Any] = {
            "total": 0,
            "by_status": {},
            "by_layer": {},
            "disputed": 0,
        }
        async with self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_assets WHERE wiki_id=? GROUP BY status",
            (wiki_id,),
        ) as cursor:
            for row in await cursor.fetchall():
                counts["by_status"][row["status"]] = row["n"]
                counts["total"] += row["n"]
        async with self._conn.execute(
            "SELECT layer, COUNT(*) AS n FROM memory_assets WHERE wiki_id=? GROUP BY layer",
            (wiki_id,),
        ) as cursor:
            for row in await cursor.fetchall():
                counts["by_layer"][row["layer"]] = row["n"]
        # "Disputed" is derived, not stored: an asset carrying conflict lineage
        # is one the system knows two sources disagree about.
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM memory_assets "
            "WHERE wiki_id=? AND conflict_lineage NOT IN ('', '[]')",
            (wiki_id,),
        ) as cursor:
            row = await cursor.fetchone()
            counts["disputed"] = row["n"] if row else 0
        return counts

    async def list_assets(
        self,
        *,
        wiki_id: str,
        asset_type: str | None = None,
        layer: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        scope: str | None = None,
        page_slug: str | None = None,
        before_inclusive: str | None = None,
        before_id: str | None = None,
        exclude_tied: bool = False,
        limit: int = _ASSET_LIST_LIMIT,
    ) -> list[MemoryAsset]:
        clauses = ["wiki_id=?"]
        params: list[Any] = [wiki_id]
        if asset_type is not None:
            clauses.append("asset_type=?")
            params.append(asset_type)
        if layer is not None:
            clauses.append("layer=?")
            params.append(layer)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        # Owner answers "whose memory is this?" and scope answers "which vault
        # or repo does it belong to?". The profile page needs the former: every
        # asset is owned by person:self but scoped to a wiki.
        if owner is not None:
            clauses.append("owner=?")
            params.append(owner)
        if scope is not None:
            clauses.append("scope=?")
            params.append(scope)
        if page_slug is not None:
            clauses.append("page_slug=?")
            params.append(page_slug)
        if before_inclusive:
            # Row-value comparison so a capped slice can walk through a run of
            # records sharing updated_at instead of returning the same top rows.
            if before_id:
                # Compare on id||':'||version, which is exactly the tail of the
                # activity id the feed orders by. Comparing the raw id would
                # disagree whenever one asset id is a prefix of another
                # ("memory:persona:role" vs "memory:persona:role2").
                clauses.append(
                    "(updated_at < ? OR (updated_at = ? "
                    "AND (id || ':' || CAST(version AS TEXT)) < ?))"
                )
                params.extend([before_inclusive, before_inclusive, before_id])
            elif exclude_tied:
                clauses.append("updated_at < ?")
                params.append(before_inclusive)
            else:
                clauses.append("updated_at <= ?")
                params.append(before_inclusive)
        params.append(max(limit, 0))
        async with self._conn.execute(
            f"SELECT * FROM memory_assets WHERE {' AND '.join(clauses)} "
            # Tie-break on the same key the activity feed sorts by, descending,
            # so a capped slice of records sharing an updated_at returns the rows
            # the feed would order first.
            "ORDER BY updated_at DESC, (id || ':' || CAST(version AS TEXT)) DESC LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_asset(row) for row in rows]

    async def set_status(self, asset_id: str, status: str) -> MemoryAsset:
        if status not in ASSET_STATUSES:
            raise ValueError(f"Unsupported memory asset status: {status}")
        return await self._set_field(asset_id, "status", status)

    async def set_status_for_wiki(
        self, asset_id: str, wiki_id: str, status: str
    ) -> MemoryAsset:
        if status not in ASSET_STATUSES:
            raise ValueError(f"Unsupported memory asset status: {status}")
        return await self._set_field(asset_id, "status", status, wiki_id=wiki_id)

    async def _append_asset_list_field(
        self, asset_id: str, column: str, value: str, *, updated_at: str
    ) -> None:
        async with self._conn.execute(
            f"SELECT {column} FROM memory_assets WHERE id=?", (asset_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        values = json.loads(row[column])
        if value not in values:
            values.append(value)
            await self._conn.execute(
                f"UPDATE memory_assets SET {column}=?, updated_at=? WHERE id=?",
                (_dumps(values), updated_at, asset_id),
            )

    async def mark_reviewed(
        self,
        asset_id: str,
        *,
        approved_by: str,
        reviewed_at: str | None = None,
    ) -> MemoryAsset:
        """Record who approved an asset and when; part of the promotion gate."""
        cursor = await self._conn.execute(
            "UPDATE memory_assets SET approved_by=?, reviewed_at=?, updated_at=? "
            "WHERE id=?",
            (approved_by, reviewed_at or _now(), _now(), asset_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"Memory asset '{asset_id}' not found")
        if self._autocommit:
            await self._conn.commit()
        loaded = await self.get_asset(asset_id)
        assert loaded is not None
        return loaded

    async def set_visibility(self, asset_id: str, visibility: str) -> MemoryAsset:
        if visibility not in ASSET_VISIBILITIES:
            raise ValueError(f"Unsupported memory asset visibility: {visibility}")
        return await self._set_field(asset_id, "visibility", visibility)

    async def set_visibility_for_wiki(
        self, asset_id: str, wiki_id: str, visibility: str
    ) -> MemoryAsset:
        if visibility not in ASSET_VISIBILITIES:
            raise ValueError(f"Unsupported memory asset visibility: {visibility}")
        return await self._set_field(
            asset_id, "visibility", visibility, wiki_id=wiki_id
        )

    async def set_scope(self, asset_id: str, scope: str) -> MemoryAsset:
        if not scope.strip():
            raise ValueError("Memory asset scope must be non-empty")
        return await self._set_field(asset_id, "scope", scope)

    async def set_scope_for_wiki(
        self, asset_id: str, wiki_id: str, scope: str
    ) -> MemoryAsset:
        if not scope.strip():
            raise ValueError("Memory asset scope must be non-empty")
        return await self._set_field(asset_id, "scope", scope, wiki_id=wiki_id)

    async def _set_field(
        self,
        asset_id: str,
        column: str,
        value: str,
        *,
        wiki_id: str | None = None,
    ) -> MemoryAsset:
        where = "id=?" if wiki_id is None else "id=? AND wiki_id=?"
        params: tuple[Any, ...] = (
            (value, _now(), asset_id)
            if wiki_id is None
            else (value, _now(), asset_id, wiki_id)
        )
        cursor = await self._conn.execute(
            f"UPDATE memory_assets SET {column}=?, updated_at=? WHERE {where}",
            params,
        )
        if cursor.rowcount == 0:
            raise KeyError(f"Memory asset '{asset_id}' not found")
        if column == "status":
            # Keep the current version snapshot's governance state in sync.
            await self._conn.execute(
                "UPDATE memory_asset_versions SET status=? "
                "WHERE asset_id=? AND version=(SELECT version FROM memory_assets WHERE id=?)",
                (value, asset_id, asset_id),
            )
        if self._autocommit:
            await self._conn.commit()
        loaded = (
            await self.get_asset(asset_id)
            if wiki_id is None
            else await self.get_asset_for_wiki(asset_id, wiki_id)
        )
        assert loaded is not None
        return loaded

    async def delete_asset(self, asset_id: str) -> bool:
        await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                "DELETE FROM memory_asset_bindings WHERE asset_id=?", (asset_id,)
            )
            await self._conn.execute(
                "DELETE FROM memory_asset_versions WHERE asset_id=?", (asset_id,)
            )
            cursor = await self._conn.execute(
                "DELETE FROM memory_assets WHERE id=?", (asset_id,)
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return cursor.rowcount > 0

    async def list_versions(self, asset_id: str) -> list[MemoryAssetVersion]:
        async with self._conn.execute(
            "SELECT * FROM memory_asset_versions WHERE asset_id=? ORDER BY version DESC",
            (asset_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_version(row) for row in rows]

    # ── Agents and bindings ───────────────────────────────────────────────

    async def upsert_agent(
        self, *, agent_key: str, wiki_id: str, name: str, description: str = ""
    ) -> AgentProfile:
        now = _now()
        await self._conn.execute(
            """
            INSERT INTO memory_agents (agent_key, wiki_id, name, description, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(wiki_id, agent_key) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (agent_key, wiki_id, name, description, now, now),
        )
        await self._conn.commit()
        agent = await self.get_agent(agent_key, wiki_id)
        assert agent is not None
        return agent

    async def get_agent(self, agent_key: str, wiki_id: str) -> AgentProfile | None:
        async with self._conn.execute(
            "SELECT * FROM memory_agents WHERE wiki_id=? AND agent_key=?",
            (wiki_id, agent_key),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_agent(row) if row else None

    async def list_agents(self, wiki_id: str) -> list[AgentProfile]:
        async with self._conn.execute(
            "SELECT * FROM memory_agents WHERE wiki_id=? ORDER BY agent_key ASC",
            (wiki_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_agent(row) for row in rows]

    async def delete_agent(self, agent_key: str, wiki_id: str) -> bool:
        await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                "DELETE FROM memory_asset_bindings WHERE wiki_id=? AND agent_key=?",
                (wiki_id, agent_key),
            )
            cursor = await self._conn.execute(
                "DELETE FROM memory_agents WHERE wiki_id=? AND agent_key=?",
                (wiki_id, agent_key),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return cursor.rowcount > 0

    async def bind_asset(
        self,
        *,
        agent_key: str,
        wiki_id: str,
        asset_id: str,
        mode: str = "always",
        priority: int = 100,
    ) -> AssetBinding:
        if mode not in BINDING_MODES:
            raise ValueError(f"Unsupported binding mode: {mode}")
        if await self.get_agent(agent_key, wiki_id) is None:
            raise KeyError(f"Agent '{agent_key}' not found")
        if await self.get_asset(asset_id) is None:
            raise KeyError(f"Memory asset '{asset_id}' not found")
        await self._conn.execute(
            """
            INSERT INTO memory_asset_bindings (wiki_id, agent_key, asset_id, mode, priority, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(wiki_id, agent_key, asset_id) DO UPDATE SET
                mode=excluded.mode,
                priority=excluded.priority
            """,
            (wiki_id, agent_key, asset_id, mode, priority, _now()),
        )
        await self._conn.commit()
        return AssetBinding(
            agent_key=agent_key, asset_id=asset_id, mode=mode, priority=priority
        )

    async def unbind_asset(self, *, agent_key: str, wiki_id: str, asset_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM memory_asset_bindings WHERE wiki_id=? AND agent_key=? AND asset_id=?",
            (wiki_id, agent_key, asset_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def list_bindings(self, *, agent_key: str, wiki_id: str) -> list[AssetBinding]:
        async with self._conn.execute(
            "SELECT agent_key, asset_id, mode, priority FROM memory_asset_bindings "
            "WHERE wiki_id=? AND agent_key=? ORDER BY priority ASC, asset_id ASC",
            (wiki_id, agent_key),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            AssetBinding(
                agent_key=row["agent_key"],
                asset_id=row["asset_id"],
                mode=row["mode"],
                priority=row["priority"],
            )
            for row in rows
        ]


def _row_to_asset(row: aiosqlite.Row) -> MemoryAsset:
    return MemoryAsset(
        id=row["id"],
        wiki_id=row["wiki_id"],
        asset_type=row["asset_type"],
        layer=row["layer"],
        name=row["name"],
        owner=row["owner"],
        scope=row["scope"],
        status=row["status"],
        visibility=row["visibility"],
        version=row["version"],
        page_slug=row["page_slug"],
        summary=row["summary"],
        body=row["body"],
        tags=json.loads(row["tags"]),
        metadata=json.loads(row["metadata"]),
        citations=[Citation(**c) for c in json.loads(row["citations"])],
        approved_by=row["approved_by"],
        reviewed_at=row["reviewed_at"],
        supersedes=json.loads(row["supersedes"]),
        superseded_by=json.loads(row["superseded_by"]),
        conflict_lineage=json.loads(row["conflict_lineage"]),
        retired_at=row["retired_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_scope(row: aiosqlite.Row) -> MemoryScope:
    return MemoryScope(
        id=row["id"],
        wiki_id=row["wiki_id"],
        scope_type=row["scope_type"],
        name=row["name"],
        parent_scope_id=row["parent_scope_id"],
        budget_tokens=row["budget_tokens"],
        budget_items=row["budget_items"],
        retention_policy=json.loads(row["retention_policy"]),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def _row_to_version(row: aiosqlite.Row) -> MemoryAssetVersion:
    return MemoryAssetVersion(
        asset_id=row["asset_id"],
        version=row["version"],
        name=row["name"],
        summary=row["summary"],
        body=row["body"],
        status=row["status"],
        metadata=json.loads(row["metadata"]),
        citations=[Citation(**c) for c in json.loads(row["citations"])],
        change_note=row["change_note"],
        created_at=row["created_at"],
    )


def _row_to_agent(row: aiosqlite.Row) -> AgentProfile:
    return AgentProfile(
        agent_key=row["agent_key"],
        wiki_id=row["wiki_id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
