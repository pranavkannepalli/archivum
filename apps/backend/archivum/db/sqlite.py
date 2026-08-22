"""aiosqlite wrapper — schema init and CRUD for all tables."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import aiosqlite

from archivum.config import Settings, get_settings
from archivum.knowledge.suggestions import init_suggestion_schema
from archivum.memory.registry import init_memory_schema
from archivum.sharing.migration import migrate_share_links
from archivum.sharing.repository import init_sharing_schema
from archivum.store.schema import init_evidence_schema

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    username    TEXT    NOT NULL UNIQUE,
    password_hash TEXT  NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'viewer',  -- owner|collaborator|viewer
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    slug        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    authored_by TEXT    NOT NULL DEFAULT 'agent', -- user|agent
    UNIQUE(wiki_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_pages_wiki ON pages(wiki_id);
CREATE INDEX IF NOT EXISTS idx_pages_slug ON pages(slug);

CREATE TABLE IF NOT EXISTS page_write_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id       TEXT    NOT NULL DEFAULT 'default',
    slug          TEXT,
    title         TEXT    NOT NULL,
    content       TEXT    NOT NULL DEFAULT '',
    tags          TEXT    NOT NULL DEFAULT '[]',
    authored_by   TEXT    NOT NULL DEFAULT 'agent',
    status        TEXT    NOT NULL DEFAULT 'pending', -- pending|processing|done|error
    result_slug   TEXT,
    error         TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_page_write_jobs_status ON page_write_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    path        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(wiki_id, path)
);

CREATE INDEX IF NOT EXISTS idx_folders_wiki ON folders(wiki_id);
CREATE INDEX IF NOT EXISTS idx_folders_path ON folders(path);

-- FTS5 virtual table for keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    slug,
    title,
    content,
    tags,
    content='pages',
    content_rowid='id'
);

-- Keep FTS in sync
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, slug, title, content, tags)
    VALUES (new.id, new.slug, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, slug, title, content, tags)
    VALUES ('delete', old.id, old.slug, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, slug, title, content, tags)
    VALUES ('delete', old.id, old.slug, old.title, old.content, old.tags);
    INSERT INTO pages_fts(rowid, slug, title, content, tags)
    VALUES (new.id, new.slug, new.title, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS share_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    token       TEXT    NOT NULL UNIQUE,
    type        TEXT    NOT NULL DEFAULT 'page',  -- page|query
    target_id   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS distill_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    source_id   TEXT    NOT NULL,
    -- A page queues by slug and a captured source by id; the worker routes on
    -- this rather than guessing from the identifier's shape.
    kind        TEXT    NOT NULL DEFAULT 'source'
                CHECK (kind IN ('source', 'page')),
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'error')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(wiki_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_distill_queue_status
    ON distill_queue(status, id);

CREATE TABLE IF NOT EXISTS ingest_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id         TEXT    NOT NULL DEFAULT 'default',
    source_type     TEXT    NOT NULL,  -- file|url|batch
    source_path     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|error
    pages_created   INTEGER NOT NULL DEFAULT 0,
    pages_updated   INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT    NOT NULL UNIQUE,
    expires_at  TEXT    NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash ON refresh_tokens(token_hash);

CREATE TABLE IF NOT EXISTS invite_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id    TEXT    NOT NULL DEFAULT 'default',
    token      TEXT    NOT NULL UNIQUE,
    role       TEXT    NOT NULL DEFAULT 'viewer',  -- collaborator|viewer
    created_by TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS code_repos (
    scope        TEXT PRIMARY KEY,
    wiki_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    last_sha     TEXT,
    files        INTEGER NOT NULL DEFAULT 0,
    nodes        INTEGER NOT NULL DEFAULT 0,
    edges        INTEGER NOT NULL DEFAULT 0,
    pages        INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    indexed_at   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_code_repos_wiki ON code_repos(wiki_id);

CREATE TABLE IF NOT EXISTS agent_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT NOT NULL DEFAULT 'default',
    actor       TEXT NOT NULL DEFAULT 'agent',
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT,
    summary     TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_activity_created ON agent_activity(wiki_id, created_at);
"""


# ── Connection factory ────────────────────────────────────────────────────────

_db_path: Path | None = None


def configure(settings: Settings) -> None:
    global _db_path
    _db_path = settings.db_path
    _db_path.parent.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    settings = get_settings()
    path = _db_path or settings.db_path
    async with aiosqlite.connect(
        str(path), timeout=settings.sqlite_busy_timeout_seconds
    ) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        # Set explicitly as well as via the driver: waiting is the behaviour
        # this depends on, and it should not rest on a default that a driver
        # upgrade could change.
        await conn.execute(
            f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_seconds * 1000)}"
        )
        yield conn


# ── Schema init ───────────────────────────────────────────────────────────────

async def init_db(settings: Settings) -> None:
    configure(settings)
    async with get_db() as db:
        await db.executescript(_SCHEMA)
        await init_evidence_schema(db)
        # init_memory_schema, not a bare MEMORY_SCHEMA executescript: the
        # script only creates missing tables, while the columns added since the
        # first release (conflict_lineage, supersedes, approved_by, ...) arrive
        # through ALTER TABLE. A database created before those columns existed
        # would otherwise run without them and fail at query time.
        await init_memory_schema(db)
        await init_suggestion_schema(db)
        await init_sharing_schema(db)
        # Legacy share links become grants so a URL handed out before this
        # existed keeps resolving. Idempotent, so it is safe on every boot.
        await migrate_share_links(db)
        await db.commit()


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_user_by_username(username: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(
    username: str,
    password_hash: str,
    role: str = "viewer",
    wiki_id: str = "default",
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO users (wiki_id, username, password_hash, role) VALUES (?,?,?,?)",
            (wiki_id, username, password_hash, role),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def ensure_owner_exists(username: str, password_hash: str) -> None:
    """Create the owner account if it doesn't already exist."""
    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM users WHERE role='owner' LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row:
            return
        await db.execute(
            "INSERT INTO users (wiki_id, username, password_hash, role) VALUES ('default',?,?,'owner')",
            (username, password_hash),
        )
        await db.commit()


# ── Pages ─────────────────────────────────────────────────────────────────────

async def list_pages(wiki_id: str = "default") -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, wiki_id, slug, title, tags, created_at, updated_at, authored_by "
            "FROM pages WHERE wiki_id=? ORDER BY updated_at DESC",
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def list_recent_pages(
    wiki_id: str = "default",
    limit: int = 50,
    before_inclusive: str | None = None,
    before_slug: str | None = None,
    exclude_tied: bool = False,
) -> list[dict[str, Any]]:
    """Pages ordered by last touch, newest first, for the activity stream.

    `before_inclusive` is an ISO timestamp cursor. It is deliberately inclusive:
    the caller orders by (timestamp, id) and needs rows tied with the cursor
    timestamp so it can compare the full key. Filtering them out here would
    silently drop every page sharing that second.
    """
    clauses = ["wiki_id=?"]
    params: list[Any] = [wiki_id]
    if before_inclusive:
        # Row-value comparison: rows tied on the timestamp are resolved by slug,
        # which matches the order the caller merges on.
        if before_slug:
            clauses.append("(updated_at < ? OR (updated_at = ? AND slug < ?))")
            params.extend([before_inclusive, before_inclusive, before_slug])
        elif exclude_tied:
            clauses.append("updated_at < ?")
            params.append(before_inclusive)
        else:
            clauses.append("updated_at <= ?")
            params.append(before_inclusive)
    params.append(max(limit, 0))
    async with get_db() as db:
        async with db.execute(
            "SELECT id, wiki_id, slug, title, tags, created_at, updated_at, authored_by "
            f"FROM pages WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC, slug DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_page(slug: str, wiki_id: str = "default") -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM pages WHERE slug=? AND wiki_id=?",
            (slug, wiki_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_pages(
    slugs: list[str],
    wiki_id: str = "default",
) -> list[dict[str, Any]]:
    """Batch-get pages by slug to avoid N+1 query patterns.

    Returns results in the same order as `slugs` (deduplicated, first occurrence wins).
    """

    if not slugs:
        return []

    # Deduplicate while preserving order.
    ordered_slugs: list[str] = []
    seen: set[str] = set()
    for s in slugs:
        if s not in seen:
            seen.add(s)
            ordered_slugs.append(s)

    placeholders = ",".join(["?" for _ in ordered_slugs])
    sql = (
        f"SELECT id, slug, title, content, tags, created_at, updated_at, authored_by "
        f"FROM pages WHERE wiki_id=? AND slug IN ({placeholders})"
    )

    async with get_db() as db:
        async with db.execute(sql, (wiki_id, *ordered_slugs)) as cur:
            rows = await cur.fetchall()

    by_slug: dict[str, dict[str, Any]] = {r["slug"]: dict(r) for r in rows}
    return [by_slug[s] for s in ordered_slugs if s in by_slug]


async def upsert_page(
    slug: str,
    title: str,
    content: str,
    tags: list[str],
    authored_by: str = "agent",
    wiki_id: str = "default",
) -> tuple[int, bool]:
    """Return (id, created) where created=True means a new row was inserted."""
    tags_json = json.dumps(tags)
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM pages WHERE slug=? AND wiki_id=?", (slug, wiki_id)
        ) as cur:
            row = await cur.fetchone()

        if row:
            await db.execute(
                "UPDATE pages SET title=?, content=?, tags=?, updated_at=?, authored_by=? "
                "WHERE slug=? AND wiki_id=?",
                (title, content, tags_json, now, authored_by, slug, wiki_id),
            )
            await db.commit()
            return row["id"], False
        else:
            # Write the timestamps explicitly rather than leaning on the column
            # defaults: SQLite's datetime('now') is space-separated and naive,
            # while every UPDATE writes ISO-8601 with a timezone. Mixing the two
            # in one column breaks lexicographic ordering, which the activity
            # stream depends on.
            cur = await db.execute(
                "INSERT INTO pages "
                "(wiki_id, slug, title, content, tags, authored_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (wiki_id, slug, title, content, tags_json, authored_by, now, now),
            )
            await db.commit()
            return cur.lastrowid, True  # type: ignore[return-value]


async def enqueue_page_write_job(
    *,
    title: str,
    content: str,
    tags: list[str],
    authored_by: str = "agent",
    wiki_id: str = "default",
    slug: str | None = None,
) -> int:
    now = datetime.now(UTC).isoformat()
    tags_json = json.dumps(tags)
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO page_write_jobs (wiki_id, slug, title, content, tags, authored_by, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
            (wiki_id, slug, title, content, tags_json, authored_by, now, now),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def get_page_write_job(job_id: int, wiki_id: str = "default") -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM page_write_jobs WHERE id=? AND wiki_id=?",
            (job_id, wiki_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def claim_next_page_write_job() -> dict[str, Any] | None:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT id FROM page_write_jobs WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await db.commit()
                return None

            job_id = row["id"]
            await db.execute(
                "UPDATE page_write_jobs SET status='processing', started_at=?, updated_at=?, error=NULL "
                "WHERE id=?",
                (now, now, job_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM page_write_jobs WHERE id=?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def complete_page_write_job(job_id: int, result_slug: str, wiki_id: str = "default") -> None:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE page_write_jobs SET status='done', result_slug=?, error=NULL, finished_at=?, updated_at=? "
            "WHERE id=? AND wiki_id=?",
            (result_slug, now, now, job_id, wiki_id),
        )
        await db.commit()


async def fail_page_write_job(job_id: int, error: str, wiki_id: str = "default") -> None:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE page_write_jobs SET status='error', error=?, finished_at=?, updated_at=? "
            "WHERE id=? AND wiki_id=?",
            (error, now, now, job_id, wiki_id),
        )
        await db.commit()


async def delete_page(slug: str, wiki_id: str = "default") -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM pages WHERE slug=? AND wiki_id=?", (slug, wiki_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def update_page_slug(old_slug: str, new_slug: str, wiki_id: str = "default") -> bool:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE pages SET slug=?, updated_at=? WHERE slug=? AND wiki_id=?",
            (new_slug, now, old_slug, wiki_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_pages_under(prefix: str, wiki_id: str = "default") -> list[dict[str, Any]]:
    like_prefix = f"{prefix}/%"
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM pages WHERE wiki_id=? AND slug LIKE ? ORDER BY slug ASC",
            (wiki_id, like_prefix),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_pages_under(prefix: str, wiki_id: str = "default") -> list[dict[str, Any]]:
    pages = await list_pages_under(prefix, wiki_id)
    if not pages:
        return []
    like_prefix = f"{prefix}/%"
    async with get_db() as db:
        await db.execute(
            "DELETE FROM pages WHERE wiki_id=? AND slug LIKE ?",
            (wiki_id, like_prefix),
        )
        await db.commit()
    return pages


async def update_share_targets(mapping: dict[str, str], wiki_id: str = "default") -> None:
    if not mapping:
        return
    async with get_db() as db:
        for old, new in mapping.items():
            await db.execute(
                "UPDATE share_links SET target_id=? WHERE wiki_id=? AND type='page' AND target_id=?",
                (new, wiki_id, old),
            )
        await db.commit()


# ── Folders ───────────────────────────────────────────────────────────────────

async def list_folders(wiki_id: str = "default") -> list[dict[str, Any]]:
    """The folders in this vault.

    Reading used to create the default folders as a side effect, which meant a
    folder you deleted reappeared the next time anything listed them — you
    could not actually remove one. Seeding now happens once at startup, in
    `vault_scaffold`.
    """
    async with get_db() as db:
        async with db.execute(
            "SELECT path, name, created_at, updated_at FROM folders WHERE wiki_id=? ORDER BY path ASC",
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_folder(path: str, wiki_id: str = "default") -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT path, name, created_at, updated_at FROM folders WHERE path=? AND wiki_id=?",
            (path, wiki_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_folder(path: str, wiki_id: str = "default") -> dict[str, Any]:
    name = path.split("/")[-1]
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO folders (wiki_id, path, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (wiki_id, path, name, now, now),
        )
        await db.commit()
    folder = await get_folder(path, wiki_id)
    return folder or {"path": path, "name": name, "created_at": now, "updated_at": now}


async def upsert_folder(path: str, wiki_id: str = "default") -> dict[str, Any]:
    existing = await get_folder(path, wiki_id)
    if existing:
        return existing
    return await create_folder(path, wiki_id)


async def list_folders_under(path: str, wiki_id: str = "default") -> list[dict[str, Any]]:
    like_prefix = f"{path}/%"
    async with get_db() as db:
        async with db.execute(
            "SELECT path, name, created_at, updated_at FROM folders "
            "WHERE wiki_id=? AND (path=? OR path LIKE ?) ORDER BY path ASC",
            (wiki_id, path, like_prefix),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def move_folder_paths(old_path: str, new_path: str, wiki_id: str = "default") -> int:
    rows = await list_folders_under(old_path, wiki_id)
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        for row in sorted(rows, key=lambda r: len(r["path"])):
            suffix = row["path"][len(old_path):]
            path = f"{new_path}{suffix}"
            await db.execute(
                "UPDATE folders SET path=?, name=?, updated_at=? WHERE wiki_id=? AND path=?",
                (path, path.split("/")[-1], now, wiki_id, row["path"]),
            )
        await db.commit()
    return len(rows)


async def delete_folder(path: str, wiki_id: str = "default") -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM folders WHERE wiki_id=? AND path=?",
            (wiki_id, path),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_folders_under(path: str, wiki_id: str = "default") -> int:
    like_prefix = f"{path}/%"
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM folders WHERE wiki_id=? AND (path=? OR path LIKE ?)",
            (wiki_id, path, like_prefix),
        )
        await db.commit()
        return cur.rowcount


async def search_pages_fts(
    query: str, wiki_id: str = "default", limit: int = 10
) -> list[dict[str, Any]]:
    """Full-text search using FTS5. Returns rows with a snippet."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT p.slug, p.title, p.tags,
                   snippet(pages_fts, 2, '<mark>', '</mark>', '…', 20) AS excerpt,
                   rank
            FROM pages_fts
            JOIN pages p ON p.id = pages_fts.rowid
            WHERE pages_fts MATCH ? AND p.wiki_id = ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, wiki_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Share links ───────────────────────────────────────────────────────────────

async def create_share_link(
    token: str,
    link_type: str,
    target_id: str | None,
    expires_at: str | None,
    wiki_id: str = "default",
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO share_links (wiki_id, token, type, target_id, expires_at) "
            "VALUES (?,?,?,?,?)",
            (wiki_id, token, link_type, target_id, expires_at),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def get_share_link(token: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM share_links WHERE token=? AND revoked=0",
            (token,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None

            data = dict(row)
            expires_at = data.get("expires_at")
            if expires_at:
                try:
                    exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=UTC)
                    if exp <= datetime.now(UTC):
                        return None
                except Exception:
                    # If we can't parse expires_at, fall back to serving it.
                    pass

            return data


async def revoke_share_link(token: str) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE share_links SET revoked=1 WHERE token=?", (token,)
        )
        await db.commit()
        return cur.rowcount > 0


async def list_share_links(wiki_id: str = "default") -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM share_links WHERE wiki_id=? ORDER BY created_at DESC",
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Ingest log ────────────────────────────────────────────────────────────────

async def create_ingest_log(
    source_type: str,
    source_path: str,
    wiki_id: str = "default",
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO ingest_log (wiki_id, source_type, source_path, status) VALUES (?,?,?,'running')",
            (wiki_id, source_type, source_path),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def update_ingest_log(
    log_id: int,
    status: str,
    pages_created: int = 0,
    pages_updated: int = 0,
    error: str | None = None,
) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE ingest_log SET status=?, pages_created=?, pages_updated=?, error=? WHERE id=?",
            (status, pages_created, pages_updated, error, log_id),
        )
        await db.commit()


async def list_ingest_logs(
    wiki_id: str = "default",
    limit: int = 50,
    before_inclusive: str | None = None,
    before_id: str | None = None,
    exclude_tied: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["wiki_id=?"]
    params: list[Any] = [wiki_id]
    if before_inclusive:
        if before_id:
            # CAST: the activity id embeds this row id as text, so the feed
            # compares "ingest:9" against "ingest:10" as strings. An integer
            # comparison here would order them the other way round.
            clauses.append(
                "(created_at < ? OR (created_at = ? AND CAST(id AS TEXT) < ?))"
            )
            params.extend([before_inclusive, before_inclusive, before_id])
        elif exclude_tied:
            clauses.append("created_at < ?")
            params.append(before_inclusive)
        else:
            clauses.append("created_at <= ?")
            params.append(before_inclusive)
    params.append(limit)
    async with get_db() as db:
        async with db.execute(
            f"SELECT * FROM ingest_log WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, CAST(id AS TEXT) DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Refresh tokens ────────────────────────────────────────────────────────────

async def store_refresh_token(
    user_id: int,
    token_hash: str,
    expires_at: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES (?,?,?)",
            (user_id, token_hash, expires_at),
        )
        await db.commit()


async def get_refresh_token(token_hash: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT rt.*, u.username, u.role, u.wiki_id FROM refresh_tokens rt "
            "JOIN users u ON u.id = rt.user_id "
            "WHERE rt.token_hash=? AND rt.revoked=0",
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def revoke_refresh_token(token_hash: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?", (token_hash,)
        )
        await db.commit()


async def revoke_all_refresh_tokens(user_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (user_id,)
        )
        await db.commit()


# ── Invite tokens ─────────────────────────────────────────────────────────────

async def create_invite_token(
    wiki_id: str,
    token: str,
    role: str,
    created_by: str,
    expires_at: str | None,
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO invite_tokens (wiki_id, token, role, created_by, expires_at) VALUES (?,?,?,?,?)",
            (wiki_id, token, role, created_by, expires_at),
        )
        await db.commit()


async def get_invite_token(token: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM invite_tokens WHERE token=?", (token,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def mark_invite_used(token: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE invite_tokens SET used=1 WHERE token=?", (token,)
        )
        await db.commit()


async def list_invite_tokens(wiki_id: str = "default") -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM invite_tokens WHERE wiki_id=? ORDER BY created_at DESC",
            (wiki_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Distillation queue ───────────────────────────────────────────────────────

async def enqueue_distillation(
    source_id: str, wiki_id: str = "default", kind: str = "source"
) -> None:
    """Mark a captured source as needing distillation.

    Idempotent: re-capturing the same source resets it to pending rather than
    queueing it twice, so a retry is just another enqueue.
    """
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO distill_queue (wiki_id, source_id, kind, status, created_at, updated_at)
            VALUES (?,?,?, 'pending', ?, ?)
            ON CONFLICT(wiki_id, source_id) DO UPDATE SET
                status='pending', error=NULL, updated_at=excluded.updated_at
            """,
            (wiki_id, source_id, kind, now, now),
        )
        await db.commit()


async def claim_distillation() -> dict[str, Any] | None:
    """Take the oldest pending job, marking it running so it is claimed once."""
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM distill_queue WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await db.execute(
            "UPDATE distill_queue SET status='running', attempts=attempts+1, updated_at=? "
            "WHERE id=? AND status='pending'",
            (now, row["id"]),
        )
        await db.commit()
        return dict(row)


async def finish_distillation(
    job_id: int, *, status: str, error: str | None = None
) -> None:
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE distill_queue SET status=?, error=?, updated_at=? WHERE id=?",
            (status, error, now, job_id),
        )
        await db.commit()


async def list_distillation_jobs(
    wiki_id: str = "default", limit: int = 50
) -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM distill_queue WHERE wiki_id=? ORDER BY id DESC LIMIT ?",
            (wiki_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
