from __future__ import annotations

import asyncio
import contextlib
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from starlette.testclient import TestClient

from archivum.config import Settings
from archivum.mcp import server


@asynccontextmanager
async def _serve_app(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


def test_sse_transport_rejects_missing_bearer_token():
    app = server.create_mcp(Settings(mcp_api_key="valid-token")).sse_app(mount_path="/")

    response = TestClient(app).get("/sse")

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_sse_message_post_rejects_missing_bearer_token():
    app = server.create_mcp(Settings(mcp_api_key="valid-token")).sse_app(mount_path="/")

    response = TestClient(app).post("/messages/", json={})

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_sse_transport_allows_valid_configured_bearer_token_to_list_tools():
    app = server.create_mcp(Settings(mcp_api_key="valid-token")).sse_app(mount_path="/")

    async with _serve_app(app) as base_url:
        async with sse_client(
            f"{base_url}/sse",
            headers={"Authorization": "Bearer valid-token"},
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.list_tools()
                tool_response = await session.call_tool("dispatch_command", {"command": "help"})

    assert "list_pages" in {tool.name for tool in response.tools}
    assert not tool_response.isError


def test_sse_rejects_missing_bearer_even_with_no_api_key_configured():
    """An empty MCP_API_KEY used to mean 'serve the vault to anyone'.

    docker-compose defaults MCP_API_KEY to empty, so the shipped default was an
    unauthenticated vault on any host where the port was reachable.
    """
    app = server.create_mcp(Settings(mcp_api_key="")).sse_app(mount_path="/")

    response = TestClient(app).get("/sse")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_device_key_authenticates_over_sse(tmp_path, monkeypatch):
    import aiosqlite

    from archivum.devices.repository import DeviceRepository
    from archivum.devices.schema import init_devices_schema

    db_path = tmp_path / "devices.db"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await init_devices_schema(conn)
        _, raw_key = await DeviceRepository(conn).mint("test laptop")

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(server.sqlite, "get_db", fake_get_db)

    app = server.create_mcp(Settings(mcp_api_key="")).sse_app(mount_path="/")

    async with _serve_app(app) as base_url:
        async with sse_client(
            f"{base_url}/sse", headers={"Authorization": f"Bearer {raw_key}"}
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.list_tools()

    assert "list_pages" in {tool.name for tool in response.tools}


def test_stdio_transport_does_not_require_a_bearer(monkeypatch):
    # A configured key is what triggered the old bug: the old _require_key
    # demanded a bearer whenever settings.mcp_api_key was set, even over
    # stdio, where no bearer can ever be supplied. Without this, the test
    # would pass against the old buggy code too, since mcp_api_key defaults
    # to "".
    monkeypatch.setattr(server.settings, "mcp_api_key", "some-key")
    server.set_transport("stdio")
    try:
        server._require_key()  # must not raise
    finally:
        server.set_transport("http")


def test_sse_rejects_an_unknown_bearer(tmp_path, monkeypatch):
    import aiosqlite

    from archivum.devices.schema import init_devices_schema

    db_path = tmp_path / "devices.db"

    async def _init() -> None:
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await init_devices_schema(conn)

    asyncio.run(_init())

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(server.sqlite, "get_db", fake_get_db)

    app = server.create_mcp(Settings(mcp_api_key="")).sse_app(mount_path="/")

    response = TestClient(app).get(
        "/sse", headers={"Authorization": "Bearer wrong-key"}
    )

    assert response.status_code == 401


def test_sse_rejects_a_revoked_device_key(tmp_path, monkeypatch):
    import aiosqlite

    from archivum.devices.repository import DeviceRepository
    from archivum.devices.schema import init_devices_schema

    db_path = tmp_path / "devices.db"

    async def _init() -> str:
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await init_devices_schema(conn)
            device, raw_key = await DeviceRepository(conn).mint("test laptop")
            await DeviceRepository(conn).revoke(device["id"])
            return raw_key

    raw_key = asyncio.run(_init())

    @contextlib.asynccontextmanager
    async def fake_get_db():
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(server.sqlite, "get_db", fake_get_db)

    app = server.create_mcp(Settings(mcp_api_key="")).sse_app(mount_path="/")

    response = TestClient(app).get(
        "/sse", headers={"Authorization": f"Bearer {raw_key}"}
    )

    assert response.status_code == 401
