"""Owner-side sharing management: /api/sharing/*."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from archivum.auth import create_access_token
from archivum.config import get_settings
from archivum.sharing.repository import init_sharing_schema


@pytest.fixture
def sharing_client(tmp_path):
    """A TestClient whose sharing routes talk to a real throwaway SQLite file.

    A file rather than `:memory:` because the app runs requests on its own event
    loop; a file-backed database lets each request open its own connection and
    still see the same rows.
    """
    db_path = tmp_path / "sharing.db"

    async def _prepare():
        async with aiosqlite.connect(db_path) as conn:
            await init_sharing_schema(conn)

    asyncio.run(_prepare())

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    settings = get_settings()
    token = create_access_token("owner", "owner", "default", settings)

    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        patch("archivum.db.sqlite.get_db", fake_get_db),
    ):
        from archivum.main import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client


def _principal(client, name="Alice"):
    response = client.post("/api/sharing/principals", json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()


# ── Principals ────────────────────────────────────────────────────────────────


def test_creating_a_principal_returns_a_one_time_claim_url(sharing_client):
    body = _principal(sharing_client)
    assert body["principal"]["display_name"] == "Alice"
    assert body["principal"]["claimed_at"] is None
    assert body["claim_url"].startswith("/claim/")
    assert len(body["claim_token"]) > 20


def test_the_claim_token_is_not_returned_when_listing_principals(sharing_client):
    _principal(sharing_client)
    response = sharing_client.get("/api/sharing/principals")
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 1
    assert "claim_token" not in listed[0]
    assert "claim_hash" not in listed[0]


def test_a_principal_needs_a_name(sharing_client):
    response = sharing_client.post("/api/sharing/principals", json={"display_name": "  "})
    assert response.status_code == 400


def test_revoking_a_principal_removes_it_from_the_list(sharing_client):
    body = _principal(sharing_client)
    response = sharing_client.delete(f"/api/sharing/principals/{body['principal']['id']}")
    assert response.status_code == 200
    assert sharing_client.get("/api/sharing/principals").json() == []


# ── Grants ────────────────────────────────────────────────────────────────────


def test_granting_a_principal_access_to_an_entry(sharing_client):
    principal = _principal(sharing_client)["principal"]
    response = sharing_client.post(
        "/api/sharing/grants",
        json={
            "principal_id": principal["id"],
            "resource_urn": "entry:default:people/alice",
            "role": "commenter",
        },
    )
    assert response.status_code == 201, response.text
    grant = response.json()["grant"]
    assert grant["role"] == "commenter"
    assert grant["subject_kind"] == "principal"
    assert response.json()["share_token"] is None


def test_creating_a_link_grant_returns_its_token_once(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:alice"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["share_token"]
    assert body["share_url"] == f"/share/{body['share_token']}"

    # Listing must never hand the token back out.
    listed = sharing_client.get(
        "/api/sharing/grants", params={"resource_urn": "entry:default:alice"}
    ).json()
    assert len(listed) == 1
    assert body["share_token"] not in str(listed)


def test_a_resource_can_be_named_by_kind_and_id_without_a_wiki(sharing_client):
    # The browser should not need to know its own tenant id to share its page.
    response = sharing_client.post(
        "/api/sharing/grants",
        json={
            "subject_kind": "link",
            "resource_kind": "entry",
            "resource_id": "people/alice",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["grant"]["resource_urn"] == "entry:default:people/alice"

    listed = sharing_client.get(
        "/api/sharing/grants",
        params={"resource_kind": "entry", "resource_id": "people/alice"},
    ).json()
    assert len(listed) == 1


def test_naming_no_resource_at_all_is_rejected(sharing_client):
    response = sharing_client.post("/api/sharing/grants", json={"subject_kind": "link"})
    assert response.status_code == 400


def test_a_traversal_in_the_resource_id_is_rejected(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={
            "subject_kind": "link",
            "resource_kind": "entry",
            "resource_id": "../../etc/passwd",
        },
    )
    assert response.status_code == 400


def test_a_malformed_resource_urn_is_rejected(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:default:../etc/passwd"},
    )
    assert response.status_code == 400


def test_a_grant_cannot_be_written_against_another_wiki(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={"subject_kind": "link", "resource_urn": "entry:other:alice"},
    )
    assert response.status_code == 400


def test_an_unknown_role_is_rejected(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={
            "subject_kind": "link",
            "resource_urn": "entry:default:alice",
            "role": "editor",
        },
    )
    assert response.status_code == 400


def test_granting_to_an_unknown_principal_is_rejected(sharing_client):
    response = sharing_client.post(
        "/api/sharing/grants",
        json={"principal_id": "prn_nope", "resource_urn": "entry:default:alice"},
    )
    assert response.status_code == 404


def test_listing_grants_for_a_resource_names_who_can_see_it(sharing_client):
    principal = _principal(sharing_client)["principal"]
    sharing_client.post(
        "/api/sharing/grants",
        json={"principal_id": principal["id"], "resource_urn": "entry:default:alice"},
    )
    listed = sharing_client.get(
        "/api/sharing/grants", params={"resource_urn": "entry:default:alice"}
    ).json()
    assert listed[0]["display_name"] == "Alice"


def test_revoking_a_grant_drops_it_from_the_resource_listing(sharing_client):
    principal = _principal(sharing_client)["principal"]
    grant = sharing_client.post(
        "/api/sharing/grants",
        json={"principal_id": principal["id"], "resource_urn": "entry:default:alice"},
    ).json()["grant"]

    assert sharing_client.delete(f"/api/sharing/grants/{grant['id']}").status_code == 200
    listed = sharing_client.get(
        "/api/sharing/grants", params={"resource_urn": "entry:default:alice"}
    ).json()
    assert listed == []


# ── Holds ─────────────────────────────────────────────────────────────────────


def test_a_hold_is_listed_with_the_person_it_is_withheld_from(sharing_client):
    principal = _principal(sharing_client)["principal"]
    grant = sharing_client.post(
        "/api/sharing/grants",
        json={"principal_id": principal["id"], "resource_urn": "folder:default:work"},
    ).json()["grant"]

    held = sharing_client.post(
        "/api/sharing/holds",
        json={"grant_id": grant["id"], "resource_urn": "entry:default:work/secret"},
    )
    assert held.status_code == 201, held.text

    holds = sharing_client.get("/api/sharing/holds").json()
    assert len(holds) == 1
    assert holds[0]["display_name"] == "Alice"
    assert holds[0]["resource_urn"] == "entry:default:work/secret"


def test_approving_a_hold_releases_it(sharing_client):
    principal = _principal(sharing_client)["principal"]
    grant = sharing_client.post(
        "/api/sharing/grants",
        json={"principal_id": principal["id"], "resource_urn": "folder:default:work"},
    ).json()["grant"]
    sharing_client.post(
        "/api/sharing/holds",
        json={"grant_id": grant["id"], "resource_urn": "entry:default:work/secret"},
    )

    response = sharing_client.post(
        f"/api/sharing/holds/{grant['id']}/approve",
        json={"resource_urn": "entry:default:work/secret"},
    )
    assert response.status_code == 200
    assert sharing_client.get("/api/sharing/holds").json() == []


def test_approving_a_hold_that_does_not_exist_is_a_404(sharing_client):
    response = sharing_client.post(
        "/api/sharing/holds/grt_nope/approve",
        json={"resource_urn": "entry:default:work/secret"},
    )
    assert response.status_code == 404


# ── Authorisation ─────────────────────────────────────────────────────────────


def test_sharing_management_requires_write_access(sharing_client):
    settings = get_settings()
    viewer = create_access_token("v", "viewer", "default", settings)
    response = sharing_client.get(
        "/api/sharing/principals", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert response.status_code == 403


def test_sharing_management_rejects_anonymous_callers(sharing_client):
    response = sharing_client.get(
        "/api/sharing/principals", headers={"Authorization": ""}
    )
    assert response.status_code == 401
