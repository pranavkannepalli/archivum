"""Pairing and device management: /api/mcp/*."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from archivum.api.devices import SKILL_PATH
from archivum.auth import create_access_token
from archivum.config import get_settings
from archivum.devices.pairing import decode_pairing_token
from archivum.devices.schema import init_devices_schema


@pytest.fixture
def devices_client(tmp_path):
    db_path = tmp_path / "devices.db"

    async def _prepare():
        async with aiosqlite.connect(db_path) as conn:
            await init_devices_schema(conn)

    asyncio.run(_prepare())

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    settings = get_settings()
    token = create_access_token("owner", "owner", "default", settings)

    # No `with TestClient(...) as client:` here: entering the context manager
    # runs the real app lifespan (mkdir under settings.wiki_dir/db_path/..., a
    # real Kuzu graph, a real owner bootstrap) — exactly what
    # tests/api/test_sharing_api.py avoids by not entering the lifespan
    # either. We patch the same seams it does and skip the rest.
    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        patch("archivum.api.devices.sqlite.get_db", new=fake_get_db),
    ):
        from archivum.main import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        client.cookies.set("access_token", token)
        # POST/DELETE under cookie auth go through the CSRF double-submit
        # check (see _CSRFProtection in main.py); match the pattern
        # tests/api/test_shared_api.py uses for the same middleware.
        client.cookies.set("csrf_token", "csrf-value")
        client.headers.update({"X-CSRF-Token": "csrf-value"})
        yield client


def _issue(client) -> str:
    response = client.post("/api/mcp/pairing-tokens")
    assert response.status_code == 200
    return response.json()["token"]


def test_owner_can_issue_a_pairing_token(devices_client):
    body = devices_client.post("/api/mcp/pairing-tokens").json()

    assert body["token"].startswith("arch1_")
    assert body["expires_at"]


def test_issuing_requires_the_owner(devices_client):
    devices_client.cookies.clear()

    assert devices_client.post("/api/mcp/pairing-tokens").status_code == 401


def test_redeeming_returns_a_device_key_and_connection_details(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))

    response = devices_client.post(
        "/api/mcp/pairing/redeem",
        json={"secret": secret, "device_name": "work laptop"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["key"].startswith("amk_")
    assert body["device_id"].startswith("dev_")
    assert body["sse_url"].endswith("/sse")


def test_redeeming_twice_is_refused(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))
    devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "a"}
    )

    second = devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "b"}
    )

    assert second.status_code == 400


def test_listing_devices_never_returns_key_material(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))
    key = devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "laptop"}
    ).json()["key"]

    body = devices_client.get("/api/mcp/devices").json()

    assert [d["name"] for d in body["devices"]] == ["laptop"]
    assert key not in devices_client.get("/api/mcp/devices").text
    assert "key_hash" not in body["devices"][0]


def test_the_skill_endpoint_serves_the_vendored_file_unauthenticated(devices_client):
    devices_client.cookies.clear()

    response = devices_client.get("/api/mcp/skill")

    assert response.status_code == 200
    assert response.text == SKILL_PATH.read_text()


def test_owner_can_revoke_a_device(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))
    device_id = devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "laptop"}
    ).json()["device_id"]

    response = devices_client.delete(f"/api/mcp/devices/{device_id}")

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert devices_client.get("/api/mcp/devices").json()["devices"][0]["revoked_at"]


def test_revoking_a_device_in_another_wiki_is_refused(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))
    device_id = devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "laptop"}
    ).json()["device_id"]

    other_wiki_owner = create_access_token("owner", "owner", "other-wiki", get_settings())
    devices_client.cookies.set("access_token", other_wiki_owner)

    response = devices_client.delete(f"/api/mcp/devices/{device_id}")

    assert response.status_code == 404
