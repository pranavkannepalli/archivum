from __future__ import annotations

import secrets
from typing import Any

import aiosqlite

from archivum.sharing.models import hash_token

KEY_PREFIX = "amk_"


class DeviceRepository:
    """Mint, verify, and revoke the keys individual machines authenticate with."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def mint(
        self, name: str, wiki_id: str = "default"
    ) -> tuple[dict[str, Any], str]:
        """Create a device and return it with its raw key.

        The raw key is returned exactly once. Only its hash is persisted, so a
        lost key cannot be recovered — it can only be revoked and replaced.
        """
        device_id = f"dev_{secrets.token_urlsafe(12)}"
        raw_key = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
        await self.conn.execute(
            "INSERT INTO device_keys (id, wiki_id, name, key_hash) VALUES (?,?,?,?)",
            (device_id, wiki_id, name, hash_token(raw_key)),
        )
        await self.conn.commit()
        device = await self.get(device_id)
        assert device is not None
        return device, raw_key

    async def get(self, device_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM device_keys WHERE id=?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def verify(self, raw_key: str) -> dict[str, Any] | None:
        """Resolve a raw key to its active device, recording the sighting.

        Lookup is by hash equality in SQLite rather than a Python-side compare
        over every row: the stored value is a digest, so an index lookup leaks
        nothing a timing-safe scan would protect.
        """
        async with self.conn.execute(
            "SELECT * FROM device_keys WHERE key_hash=? AND revoked_at IS NULL",
            (hash_token(raw_key),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self.conn.execute(
            "UPDATE device_keys SET last_seen_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        await self.conn.commit()
        return dict(row)

    async def list_devices(self, wiki_id: str = "default") -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM device_keys WHERE wiki_id=? ORDER BY created_at DESC",
            (wiki_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def revoke(self, device_id: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE device_keys SET revoked_at=datetime('now') "
            "WHERE id=? AND revoked_at IS NULL",
            (device_id,),
        )
        await self.conn.commit()
        return cur.rowcount > 0
