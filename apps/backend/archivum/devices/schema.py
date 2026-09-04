from __future__ import annotations

import aiosqlite

# One row per linked client on one machine. `key_hash` rather than the key for
# the same reason `refresh_tokens` and share links store hashes: a leaked
# database should not hand over working access to every machine ever paired.
SCHEMA = """
CREATE TABLE IF NOT EXISTS device_keys (
    id           TEXT PRIMARY KEY,
    wiki_id      TEXT NOT NULL DEFAULT 'default',
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_keys_hash ON device_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_device_keys_wiki ON device_keys(wiki_id);

CREATE TABLE IF NOT EXISTS pairing_tokens (
    id          TEXT PRIMARY KEY,
    wiki_id     TEXT NOT NULL DEFAULT 'default',
    secret_hash TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    redeemed_at TEXT,
    device_id   TEXT REFERENCES device_keys(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_pairing_tokens_secret ON pairing_tokens(secret_hash);
"""


async def init_devices_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
