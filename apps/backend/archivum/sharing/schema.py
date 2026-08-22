"""Sharing schema. Applied idempotently at init, like the other stores."""

from __future__ import annotations

SHARING_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_principals (
    id            TEXT    PRIMARY KEY,
    wiki_id       TEXT    NOT NULL DEFAULT 'default',
    display_name  TEXT    NOT NULL,
    -- Cleared once claimed: a claim token is single-use, so keeping it would
    -- leave a second working credential for an already-claimed principal.
    claim_hash    TEXT,
    claimed_at    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_share_principals_wiki
    ON share_principals(wiki_id, revoked);
CREATE INDEX IF NOT EXISTS idx_share_principals_claim
    ON share_principals(claim_hash);

CREATE TABLE IF NOT EXISTS share_grants (
    id            TEXT    PRIMARY KEY,
    wiki_id       TEXT    NOT NULL DEFAULT 'default',
    subject_kind  TEXT    NOT NULL
                  CHECK (subject_kind IN ('principal', 'link')),
    -- A principal id, or the sha256 of a link token. Never a raw token.
    subject_id    TEXT    NOT NULL,
    resource_urn  TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'viewer'
                  CHECK (role IN ('viewer', 'commenter')),
    include_cited INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(subject_kind, subject_id, resource_urn)
);

CREATE INDEX IF NOT EXISTS idx_share_grants_subject
    ON share_grants(subject_kind, subject_id, revoked);
CREATE INDEX IF NOT EXISTS idx_share_grants_resource
    ON share_grants(wiki_id, resource_urn, revoked);

CREATE TABLE IF NOT EXISTS share_holds (
    grant_id      TEXT    NOT NULL,
    resource_urn  TEXT    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT 'agent_authored',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (grant_id, resource_urn)
);

CREATE INDEX IF NOT EXISTS idx_share_holds_resource
    ON share_holds(resource_urn);

-- A shared view: either a frozen question/answer snapshot (what the legacy
-- `type='query'` share link was) or a saved live query. Storing the payload
-- here rather than in the grant keeps `resource_urn` a pure address — the old
-- `share_links.target_id` held a slug for one type and a JSON blob for the
-- other, and that overloading is what made the table impossible to extend.
CREATE TABLE IF NOT EXISTS share_views (
    id          TEXT    PRIMARY KEY,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    kind        TEXT    NOT NULL DEFAULT 'query_snapshot'
                CHECK (kind IN ('query_snapshot', 'live_query')),
    title       TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_share_views_wiki ON share_views(wiki_id);
"""
