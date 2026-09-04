"""Per-device MCP keys: minting, verification, revocation."""

from __future__ import annotations

import aiosqlite
import pytest

from archivum.devices.repository import DeviceRepository
from archivum.devices.schema import init_devices_schema


@pytest.fixture
async def repo(tmp_path):
    db_path = tmp_path / "devices.db"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await init_devices_schema(conn)
        yield DeviceRepository(conn)


@pytest.mark.asyncio
async def test_mint_returns_a_prefixed_key_and_a_device_row(repo):
    device, raw_key = await repo.mint("work laptop / Claude Code")

    assert device["id"].startswith("dev_")
    assert raw_key.startswith("amk_")
    assert device["name"] == "work laptop / Claude Code"
    assert device["revoked_at"] is None


@pytest.mark.asyncio
async def test_the_raw_key_is_never_stored(repo):
    _, raw_key = await repo.mint("laptop")

    async with repo.conn.execute("SELECT key_hash FROM device_keys") as cur:
        row = await cur.fetchone()

    assert raw_key not in row["key_hash"]


@pytest.mark.asyncio
async def test_verify_accepts_a_minted_key(repo):
    device, raw_key = await repo.mint("laptop")

    verified = await repo.verify(raw_key)

    assert verified is not None
    assert verified["id"] == device["id"]


@pytest.mark.asyncio
async def test_verify_rejects_an_unknown_key(repo):
    await repo.mint("laptop")

    assert await repo.verify("amk_nope") is None


@pytest.mark.asyncio
async def test_verify_rejects_a_revoked_key(repo):
    device, raw_key = await repo.mint("laptop")

    assert await repo.revoke(device["id"]) is True
    assert await repo.verify(raw_key) is None


@pytest.mark.asyncio
async def test_revoking_an_unknown_device_reports_failure(repo):
    assert await repo.revoke("dev_missing") is False


@pytest.mark.asyncio
async def test_verify_advances_last_seen(repo):
    device, raw_key = await repo.mint("laptop")
    assert device["last_seen_at"] is None

    await repo.verify(raw_key)

    refreshed = (await repo.list_devices())[0]
    assert refreshed["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_list_devices_is_scoped_by_wiki(repo):
    await repo.mint("a", wiki_id="default")
    await repo.mint("b", wiki_id="other")

    names = [d["name"] for d in await repo.list_devices("default")]

    assert names == ["a"]
