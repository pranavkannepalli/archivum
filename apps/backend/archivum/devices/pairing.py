from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from archivum.devices.repository import DeviceRepository
from archivum.sharing.models import hash_token

TOKEN_PREFIX = "arch1_"
DEFAULT_TTL_SECONDS = 900  # 15 minutes

# One message for every failure mode. Telling a caller *why* redemption failed
# would say whether a guessed secret was ever real.
_REFUSED = "Pairing token is not valid. Issue a new one from Settings."


class PairingError(Exception):
    """A pairing token was unknown, expired, or already redeemed."""


def encode_pairing_token(base_url: str, secret: str) -> str:
    """Pack the server URL and secret into one string the user copies.

    The URL travels inside the token so `archivum connect` needs no --host flag
    and no .env — the whole point is that the second machine has neither.
    """
    payload = json.dumps({"u": base_url, "s": secret}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{TOKEN_PREFIX}{encoded}"


def decode_pairing_token(token: str) -> tuple[str, str]:
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("Not an Archivum pairing token")
    encoded = token[len(TOKEN_PREFIX):]
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        return payload["u"], payload["s"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError("Malformed pairing token") from exc


class PairingService:
    def __init__(
        self, conn: aiosqlite.Connection, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self.conn = conn
        self.ttl_seconds = ttl_seconds
        self.devices = DeviceRepository(conn)

    async def issue(
        self, base_url: str, wiki_id: str = "default"
    ) -> tuple[str, str]:
        secret = secrets.token_urlsafe(24)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        ).isoformat()
        await self.conn.execute(
            "INSERT INTO pairing_tokens (id, wiki_id, secret_hash, expires_at) "
            "VALUES (?,?,?,?)",
            (f"pair_{secrets.token_urlsafe(12)}", wiki_id, hash_token(secret), expires_at),
        )
        await self.conn.commit()
        return encode_pairing_token(base_url, secret), expires_at

    async def redeem(
        self, secret: str, device_name: str
    ) -> tuple[dict[str, Any], str]:
        async with self.conn.execute(
            "SELECT * FROM pairing_tokens WHERE secret_hash=? AND redeemed_at IS NULL",
            (hash_token(secret),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise PairingError(_REFUSED)
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise PairingError(_REFUSED)

        device, raw_key = await self.devices.mint(device_name, wiki_id=row["wiki_id"])
        await self.conn.execute(
            "UPDATE pairing_tokens SET redeemed_at=datetime('now'), device_id=? "
            "WHERE id=? AND redeemed_at IS NULL",
            (device["id"], row["id"]),
        )
        await self.conn.commit()
        return device, raw_key
