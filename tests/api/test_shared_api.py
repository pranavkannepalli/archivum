"""Recipient-side sharing: /api/shared/*, plus the isolation that backs it."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from archivum.auth import create_access_token, create_recipient_token
from archivum.config import get_settings
from archivum.knowledge.suggestions import SuggestionRepository, init_suggestion_schema
from archivum.sharing.repository import init_sharing_schema

PAGES_DDL = """
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id     TEXT    NOT NULL DEFAULT 'default',
    slug        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    authored_by TEXT    NOT NULL DEFAULT 'agent',
    UNIQUE(wiki_id, slug)
);
"""

PAGES = [
    ("work/notes", "Work notes", "The quarterly plan."),
    ("work/secret", "Secret", "Not for sharing yet."),
    ("solo", "Solo page", "Standalone."),
]


@pytest.fixture
def shared_env(tmp_path):
    """An app whose sharing and page reads hit a real throwaway database."""
    db_path = tmp_path / "shared.db"

    async def _prepare():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(PAGES_DDL)
            await init_sharing_schema(conn)
            await init_suggestion_schema(conn)
            for slug, title, content in PAGES:
                await conn.execute(
                    "INSERT INTO pages (slug, title, content) VALUES (?, ?, ?)",
                    (slug, title, content),
                )
            await conn.commit()

    asyncio.run(_prepare())

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    settings = get_settings()
    owner_token = create_access_token("owner", "owner", "default", settings)

    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        patch("archivum.db.sqlite.get_db", fake_get_db),
    ):
        from archivum.main import create_app

        app = create_app()
        owner = TestClient(app, raise_server_exceptions=True)
        owner.headers.update({"Authorization": f"Bearer {owner_token}"})
        yield {"app": app, "owner": owner, "db_path": db_path}


def _share_with_person(owner, resource_urn, role="viewer", name="Alice"):
    created = owner.post("/api/sharing/principals", json={"display_name": name}).json()
    grant = owner.post(
        "/api/sharing/grants",
        json={
            "principal_id": created["principal"]["id"],
            "resource_urn": resource_urn,
            "role": role,
        },
    ).json()["grant"]
    return created, grant


def _recipient_client(app, principal_id):
    """A claimed recipient's browser: session cookie plus the CSRF double-submit."""
    client = TestClient(app, raise_server_exceptions=True)
    session = create_recipient_token(principal_id, "default", get_settings())
    client.cookies.set("share_session", session)
    client.cookies.set("csrf_token", "csrf-value")
    client.headers.update({"X-CSRF-Token": "csrf-value"})
    return client


# ── Claiming ──────────────────────────────────────────────────────────────────

def test_claiming_a_share_returns_the_person_and_sets_a_session(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.post(
        "/api/shared/claim", json={"claim_token": created["claim_token"]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Alice"
    assert "share_session" in response.cookies


def test_an_unknown_claim_token_is_a_flat_404(shared_env):
    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.post("/api/shared/claim", json={"claim_token": "nope-not-real"})
    assert response.status_code == 404


def test_a_claim_token_only_works_once(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")
    anon = TestClient(shared_env["app"], raise_server_exceptions=True)

    first = anon.post("/api/shared/claim", json={"claim_token": created["claim_token"]})
    second = anon.post("/api/shared/claim", json={"claim_token": created["claim_token"]})

    assert first.status_code == 200
    assert second.status_code == 404


# ── Reading ───────────────────────────────────────────────────────────────────

def test_a_recipient_can_open_what_was_shared_with_them(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.get("/api/shared/resource", params={"urn": "entry:default:solo"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Solo page"
    assert body["body"] == "Standalone."
    assert body["may_comment"] is False


def test_a_recipient_cannot_open_what_was_not_shared(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.get(
        "/api/shared/resource", params={"urn": "entry:default:work/secret"}
    )
    assert response.status_code == 404


def test_a_held_page_is_indistinguishable_from_one_that_does_not_exist(shared_env):
    owner = shared_env["owner"]
    created, grant = _share_with_person(owner, "folder:default:work")
    owner.post(
        "/api/sharing/holds",
        json={"grant_id": grant["id"], "resource_urn": "entry:default:work/secret"},
    )
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    held = client.get(
        "/api/shared/resource", params={"urn": "entry:default:work/secret"}
    )
    absent = client.get(
        "/api/shared/resource", params={"urn": "entry:default:work/no-such-page"}
    )

    assert held.status_code == absent.status_code == 404
    assert held.json() == absent.json()


def test_a_shared_folder_lists_its_children_but_omits_held_ones(shared_env):
    owner = shared_env["owner"]
    created, grant = _share_with_person(owner, "folder:default:work")
    owner.post(
        "/api/sharing/holds",
        json={"grant_id": grant["id"], "resource_urn": "entry:default:work/secret"},
    )
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.get(
        "/api/shared/resource", params={"urn": "folder:default:work"}
    )
    assert response.status_code == 200, response.text
    titles = [child["title"] for child in response.json()["children"]]
    assert titles == ["Work notes"]


def test_an_inherited_entry_reports_where_its_access_came_from(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "folder:default:work")
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    body = client.get(
        "/api/shared/resource", params={"urn": "entry:default:work/notes"}
    ).json()
    assert body["shared_by_inheritance"] == "folder:default:work"


def test_listing_shows_everything_shared_with_the_caller(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    listed = client.get("/api/shared").json()
    assert [item["urn"] for item in listed] == ["entry:default:solo"]
    assert listed[0]["title"] == "Solo page"


def test_reading_without_any_identity_is_rejected(shared_env):
    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get("/api/shared/resource", params={"urn": "entry:default:solo"})
    assert response.status_code == 401


# ── Link tokens ───────────────────────────────────────────────────────────────

def test_a_link_token_opens_the_resource_without_a_session(shared_env):
    created = shared_env["owner"].post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:solo"},
    ).json()

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get(
        "/api/shared/resource",
        params={"urn": "entry:default:solo", "token": created["share_token"]},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Solo page"


def test_a_link_token_does_not_open_anything_else(shared_env):
    created = shared_env["owner"].post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:solo"},
    ).json()

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get(
        "/api/shared/resource",
        params={"urn": "entry:default:work/secret", "token": created["share_token"]},
    )
    assert response.status_code == 404


def test_a_link_can_be_opened_from_the_token_alone(shared_env):
    # The holder of /share/{token} knows the token, not the urn behind it.
    created = shared_env["owner"].post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:solo"},
    ).json()

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get(f"/api/shared/by-token/{created['share_token']}")
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Solo page"


def test_opening_an_unknown_token_is_a_404(shared_env):
    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    assert anon.get("/api/shared/by-token/not-a-real-token").status_code == 404


def test_a_migrated_legacy_link_opens_through_the_new_path(shared_env):
    """A URL handed out before any of this existed still resolves."""
    from archivum.sharing.migration import migrate_share_links

    async def _seed():
        async with aiosqlite.connect(shared_env["db_path"]) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS share_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wiki_id TEXT NOT NULL DEFAULT 'default',
                    token TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL DEFAULT 'page',
                    target_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    expires_at TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            await conn.execute(
                "INSERT INTO share_links (token, type, target_id) VALUES (?, 'page', ?)",
                ("legacy-token-abc", "solo"),
            )
            await conn.commit()
            await migrate_share_links(conn)

    asyncio.run(_seed())

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get("/api/shared/by-token/legacy-token-abc")
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Solo page"


def test_a_revoked_link_stops_working(shared_env):
    owner = shared_env["owner"]
    created = owner.post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:solo"},
    ).json()
    owner.delete(f"/api/sharing/grants/{created['grant']['id']}")

    anon = TestClient(shared_env["app"], raise_server_exceptions=True)
    response = anon.get(
        "/api/shared/resource",
        params={"urn": "entry:default:solo", "token": created["share_token"]},
    )
    assert response.status_code == 404


# ── Commenting ────────────────────────────────────────────────────────────────

def test_a_commenter_files_a_pending_suggestion_rather_than_writing(shared_env):
    created, _ = _share_with_person(
        shared_env["owner"], "entry:default:solo", role="commenter"
    )
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.post(
        "/api/shared/comment",
        json={"urn": "entry:default:solo", "text": "Should this say Q3?"},
    )
    assert response.status_code == 201, response.text

    async def _read():
        async with aiosqlite.connect(shared_env["db_path"]) as conn:
            conn.row_factory = aiosqlite.Row
            return await SuggestionRepository(conn).list_suggestions(
                target_id="page:default:solo"
            )

    suggestions = asyncio.run(_read())
    assert len(suggestions) == 1
    assert suggestions[0].status == "pending"
    assert suggestions[0].proposed_markdown == "Should this say Q3?"
    assert suggestions[0].author_principal_id == created["principal"]["id"]

    # The page itself is untouched — a comment is a proposal, not a write.
    page = client.get(
        "/api/shared/resource", params={"urn": "entry:default:solo"}
    ).json()
    assert page["body"] == "Standalone."


def test_a_comment_without_the_csrf_double_submit_is_rejected(shared_env):
    created, _ = _share_with_person(
        shared_env["owner"], "entry:default:solo", role="commenter"
    )
    client = TestClient(shared_env["app"], raise_server_exceptions=True)
    client.cookies.set(
        "share_session",
        create_recipient_token(created["principal"]["id"], "default", get_settings()),
    )

    response = client.post(
        "/api/shared/comment",
        json={"urn": "entry:default:solo", "text": "no csrf header"},
    )
    assert response.status_code == 403


def test_claiming_hands_back_a_csrf_token_so_the_first_comment_works(shared_env):
    created, _ = _share_with_person(
        shared_env["owner"], "entry:default:solo", role="commenter"
    )
    client = TestClient(shared_env["app"], raise_server_exceptions=True)
    claimed = client.post(
        "/api/shared/claim", json={"claim_token": created["claim_token"]}
    )
    assert claimed.status_code == 200

    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/api/shared/comment",
        json={"urn": "entry:default:solo", "text": "first thing I say"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text


def test_a_viewer_cannot_comment(shared_env):
    created, _ = _share_with_person(shared_env["owner"], "entry:default:solo")
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.post(
        "/api/shared/comment",
        json={"urn": "entry:default:solo", "text": "Let me in"},
    )
    assert response.status_code == 403


def test_commenting_on_something_unshared_is_a_404(shared_env):
    created, _ = _share_with_person(
        shared_env["owner"], "entry:default:solo", role="commenter"
    )
    client = _recipient_client(shared_env["app"], created["principal"]["id"])

    response = client.post(
        "/api/shared/comment",
        json={"urn": "entry:default:work/secret", "text": "hi"},
    )
    assert response.status_code == 404


# ── Isolation: the invariant the whole design rests on ────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "/api/entries",
        "/api/pages",
        "/api/sharing/principals",
        "/api/memory/assets",
        "/api/folders",
        "/api/search?q=test",
        "/api/graph",
        "/api/activity",
    ],
)
def test_a_recipient_token_is_refused_by_every_owner_route(shared_env, path):
    session = create_recipient_token("prn_whoever", "default", get_settings())

    as_bearer = TestClient(shared_env["app"], raise_server_exceptions=True)
    as_bearer.headers.update({"Authorization": f"Bearer {session}"})
    assert as_bearer.get(path).status_code in {401, 403}

    as_cookie = TestClient(shared_env["app"], raise_server_exceptions=True)
    as_cookie.cookies.set("access_token", session)
    assert as_cookie.get(path).status_code in {401, 403}


def test_an_owner_token_is_not_a_share_session(shared_env):
    """The isolation runs both ways: an owner cookie is not a recipient identity."""
    owner_token = create_access_token("owner", "owner", "default", get_settings())
    client = TestClient(shared_env["app"], raise_server_exceptions=True)
    client.cookies.set("share_session", owner_token)

    response = client.get("/api/shared/resource", params={"urn": "entry:default:solo"})
    assert response.status_code == 401
