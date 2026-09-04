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

        # base_url is loopback on purpose: with no API_PUBLIC_URL set, the
        # redeem endpoint reads the request's own base to decide whether the
        # localhost SSE fallback could possibly reach this vault. The default
        # `http://testserver` would read as a remote install and (correctly)
        # refuse to hand out a localhost SSE URL.
        client = TestClient(
            create_app(), base_url="http://localhost", raise_server_exceptions=True
        )
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

    # Locks Ruling 1: sse_url must resolve from the MCP server's own base
    # (port 8001 by default), not the API's — the two are different services,
    # and `.endswith("/sse")` alone would still pass if this regressed to
    # reusing the API's base URL like the brief's original `_base_url` did.
    settings = get_settings()
    expected_sse = (
        settings.mcp_public_url.strip() or f"http://localhost:{settings.mcp_port}/sse"
    )
    assert body["sse_url"] == expected_sse

    # skill_url is an API route, so it must point at the API's own base
    # (the test client's base URL), not the MCP port.
    assert body["skill_url"] == f"{devices_client.base_url}/api/mcp/skill"


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


def _redeem_key(client) -> tuple[str, str]:
    """Issue and redeem a pairing token, returning (device_id, raw_key)."""
    _, secret = decode_pairing_token(_issue(client))
    body = client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "laptop"}
    ).json()
    return body["device_id"], body["key"]


def test_a_device_can_read_its_own_record_with_its_own_key(devices_client):
    device_id, key = _redeem_key(devices_client)
    devices_client.cookies.clear()

    response = devices_client.get(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200
    assert response.json()["id"] == device_id
    assert "key_hash" not in response.json()


def test_an_unknown_device_key_is_refused(devices_client):
    devices_client.cookies.clear()

    response = devices_client.get(
        "/api/mcp/devices/self", headers={"Authorization": "Bearer amk_not-a-real-key"}
    )

    assert response.status_code == 401


def test_a_device_can_revoke_itself(devices_client):
    _, key = _redeem_key(devices_client)
    devices_client.cookies.clear()

    response = devices_client.delete(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200
    assert response.json()["revoked"] is True


def test_a_revoked_device_key_no_longer_authenticates(devices_client):
    _, key = _redeem_key(devices_client)
    devices_client.cookies.clear()
    devices_client.delete(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    response = devices_client.get(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 401


def test_self_revoke_on_an_already_revoked_key_reports_not_revoked(devices_client):
    _, key = _redeem_key(devices_client)
    devices_client.cookies.clear()
    devices_client.delete(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    # The key no longer verifies once revoked, so a second self-revoke with
    # the same key must fail authentication rather than report success.
    response = devices_client.delete(
        "/api/mcp/devices/self", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 401


def test_an_owner_session_without_a_device_key_cannot_reach_self_routes(devices_client):
    # devices_client carries an owner cookie by default and no Authorization
    # header — a device key is the only credential require_device accepts.
    response = devices_client.get("/api/mcp/devices/self")

    assert response.status_code == 401


def _settings_override(client, **updates):
    """Point the app at a copy of settings for one request.

    The fixture builds the app without a `get_settings` override, so pairing
    reads process settings; these tests need to vary the two public-URL
    settings that decide whether an SSE URL is reachable at all.
    """
    settings = get_settings().model_copy(update=updates)
    client.app.dependency_overrides[get_settings] = lambda: settings
    return settings


def test_redeem_refuses_a_loopback_sse_url_when_the_api_is_remote(devices_client):
    """A remote install with no MCP_PUBLIC_URL must not hand out localhost.

    The redeemed URL is written straight into three client configs on the
    machine being linked, where `http://localhost:8001/sse` means that
    machine's own port 8001 — a vault that does not exist.
    """
    _, secret = decode_pairing_token(_issue(devices_client))
    _settings_override(
        devices_client, api_public_url="https://vault.example.com", mcp_public_url=""
    )

    try:
        response = devices_client.post(
            "/api/mcp/pairing/redeem",
            json={"secret": secret, "device_name": "work laptop"},
        )
    finally:
        devices_client.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "mcp_url_unresolved"
    assert "MCP_PUBLIC_URL" in response.json()["detail"]["detail"]


def test_a_refused_redeem_leaves_the_pairing_token_unspent(devices_client):
    """Config errors must not cost the user their one-shot token."""
    _, secret = decode_pairing_token(_issue(devices_client))
    _settings_override(
        devices_client, api_public_url="https://vault.example.com", mcp_public_url=""
    )
    try:
        devices_client.post(
            "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "a"}
        )
    finally:
        devices_client.app.dependency_overrides.clear()

    _settings_override(
        devices_client,
        api_public_url="https://vault.example.com",
        mcp_public_url="https://mcp.example.com/sse",
    )
    try:
        retry = devices_client.post(
            "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "a"}
        )
    finally:
        devices_client.app.dependency_overrides.clear()

    assert retry.status_code == 200
    assert retry.json()["sse_url"] == "https://mcp.example.com/sse"


def test_redeem_refuses_an_explicitly_loopback_mcp_url_when_the_api_is_remote(
    devices_client,
):
    """Setting MCP_PUBLIC_URL to localhost is the same failure, spelled out."""
    _, secret = decode_pairing_token(_issue(devices_client))
    _settings_override(
        devices_client,
        api_public_url="https://vault.example.com",
        mcp_public_url="http://localhost:8001/sse",
    )

    try:
        response = devices_client.post(
            "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "a"}
        )
    finally:
        devices_client.app.dependency_overrides.clear()

    assert response.status_code == 503


def test_a_localhost_install_still_redeems_to_a_localhost_sse_url(devices_client):
    """The single-machine case is untouched: both sides are loopback."""
    _, secret = decode_pairing_token(_issue(devices_client))
    settings = _settings_override(
        devices_client, api_public_url="http://localhost:8000", mcp_public_url=""
    )

    try:
        response = devices_client.post(
            "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "a"}
        )
    finally:
        devices_client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["sse_url"] == f"http://localhost:{settings.mcp_port}/sse"
