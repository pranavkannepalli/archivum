"""Pairing tokens: one string that carries the host and a single-use secret."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from archivum.devices.pairing import (
    PairingError,
    PairingService,
    decode_pairing_token,
    encode_pairing_token,
)
from archivum.devices.schema import init_devices_schema


@pytest.fixture
async def conn(tmp_path):
    async with aiosqlite.connect(tmp_path / "devices.db") as conn:
        conn.row_factory = aiosqlite.Row
        await init_devices_schema(conn)
        yield conn


def test_encode_then_decode_round_trips():
    token = encode_pairing_token("https://vault.example.com", "s3cr3t")

    assert token.startswith("arch1_")
    assert decode_pairing_token(token) == ("https://vault.example.com", "s3cr3t")


def test_decode_rejects_a_token_without_the_version_prefix():
    with pytest.raises(ValueError):
        decode_pairing_token("nope")


def test_decode_rejects_a_token_whose_payload_is_not_valid():
    with pytest.raises(ValueError):
        decode_pairing_token("arch1_!!!!")


@pytest.mark.asyncio
async def test_issue_produces_a_decodable_token_carrying_the_base_url(conn):
    service = PairingService(conn)

    token, _expires_at = await service.issue("https://vault.example.com")

    base_url, secret = decode_pairing_token(token)
    assert base_url == "https://vault.example.com"
    assert secret


@pytest.mark.asyncio
async def test_redeem_mints_a_device(conn):
    service = PairingService(conn)
    token, _ = await service.issue("https://vault.example.com")
    _, secret = decode_pairing_token(token)

    device, raw_key = await service.redeem(secret, "work laptop")

    assert device["name"] == "work laptop"
    assert raw_key.startswith("amk_")


@pytest.mark.asyncio
async def test_a_token_can_only_be_redeemed_once(conn):
    service = PairingService(conn)
    token, _ = await service.issue("https://vault.example.com")
    _, secret = decode_pairing_token(token)
    await service.redeem(secret, "first")

    with pytest.raises(PairingError):
        await service.redeem(secret, "second")


@pytest.mark.asyncio
async def test_an_expired_token_is_refused(conn):
    service = PairingService(conn, ttl_seconds=-1)
    token, _ = await service.issue("https://vault.example.com")
    _, secret = decode_pairing_token(token)

    with pytest.raises(PairingError):
        await service.redeem(secret, "laptop")


@pytest.mark.asyncio
async def test_expired_and_already_redeemed_are_indistinguishable(conn):
    """A caller must not be able to tell which failure they hit.

    Distinguishable errors turn the endpoint into an oracle for whether a
    guessed secret ever existed.
    """
    live = PairingService(conn)
    token, _ = await live.issue("https://vault.example.com")
    _, used_secret = decode_pairing_token(token)
    await live.redeem(used_secret, "first")

    expired = PairingService(conn, ttl_seconds=-1)
    token2, _ = await expired.issue("https://vault.example.com")
    _, expired_secret = decode_pairing_token(token2)

    with pytest.raises(PairingError) as used_exc:
        await live.redeem(used_secret, "again")
    with pytest.raises(PairingError) as expired_exc:
        await live.redeem(expired_secret, "again")
    with pytest.raises(PairingError) as unknown_exc:
        await live.redeem("never-existed", "again")

    assert str(used_exc.value) == str(expired_exc.value) == str(unknown_exc.value)


@pytest.mark.asyncio
async def test_concurrent_redeems_of_same_token_only_succeed_once(conn):
    """Test that concurrent redeems of the same token are serialized correctly.

    Even with concurrent calls, only one should succeed; the second should fail
    with PairingError and its minted device should be revoked. This tests the
    TOCTOU race fix where both calls might mint devices before either UPDATE
    completes.
    """
    service = PairingService(conn)
    token, _ = await service.issue("https://vault.example.com")
    _, secret = decode_pairing_token(token)

    success_count = 0
    failure_count = 0

    async def attempt_redeem(name):
        nonlocal success_count, failure_count
        try:
            device, key = await service.redeem(secret, name)
            success_count += 1
        except PairingError:
            failure_count += 1

    # Run both redeems concurrently
    await asyncio.gather(
        attempt_redeem("device1"),
        attempt_redeem("device2"),
    )

    # Exactly one should succeed
    assert success_count == 1
    assert failure_count == 1

    # Verify that both devices were minted, but one was revoked
    async with conn.execute(
        "SELECT * FROM device_keys ORDER BY created_at"
    ) as cur:
        devices = await cur.fetchall()

    assert len(devices) == 2, "Both concurrent mints should succeed"
    revoked = [d for d in devices if d["revoked_at"] is not None]
    assert len(revoked) == 1, "Exactly one device should be revoked (the loser's)"
