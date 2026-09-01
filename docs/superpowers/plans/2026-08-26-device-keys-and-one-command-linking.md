# Device Keys and One-Command Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer link a new machine's coding agents to an existing Archivum vault with one command, using a revocable per-device key instead of a shared secret, and close the hole where MCP over HTTP serves the vault unauthenticated.

**Architecture:** A new `archivum.devices` package owns two tables — `device_keys` (long-lived, hashed, revocable) and `pairing_tokens` (single-use, 15-minute, hashed). Settings issues a pairing token encoding the server URL plus a one-time secret; `archivum connect <token>` redeems it from any machine with no checkout, receives a device key, and writes MCP config for Claude Code, Cursor, and Codex directly. MCP bearer verification changes from one constant-time compare against `MCP_API_KEY` to a lookup that accepts the legacy key *or* any active device key, and HTTP transport stops treating an empty key as "no auth required".

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pytest; Node 20+, `node:test`, no runtime dependencies in the CLI.

**Spec:** `docs/superpowers/specs/2026-08-26-coding-agent-memory-positioning-design.md`

## Global Constraints

- Backend is Python 3.12 with `from __future__ import annotations` at the top of every module.
- SQLite access goes through `aiosqlite`. Never `sqlite3` directly.
- Bearer credentials are stored hashed. Reuse `archivum.sharing.models.hash_token` (SHA-256 hex); do not write a second hasher.
- ID prefixes follow the existing convention (`prn_`, `grt_`): devices are `dev_<token_urlsafe(12)>`, pairing rows are `pair_<token_urlsafe(12)>`.
- Secret material uses `secrets.token_urlsafe(32)` and constant-time comparison via `hmac.compare_digest`.
- CLI is ESM (`"type": "module"`), Node 20+, **zero runtime dependencies** — `node:` builtins only. It is published as `@pranavkannepalli/archivum` with bin `archivum`.
- `archivum connect` must not call `ensureRoot()` or read `.env`. That assumption is exactly what makes the existing `archivum mcp config` unusable on a second machine.
- Owner-only REST routes depend on `require_owner` from `archivum.auth`.
- Pairing token lifetime: **15 minutes**, single use.
- Public docs must not claim Archivum has lower recall latency than local markdown. The claim is *less context, same memory everywhere*.

Verification commands for every task (from `AGENTS.md:43-50`):

```bash
npm test --workspace apps/frontend
npm run build --workspace apps/frontend
npm test --workspace packages/archivum-cli
cd apps/backend && uv run --group dev pytest ../../tests -q
```

Run only the relevant subset per task; run all four before the final commit.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `apps/backend/archivum/devices/__init__.py` | Package marker, re-exports |
| `apps/backend/archivum/devices/schema.py` | DDL for `device_keys` and `pairing_tokens`, `init_devices_schema` |
| `apps/backend/archivum/devices/repository.py` | `DeviceRepository` — mint, verify, list, revoke device keys |
| `apps/backend/archivum/devices/pairing.py` | `PairingService` — issue, encode, decode, redeem pairing tokens |
| `apps/backend/archivum/api/devices.py` | REST routes under `/api/mcp` |
| `apps/backend/archivum/agent_skills/archivum-memory/SKILL.md` | Vendored copy the container can serve |
| `packages/archivum-cli/src/connect.js` | `archivum connect` command |
| `packages/archivum-cli/src/clients.js` | Per-client config writers (Claude Code, Cursor, Codex) |
| `apps/frontend/src/components/DevicesPanel.tsx` | Link and revoke UI |
| `tests/devices/__init__.py` | Test package marker |
| `tests/devices/test_device_repository.py` | Device key mint/verify/revoke |
| `tests/devices/test_pairing.py` | Token encode/decode, single-use, expiry |
| `tests/api/test_devices_api.py` | Route behaviour and authorization |
| `tests/api/test_agent_skill.py` | Vendored skill does not drift from the root copy |
| `packages/archivum-cli/test/clients.test.js` | Config writers |
| `packages/archivum-cli/test/connect.test.js` | Connect flow against a stub server |
| `apps/frontend/src/components/DevicesPanel.test.tsx` | Panel rendering |

**Modified:**

| File | Change |
|---|---|
| `apps/backend/archivum/config.py:184-187` | Add pairing rate-limit settings |
| `apps/backend/archivum/rate_limit.py:64-80` | Own bucket for the redeem endpoint |
| `apps/backend/archivum/mcp/server.py:60-116` | Device-key verifier, fail-closed HTTP |
| `apps/backend/archivum/main.py:238-261` | Register the devices router |
| `apps/backend/archivum/db/sqlite.py:241` | Call `init_devices_schema` from `init_db` |
| `packages/archivum-cli/src/index.js:20-27` | Dispatch `connect` |
| `packages/archivum-cli/src/util.js:6-24` | Help text, `parseOptions` value-flag list |
| `apps/frontend/src/api.ts:1523` | Device and pairing API functions |
| `apps/frontend/src/pages/SettingsPage.tsx:54,151,505` | Render the devices panel |
| `README.md`, `AGENTS.md`, `docs/architecture/agent-access.md`, `docs/architecture/deploy.md` | Positioning and linking rewrite |

---

## Task 1: Device key store

**Files:**
- Create: `apps/backend/archivum/devices/__init__.py`, `apps/backend/archivum/devices/schema.py`, `apps/backend/archivum/devices/repository.py`
- Test: `tests/devices/__init__.py`, `tests/devices/test_device_repository.py`

**Interfaces:**
- Consumes: `archivum.sharing.models.hash_token`
- Produces:
  - `init_devices_schema(conn: aiosqlite.Connection) -> None`
  - `DeviceRepository(conn: aiosqlite.Connection)`
  - `DeviceRepository.mint(name: str, wiki_id: str = "default") -> tuple[dict[str, Any], str]` — returns `(device_row, raw_key)`; the raw key is returned once and never stored
  - `DeviceRepository.verify(raw_key: str) -> dict[str, Any] | None` — returns the device row and advances `last_seen_at`; `None` when unknown or revoked
  - `DeviceRepository.list_devices(wiki_id: str = "default") -> list[dict[str, Any]]`
  - `DeviceRepository.revoke(device_id: str) -> bool`
  - Raw keys are prefixed `amk_`; device ids `dev_`

- [ ] **Step 1: Write the failing test**

Create `tests/devices/__init__.py` as an empty file, then `tests/devices/test_device_repository.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/devices/test_device_repository.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'archivum.devices'`

- [ ] **Step 3: Write the schema module**

Create `apps/backend/archivum/devices/__init__.py`:

```python
"""Per-device MCP credentials and the pairing flow that hands them out."""

from __future__ import annotations

from archivum.devices.repository import DeviceRepository
from archivum.devices.schema import init_devices_schema

__all__ = ["DeviceRepository", "init_devices_schema"]
```

Create `apps/backend/archivum/devices/schema.py`:

```python
from __future__ import annotations

import aiosqlite

# One row per linked client on one machine. `key_hash` rather than the key for
# the same reason `refresh_tokens` and share links store hashes: a leaked
# database should not hand over working access to every machine ever paired.
SCHEMA = """
CREATE TABLE IF NOT EXISTS device_keys (
    id           TEXT PRIMARY KEY,
    wiki_id      TEXT NOT NULL DEFAULT 'default',
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_keys_hash ON device_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_device_keys_wiki ON device_keys(wiki_id);

CREATE TABLE IF NOT EXISTS pairing_tokens (
    id          TEXT PRIMARY KEY,
    wiki_id     TEXT NOT NULL DEFAULT 'default',
    secret_hash TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    redeemed_at TEXT,
    device_id   TEXT REFERENCES device_keys(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_pairing_tokens_secret ON pairing_tokens(secret_hash);
"""


async def init_devices_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
```

- [ ] **Step 4: Write the repository**

Create `apps/backend/archivum/devices/repository.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/devices/test_device_repository.py -q`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add apps/backend/archivum/devices tests/devices
git commit -m "feat(devices): per-device MCP keys with revocation"
```

---

## Task 2: Pairing tokens

**Files:**
- Create: `apps/backend/archivum/devices/pairing.py`
- Test: `tests/devices/test_pairing.py`

**Interfaces:**
- Consumes: `DeviceRepository`, `init_devices_schema` from Task 1
- Produces:
  - `encode_pairing_token(base_url: str, secret: str) -> str` — returns `arch1_<base64url>`
  - `decode_pairing_token(token: str) -> tuple[str, str]` — returns `(base_url, secret)`; raises `ValueError` on a malformed token
  - `PairingService(conn: aiosqlite.Connection, *, ttl_seconds: int = 900)`
  - `PairingService.issue(base_url: str, wiki_id: str = "default") -> tuple[str, str]` — returns `(wire_token, expires_at)`
  - `PairingService.redeem(secret: str, device_name: str, wiki_id: str = "default") -> tuple[dict[str, Any], str]` — returns `(device_row, raw_key)`; raises `PairingError` when expired, unknown, or already redeemed
  - `PairingError(Exception)`

- [ ] **Step 1: Write the failing test**

Create `tests/devices/test_pairing.py`:

```python
"""Pairing tokens: one string that carries the host and a single-use secret."""

from __future__ import annotations

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/devices/test_pairing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'archivum.devices.pairing'`

- [ ] **Step 3: Write the implementation**

Create `apps/backend/archivum/devices/pairing.py`:

```python
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
        self, secret: str, device_name: str, wiki_id: str = "default"
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

        device, raw_key = await self.devices.mint(device_name, wiki_id=wiki_id)
        await self.conn.execute(
            "UPDATE pairing_tokens SET redeemed_at=datetime('now'), device_id=? "
            "WHERE id=? AND redeemed_at IS NULL",
            (device["id"], row["id"]),
        )
        await self.conn.commit()
        return device, raw_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/devices -q`
Expected: PASS, 16 tests

- [ ] **Step 5: Commit**

```bash
git add apps/backend/archivum/devices/pairing.py tests/devices/test_pairing.py
git commit -m "feat(devices): single-use pairing tokens carrying the server url"
```

---

## Task 3: MCP auth accepts device keys and fails closed over HTTP

**Files:**
- Modify: `apps/backend/archivum/mcp/server.py:60-116`, `apps/backend/archivum/mcp/server.py` `main()`
- Test: `tests/mcp_tests/test_sse_auth.py` (extend)

**Interfaces:**
- Consumes: `DeviceRepository.verify` from Task 1
- Produces:
  - `server.DeviceBearerTokenVerifier(api_key: str)` replacing `StaticBearerTokenVerifier`
  - `server.create_mcp(app_settings, *, register_existing_tools=True, transport="http")`
  - `server.set_transport(transport: str) -> None`

The behaviour change: over HTTP, auth is **always** required — a device key is a valid credential whether or not `MCP_API_KEY` is set, so "no key configured" no longer means "no auth". Over stdio the bearer check stays off, because the transport is a local pipe into a process the caller already controls.

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp_tests/test_sse_auth.py`:

```python
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


def test_stdio_transport_does_not_require_a_bearer():
    server.set_transport("stdio")
    try:
        server._require_key()  # must not raise
    finally:
        server.set_transport("http")
```

Add `import contextlib` to the file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/mcp_tests/test_sse_auth.py -q`
Expected: FAIL — the empty-key case returns 200 instead of 401, and `set_transport` does not exist.

- [ ] **Step 3: Replace the verifier**

In `apps/backend/archivum/mcp/server.py`, replace `StaticBearerTokenVerifier` (lines 60-69) with:

```python
class DeviceBearerTokenVerifier:
    """Accept the legacy shared key or any active per-device key.

    The legacy key is checked first and in constant time; device keys are
    resolved by hash lookup. Keeping the legacy key valid is what lets existing
    installs keep working while machines migrate to `archivum connect`.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._api_key and hmac.compare_digest(token, self._api_key):
            return AccessToken(token=token, client_id="archivum-legacy-key", scopes=[])
        async with sqlite.get_db() as conn:
            device = await DeviceRepository(conn).verify(token)
        if device is None:
            return None
        return AccessToken(token=token, client_id=device["id"], scopes=[])
```

Add the imports at the top of the module:

```python
from archivum.db import sqlite
from archivum.devices.repository import DeviceRepository
```

- [ ] **Step 4: Make HTTP always authenticate and teach the module its transport**

Replace `create_mcp`'s auth block (lines 72-82) with:

```python
_transport = "http"


def set_transport(transport: str) -> None:
    """Record which transport this process is serving.

    stdio is a local pipe into a container the caller can already exec into, so
    a bearer check there protects nothing. HTTP is reachable from the network
    and always authenticates.
    """
    global _transport
    _transport = transport


def create_mcp(
    app_settings: Settings,
    *,
    register_existing_tools: bool = True,
    transport: str = "http",
) -> FastMCP:
    token_verifier = None
    auth = None
    if transport == "http":
        token_verifier = DeviceBearerTokenVerifier(app_settings.mcp_api_key)
        resource_url = (
            app_settings.mcp_public_url.strip()
            or f"http://localhost:{app_settings.mcp_port}"
        )
        auth = AuthSettings(
            issuer_url=resource_url,
            resource_server_url=resource_url,
            required_scopes=[],
        )
```

Replace `_require_key` (lines 111-116) with:

```python
def _require_key() -> None:
    if _transport == "stdio":
        return
    if get_access_token() is None:
        raise PermissionError("MCP bearer authentication required")
```

The token itself was already validated by `DeviceBearerTokenVerifier` before the
request reached a tool, so re-comparing it here would only re-check what the
transport layer proved.

- [ ] **Step 5: Set the transport in `main()`**

In `main()`, add `set_transport("stdio")` immediately before `mcp.run(transport="stdio")`, and `set_transport("http")` immediately before `mcp.run(transport="sse", mount_path="/")`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/mcp_tests -q`
Expected: PASS. The pre-existing tests in this file still pass — they configure `mcp_api_key="valid-token"`, which the new verifier accepts on the legacy branch.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/archivum/mcp/server.py tests/mcp_tests/test_sse_auth.py
git commit -m "fix(mcp): require a bearer over http and accept device keys

An empty MCP_API_KEY previously disabled authentication entirely, and
docker-compose defaults it to empty. HTTP now always authenticates,
against the legacy shared key or any active device key. stdio is
unchanged: it is a local pipe into a container the caller can exec into."
```

---

## Task 4: REST routes for pairing and devices

**Files:**
- Create: `apps/backend/archivum/api/devices.py`
- Modify: `apps/backend/archivum/main.py:238-261`, `apps/backend/archivum/db/sqlite.py:241`, `apps/backend/archivum/config.py:184-187`, `apps/backend/archivum/rate_limit.py:64-80`
- Test: `tests/api/test_devices_api.py`

**Interfaces:**
- Consumes: `PairingService`, `PairingError`, `DeviceRepository` from Tasks 1-2
- Produces:
  - `POST /api/mcp/pairing-tokens` (owner) → `{"token": str, "expires_at": str}`
  - `POST /api/mcp/pairing/redeem` (unauthenticated, rate-limited) — body `{"secret": str, "device_name": str}` → `{"device_id", "key", "sse_url", "vault_name", "skill_url"}`
  - `GET /api/mcp/devices` (owner) → `{"devices": [...]}` — never includes key material
  - `DELETE /api/mcp/devices/{device_id}` (owner) → `{"revoked": bool}`
  - `GET /api/mcp/skill` (unauthenticated) → `text/markdown` body of the `archivum-memory` SKILL.md
  - `Settings.rate_limit_pairing_requests: int = 10`, `Settings.rate_limit_pairing_window_seconds: int = 600`

The server serves the skill because `connect` runs on machines with no checkout,
and because the skill describes *that server's* tools — the vault is the right
source of truth for it, not whatever version the CLI happened to ship with.
`apps/backend/Dockerfile:23` copies only `archivum/`, so root `skills/` never
reaches the container; the file is vendored into the package and a test keeps
the two copies identical.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_devices_api.py`, following the fixture shape of `tests/api/test_sharing_api.py`:

```python
"""Pairing and device management: /api/mcp/*."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

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

    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.api.devices.sqlite.get_db", new=fake_get_db),
    ):
        from archivum.main import create_app

        with TestClient(create_app()) as client:
            client.cookies.set("access_token", token)
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


def test_owner_can_revoke_a_device(devices_client):
    _, secret = decode_pairing_token(_issue(devices_client))
    device_id = devices_client.post(
        "/api/mcp/pairing/redeem", json={"secret": secret, "device_name": "laptop"}
    ).json()["device_id"]

    response = devices_client.delete(f"/api/mcp/devices/{device_id}")

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert devices_client.get("/api/mcp/devices").json()["devices"][0]["revoked_at"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/api/test_devices_api.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'archivum.api.devices'`

- [ ] **Step 3: Write the router**

Create `apps/backend/archivum/api/devices.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from archivum.auth import CurrentUser, require_owner
from archivum.config import Settings, get_settings
from archivum.db import sqlite
from archivum.devices.pairing import PairingError, PairingService
from archivum.devices.repository import DeviceRepository

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Never let a device row reach a client with its hash attached: the hash is the
# only thing standing between a leaked response body and an offline attack.
_PUBLIC_FIELDS = ("id", "name", "created_at", "last_seen_at", "revoked_at")


class RedeemRequest(BaseModel):
    secret: str = Field(min_length=1)
    device_name: str = Field(min_length=1, max_length=120)


def _public(device: dict[str, Any]) -> dict[str, Any]:
    return {field: device[field] for field in _PUBLIC_FIELDS}


def _base_url(request: Request, settings: Settings) -> str:
    configured = settings.mcp_public_url.strip()
    if configured:
        return configured.removesuffix("/sse").rstrip("/")
    return str(request.base_url).rstrip("/")


@router.post("/pairing-tokens")
async def issue_pairing_token(
    request: Request,
    current_user: CurrentUser = Depends(require_owner),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        token, expires_at = await PairingService(conn).issue(
            _base_url(request, settings), wiki_id=current_user.wiki_id
        )
    return {"token": token, "expires_at": expires_at}


@router.post("/pairing/redeem")
async def redeem_pairing_token(
    body: RedeemRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        try:
            device, raw_key = await PairingService(conn).redeem(
                body.secret, body.device_name
            )
        except PairingError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"detail": str(exc), "code": "pairing_refused"},
            ) from exc
    base = _base_url(request, settings)
    return {
        "device_id": device["id"],
        "key": raw_key,
        "sse_url": f"{base}/sse",
        "vault_name": settings.owner_username or "Archivum",
        "skill_url": f"{base}/api/mcp/skill",
    }


SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "agent_skills" / "archivum-memory" / "SKILL.md"
)


@router.get("/skill", response_class=PlainTextResponse)
async def get_agent_skill() -> str:
    """Serve the archivum-memory skill so `connect` can install it anywhere.

    Unauthenticated on purpose: the skill is public repository content that
    tells an agent which tools to reach for, and gating it would mean a machine
    could not be set up before it has a key.
    """
    if not SKILL_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "Agent skill is not bundled with this build.", "code": "skill_missing"},
        )
    return SKILL_PATH.read_text()


@router.get("/devices")
async def list_devices(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, list[dict[str, Any]]]:
    async with sqlite.get_db() as conn:
        devices = await DeviceRepository(conn).list_devices(current_user.wiki_id)
    return {"devices": [_public(d) for d in devices]}


@router.delete("/devices/{device_id}")
async def revoke_device(
    device_id: str,
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, bool]:
    async with sqlite.get_db() as conn:
        revoked = await DeviceRepository(conn).revoke(device_id)
    return {"revoked": revoked}
```

- [ ] **Step 4: Vendor the skill into the package so the container can serve it**

`apps/backend/Dockerfile:23` copies only `archivum/` into the image, and the build
context is `./apps/backend`, so root `skills/` cannot be reached from the
Dockerfile at all. Copy the skill into the package and guard against drift:

```bash
mkdir -p apps/backend/archivum/agent_skills/archivum-memory
cp skills/archivum-memory/SKILL.md apps/backend/archivum/agent_skills/archivum-memory/SKILL.md
```

Add `tests/api/test_agent_skill.py`:

```python
"""The vendored skill the API serves must match the one in the repo root."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "skills" / "archivum-memory" / "SKILL.md"
VENDORED = (
    REPO_ROOT
    / "apps" / "backend" / "archivum" / "agent_skills" / "archivum-memory" / "SKILL.md"
)


def test_the_vendored_skill_matches_the_repo_root_copy():
    """Two copies exist because the Docker build context cannot see the root one.

    Editing the root copy alone would ship a stale skill to every machine that
    runs `archivum connect`, and nothing else would notice.
    """
    assert VENDORED.read_text() == SOURCE.read_text(), (
        "skills/archivum-memory/SKILL.md changed without updating the vendored "
        "copy under apps/backend/archivum/agent_skills/. Copy it across."
    )
```

If `pyproject.toml` restricts packaged files, add `agent_skills/**/*.md` to the
package-data include list so the file survives a wheel build.

- [ ] **Step 5: Register the router and the schema**

In `apps/backend/archivum/main.py`, add `from archivum.api import devices as devices_routes` alongside the other route imports, and `app.include_router(devices_routes.router)` after line 256 (`system_router`).

In `apps/backend/archivum/db/sqlite.py`, inside `init_db` (line 241), after the existing schema setup, add:

```python
    from archivum.devices.schema import init_devices_schema

    async with get_db() as db:
        await init_devices_schema(db)
```

Import locally to avoid a circular import: `archivum.devices.repository` imports `archivum.sharing.models`, and `sqlite` is imported by nearly everything.

- [ ] **Step 6: Give redemption its own rate-limit bucket**

In `apps/backend/archivum/config.py`, after line 187, add:

```python
    rate_limit_pairing_requests: int = 10
    rate_limit_pairing_window_seconds: int = 600  # 10 minutes
```

In `apps/backend/archivum/rate_limit.py`, in `rate_limit_policy_for_path`, add this branch **before** the generic `/api/` branch:

```python
    # Its own bucket rather than the login bucket: deploy.md records that
    # scripted traffic through the Cloudflare tunnel trips the shared login
    # limiter, and pairing is exactly the scripted case.
    if path.startswith("/api/mcp/pairing/redeem"):
        return RateLimitPolicy(
            limit=settings.rate_limit_pairing_requests,
            window_seconds=settings.rate_limit_pairing_window_seconds,
        )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/backend && uv run --group dev pytest ../../tests/api/test_devices_api.py ../../tests/api/test_agent_skill.py ../../tests/devices -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add apps/backend/archivum/api/devices.py apps/backend/archivum/main.py \
  apps/backend/archivum/db/sqlite.py apps/backend/archivum/config.py \
  apps/backend/archivum/rate_limit.py apps/backend/archivum/agent_skills \
  tests/api/test_devices_api.py tests/api/test_agent_skill.py
git commit -m "feat(api): pairing and device routes under /api/mcp"
```

---

## Task 5: Client config writers

**Files:**
- Create: `packages/archivum-cli/src/clients.js`
- Test: `packages/archivum-cli/test/clients.test.js`

**Interfaces:**
- Produces:
  - `detectClients(home: string) -> string[]` — subset of `["claude", "cursor", "codex"]`
  - `writeClaudeConfig({home, sseUrl, key}) -> string` — returns the path written
  - `writeCursorConfig({home, sseUrl, key}) -> string`
  - `writeCodexConfig({home, sseUrl, key}) -> string`
  - `CLIENT_WRITERS: Record<string, (opts) => string>`

Every writer is idempotent: re-running replaces the `archivum` entry and leaves every other server untouched.

- [ ] **Step 1: Write the failing test**

Create `packages/archivum-cli/test/clients.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { detectClients, writeClaudeConfig, writeCursorConfig, writeCodexConfig } from "../src/clients.js";

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "archivum-home-"));
}

const opts = (home) => ({ home, sseUrl: "https://vault.example.com/sse", key: "amk_test" });

test("detectClients finds only the clients that are installed", () => {
  const home = tempHome();
  fs.mkdirSync(path.join(home, ".cursor"), { recursive: true });

  assert.deepEqual(detectClients(home), ["cursor"]);
});

test("writeClaudeConfig adds an archivum server with the bearer header", () => {
  const home = tempHome();

  const written = writeClaudeConfig(opts(home));
  const config = JSON.parse(fs.readFileSync(written, "utf8"));

  assert.equal(config.mcpServers.archivum.url, "https://vault.example.com/sse");
  assert.equal(config.mcpServers.archivum.headers.Authorization, "Bearer amk_test");
});

test("writeClaudeConfig preserves other servers and is idempotent", () => {
  const home = tempHome();
  const target = path.join(home, ".claude.json");
  fs.writeFileSync(target, JSON.stringify({ mcpServers: { other: { url: "http://x" } } }));

  writeClaudeConfig(opts(home));
  writeClaudeConfig(opts(home));
  const config = JSON.parse(fs.readFileSync(target, "utf8"));

  assert.equal(config.mcpServers.other.url, "http://x");
  assert.equal(Object.keys(config.mcpServers).length, 2);
});

test("writeCursorConfig writes to .cursor/mcp.json", () => {
  const home = tempHome();

  const written = writeCursorConfig(opts(home));

  assert.equal(written, path.join(home, ".cursor", "mcp.json"));
  assert.match(fs.readFileSync(written, "utf8"), /amk_test/);
});

test("writeCodexConfig writes a toml block and replaces it on re-run", () => {
  const home = tempHome();

  writeCodexConfig(opts(home));
  const written = writeCodexConfig(opts(home));
  const toml = fs.readFileSync(written, "utf8");

  assert.equal(written, path.join(home, ".codex", "config.toml"));
  assert.equal(toml.match(/\[mcp_servers\.archivum\]/g).length, 1);
  assert.match(toml, /url = "https:\/\/vault\.example\.com\/sse"/);
});

test("writeCodexConfig leaves unrelated toml intact", () => {
  const home = tempHome();
  const target = path.join(home, ".codex", "config.toml");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, 'model = "gpt-5"\n');

  writeCodexConfig(opts(home));

  assert.match(fs.readFileSync(target, "utf8"), /model = "gpt-5"/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --workspace packages/archivum-cli`
Expected: FAIL — `Cannot find module '../src/clients.js'`

- [ ] **Step 3: Write the implementation**

Create `packages/archivum-cli/src/clients.js`:

```javascript
import fs from "node:fs";
import path from "node:path";

const CODEX_BEGIN = "# >>> archivum >>>";
const CODEX_END = "# <<< archivum <<<";

function readJson(file) {
  if (!fs.existsSync(file)) return {};
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    // A config we cannot parse is a config we must not overwrite silently.
    throw new Error(`${file} is not valid JSON. Fix or move it, then re-run.`);
  }
}

function writeJsonServer(file, { sseUrl, key }) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const config = readJson(file);
  config.mcpServers = {
    ...(config.mcpServers ?? {}),
    archivum: { url: sseUrl, headers: { Authorization: `Bearer ${key}` } },
  };
  fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
  return file;
}

export function writeClaudeConfig({ home, sseUrl, key }) {
  return writeJsonServer(path.join(home, ".claude.json"), { sseUrl, key });
}

export function writeCursorConfig({ home, sseUrl, key }) {
  return writeJsonServer(path.join(home, ".cursor", "mcp.json"), { sseUrl, key });
}

export function writeCodexConfig({ home, sseUrl, key }) {
  const file = path.join(home, ".codex", "config.toml");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  // Fenced block rather than a TOML parse: the CLI has no dependencies, and
  // rewriting only what we own keeps hand-written settings untouched.
  const stripped = existing.replace(
    new RegExp(`${CODEX_BEGIN}[\\s\\S]*?${CODEX_END}\\n?`, "g"),
    "",
  );
  const block = [
    CODEX_BEGIN,
    "[mcp_servers.archivum]",
    `url = "${sseUrl}"`,
    `http_headers = { Authorization = "Bearer ${key}" }`,
    CODEX_END,
    "",
  ].join("\n");
  fs.writeFileSync(file, `${stripped.trimEnd()}\n${stripped.trim() ? "\n" : ""}${block}`.trimStart());
  return file;
}

export const CLIENT_WRITERS = {
  claude: writeClaudeConfig,
  cursor: writeCursorConfig,
  codex: writeCodexConfig,
};

const MARKERS = {
  claude: [".claude.json", ".claude"],
  cursor: [".cursor"],
  codex: [".codex"],
};

export function detectClients(home) {
  return Object.entries(MARKERS)
    .filter(([, markers]) => markers.some((m) => fs.existsSync(path.join(home, m))))
    .map(([name]) => name);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test --workspace packages/archivum-cli`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/archivum-cli/src/clients.js packages/archivum-cli/test/clients.test.js
git commit -m "feat(cli): idempotent mcp config writers for claude, cursor, codex"
```

---

## Task 6: `archivum connect`

**Files:**
- Create: `packages/archivum-cli/src/connect.js`
- Modify: `packages/archivum-cli/src/index.js:20-27`, `packages/archivum-cli/src/util.js:6-24`
- Test: `packages/archivum-cli/test/connect.test.js`

**Interfaces:**
- Consumes: `detectClients`, `CLIENT_WRITERS` from Task 5; `POST /api/mcp/pairing/redeem` from Task 4
- Produces:
  - `connectCommand(args: string[]) -> Promise<void>`
  - `decodePairingToken(token: string) -> {baseUrl: string, secret: string}`
  - `redeem({baseUrl, secret, deviceName, fetchImpl}) -> Promise<object>`
  - `installSkill({home, skillUrl, fetchImpl}) -> Promise<string | null>` — writes `~/.claude/skills/archivum-memory/SKILL.md`, returns the path, or `null` when the server does not bundle a skill
  - State file at `~/.archivum/connection.json`: `{device_id, sse_url, base_url, key, linked_at, clients}`

`connect` never reads `.env` and never calls `ensureRoot()`.

- [ ] **Step 1: Write the failing test**

Create `packages/archivum-cli/test/connect.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { spawnSync } from "node:child_process";

import { decodePairingToken, installSkill, redeem } from "../src/connect.js";

function encode(baseUrl, secret) {
  const payload = Buffer.from(JSON.stringify({ u: baseUrl, s: secret })).toString("base64url");
  return `arch1_${payload}`;
}

test("decodePairingToken recovers the base url and secret", () => {
  const decoded = decodePairingToken(encode("https://vault.example.com", "s3cr3t"));

  assert.equal(decoded.baseUrl, "https://vault.example.com");
  assert.equal(decoded.secret, "s3cr3t");
});

test("decodePairingToken rejects a token that is not ours", () => {
  assert.throws(() => decodePairingToken("hunter2"), /pairing token/i);
});

test("redeem posts the secret and returns the device details", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return {
      ok: true,
      json: async () => ({ device_id: "dev_1", key: "amk_1", sse_url: "https://v/sse" }),
    };
  };

  const result = await redeem({
    baseUrl: "https://vault.example.com",
    secret: "s3cr3t",
    deviceName: "laptop",
    fetchImpl,
  });

  assert.equal(calls[0].url, "https://vault.example.com/api/mcp/pairing/redeem");
  assert.deepEqual(calls[0].body, { secret: "s3cr3t", device_name: "laptop" });
  assert.equal(result.key, "amk_1");
});

test("redeem surfaces the server's refusal message", async () => {
  const fetchImpl = async () => ({
    ok: false,
    status: 400,
    json: async () => ({ detail: { detail: "Pairing token is not valid.", code: "pairing_refused" } }),
  });

  await assert.rejects(
    redeem({ baseUrl: "https://v", secret: "x", deviceName: "l", fetchImpl }),
    /Pairing token is not valid/,
  );
});

test("installSkill writes the skill the server served", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const fetchImpl = async () => ({ ok: true, text: async () => "# Archivum Memory\n" });

  const written = await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl });

  assert.equal(written, path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md"));
  assert.equal(fs.readFileSync(written, "utf8"), "# Archivum Memory\n");
});

test("installSkill overwrites a stale copy so the skill can be updated", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const target = path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, "# old\n");
  const fetchImpl = async () => ({ ok: true, text: async () => "# new\n" });

  await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl });

  assert.equal(fs.readFileSync(target, "utf8"), "# new\n");
});

test("installSkill returns null when the server bundles no skill", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const fetchImpl = async () => ({ ok: false, status: 404 });

  assert.equal(await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl }), null);
});

test("connect runs without a repo checkout or an env file", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-nowhere-"));

  const result = spawnSync("node", [path.resolve("src/index.js"), "connect"], {
    cwd,
    encoding: "utf8",
    env: { ...process.env, PATH: process.env.PATH },
  });

  // No token given, so it must fail on usage — never on a missing .env or root.
  assert.match(result.stderr, /Usage: archivum connect/);
  assert.doesNotMatch(result.stderr, /install directory|repository root|\.env/i);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --workspace packages/archivum-cli`
Expected: FAIL — `Cannot find module '../src/connect.js'`

- [ ] **Step 3: Write the implementation**

Create `packages/archivum-cli/src/connect.js`:

```javascript
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { parseOptions } from "./util.js";
import { CLIENT_WRITERS, detectClients } from "./clients.js";

const STATE_DIR = ".archivum";
const STATE_FILE = "connection.json";

export function decodePairingToken(token) {
  if (typeof token !== "string" || !token.startsWith("arch1_")) {
    throw new Error("That is not an Archivum pairing token. Issue one from Settings.");
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.from(token.slice("arch1_".length), "base64url").toString());
  } catch {
    throw new Error("Malformed pairing token.");
  }
  if (!payload.u || !payload.s) throw new Error("Malformed pairing token.");
  return { baseUrl: payload.u, secret: payload.s };
}

export async function redeem({ baseUrl, secret, deviceName, fetchImpl = fetch }) {
  const response = await fetchImpl(`${baseUrl}/api/mcp/pairing/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret, device_name: deviceName }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.detail?.detail ?? `Pairing failed (HTTP ${response.status}).`);
  }
  return response.json();
}

export async function installSkill({ home, skillUrl, fetchImpl = fetch }) {
  const response = await fetchImpl(skillUrl).catch(() => null);
  // A server without a bundled skill is a working server; linking must not fail
  // over it. The tools are still there, the agent just gets no guidance on
  // which to reach for first.
  if (!response?.ok) return null;
  const target = path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, await response.text());
  return target;
}

function statePath(home) {
  return path.join(home, STATE_DIR, STATE_FILE);
}

function saveState(home, state) {
  const file = statePath(home);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  return file;
}

function readState(home) {
  const file = statePath(home);
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : null;
}

async function status(home) {
  const state = readState(home);
  if (!state) {
    console.log("Not linked. Run: archivum connect <pairing-token>");
    return;
  }
  console.log(`Linked to ${state.base_url} as ${state.device_id}`);
  console.log(`Clients configured: ${state.clients.join(", ") || "none"}`);
  const response = await fetch(`${state.base_url}/api/mcp/devices`, {
    headers: { Authorization: `Bearer ${state.key}` },
  }).catch(() => null);
  console.log(response?.ok ? "Key authenticates." : "Key no longer authenticates.");
}

async function revoke(home) {
  const state = readState(home);
  if (!state) throw new Error("Nothing to revoke: this machine is not linked.");
  await fetch(`${state.base_url}/api/mcp/devices/${state.device_id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${state.key}` },
  }).catch(() => null);
  fs.rmSync(statePath(home), { force: true });
  console.log("Revoked. Remove the archivum entry from your MCP clients if you want it gone from disk.");
}

export async function connectCommand(args) {
  const { flags, values, positionals } = parseOptions(args);
  const home = os.homedir();

  if (flags.has("status")) return status(home);
  if (flags.has("revoke")) return revoke(home);

  const token = positionals[0];
  if (!token) {
    throw new Error("Usage: archivum connect <pairing-token> [--name NAME] [--client claude|cursor|codex]");
  }

  const { baseUrl, secret } = decodePairingToken(token);
  const requested = values.get("client");
  const clients = requested
    ? [].concat(requested)
    : detectClients(home);
  if (clients.length === 0) {
    throw new Error("No supported MCP client found. Install Claude Code, Cursor, or Codex, or pass --client.");
  }

  const deviceName = values.get("name") ?? `${os.hostname()} / ${clients.join("+")}`;
  const details = await redeem({ baseUrl, secret, deviceName });

  const written = [];
  for (const client of clients) {
    const writer = CLIENT_WRITERS[client];
    if (!writer) throw new Error(`Unknown client: ${client}`);
    written.push(writer({ home, sseUrl: details.sse_url, key: details.key }));
  }

  const skillPath = details.skill_url
    ? await installSkill({ home, skillUrl: details.skill_url })
    : null;

  saveState(home, {
    device_id: details.device_id,
    base_url: baseUrl,
    sse_url: details.sse_url,
    key: details.key,
    linked_at: new Date().toISOString(),
    clients,
  });

  console.log(`Linked to ${details.vault_name ?? baseUrl} as "${deviceName}".`);
  for (const file of written) console.log(`  configured ${file}`);
  if (skillPath) console.log(`  installed ${skillPath}`);
  console.log("\nFor claude.ai or ChatGPT, add a custom connector:");
  console.log(`  URL:    ${details.sse_url}`);
  console.log(`  Header: Authorization: Bearer ${details.key}`);
  console.log("\nRestart your agent for the new server to appear.");
}
```

- [ ] **Step 4: Wire it into the CLI**

In `packages/archivum-cli/src/index.js`, add `import { connectCommand } from "./connect.js";` and `if (command === "connect") return connectCommand(args);` after the `mcp` line.

In `packages/archivum-cli/src/util.js`, add `connect <pairing-token> [--name NAME] [--client claude|cursor|codex]` to the help text under Commands, and add `"name"` to the value-flag list in `parseOptions` (the array currently containing `"set", "title", "content", "slug", "tag", "client", "service", "host", "dir"`).

Also update `packages/archivum-cli/test/cli.test.js` to assert the help output mentions `connect`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `npm test --workspace packages/archivum-cli`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/archivum-cli/src/connect.js packages/archivum-cli/src/index.js \
  packages/archivum-cli/src/util.js packages/archivum-cli/test/connect.test.js \
  packages/archivum-cli/test/cli.test.js
git commit -m "feat(cli): archivum connect links a machine from one pairing token"
```

---

## Task 7: Settings devices view

**Files:**
- Create: `apps/frontend/src/components/DevicesPanel.tsx`, `apps/frontend/src/components/DevicesPanel.test.tsx`
- Modify: `apps/frontend/src/api.ts` (after `getMcpSettings`, currently at line 1523), `apps/frontend/src/pages/SettingsPage.tsx` (the agent-access `Section`, around lines 505-520)

**Interfaces:**
- Consumes: `GET /api/mcp/devices`, `POST /api/mcp/pairing-tokens`, `DELETE /api/mcp/devices/{id}` from Task 4
- Produces:
  - `export type McpDevice = {id: string; name: string; created_at: string; last_seen_at: string | null; revoked_at: string | null}`
  - `export type PairingToken = {token: string; expires_at: string}`
  - `getMcpDevices(): Promise<McpDevice[]>`
  - `issuePairingToken(): Promise<PairingToken>`
  - `revokeMcpDevice(deviceId: string): Promise<boolean>`
  - `<DevicesPanel devices pairing legacyKeyConfigured onLink onRevoke />` — a presentational component; the page owns fetching, matching how `SettingsPage` already holds `mcpSettings` state

- [ ] **Step 1: Write the failing test**

Frontend component tests here render to a string with `react-dom/server` and assert
on the markup (see `apps/frontend/src/components/ProvenanceDrawer.test.tsx`). There
is no testing-library and no interaction harness, so the panel takes its data as
props and the page wires the callbacks.

Create `apps/frontend/src/components/DevicesPanel.test.tsx`:

```tsx
import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { DevicesPanel } from './DevicesPanel';

const device = {
  id: 'dev_abc',
  name: 'work laptop / claude',
  created_at: '2026-08-26T10:00:00Z',
  last_seen_at: '2026-08-26T11:00:00Z',
  revoked_at: null,
};

describe('DevicesPanel', () => {
  it('lists linked devices with when they were last seen', () => {
    const html = renderToString(
      <DevicesPanel devices={[device]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('work laptop / claude');
    expect(html).toContain('dev_abc');
  });

  it('shows a revoked device as revoked rather than hiding it', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[{ ...device, revoked_at: '2026-08-26T12:00:00Z' }]}
        pairing={null}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('Revoked');
    expect(html).toContain('work laptop / claude');
  });

  it('shows the exact command to run once a token is issued', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('npx archivum@latest connect arch1_xyz');
    expect(html).toContain('once');
    expect(html).toContain('15 minutes');
  });

  it('names the legacy shared key as a credential to retire', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('legacy shared key');
    expect(html).toContain('every client');
  });

  it('says nothing about a legacy key when none is configured', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).not.toContain('legacy shared key');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test --workspace apps/frontend`
Expected: FAIL — `Failed to resolve import "./DevicesPanel"`

- [ ] **Step 3: Add the API functions**

In `apps/frontend/src/api.ts`, after `getMcpSettings` (line 1523), add:

```ts
export type McpDevice = {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
};

export type PairingToken = { token: string; expires_at: string };

export async function getMcpDevices(): Promise<McpDevice[]> {
  const res = await apiFetch('/api/mcp/devices');
  if (!res.ok) throw new Error('Failed to load linked devices');
  return (await res.json()).devices;
}

export async function issuePairingToken(): Promise<PairingToken> {
  const res = await apiFetch('/api/mcp/pairing-tokens', { method: 'POST' });
  if (!res.ok) throw new Error('Failed to issue a pairing token');
  return res.json();
}

export async function revokeMcpDevice(deviceId: string): Promise<boolean> {
  const res = await apiFetch(`/api/mcp/devices/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error('Failed to revoke device');
  return (await res.json()).revoked;
}
```

`apiFetch` already attaches the CSRF token for POST and DELETE, so no extra handling is needed.

- [ ] **Step 4: Implement the panel**

Create `apps/frontend/src/components/DevicesPanel.tsx`. Match the styling primitives
`SettingsPage.tsx` already uses (`text-muted-foreground`, `font-mono text-xs`) rather
than introducing new ones. It renders:

- A **Link a device** button calling `onLink`.
- When `pairing` is set: the literal command `npx archivum@latest connect {pairing.token}` in a monospace block, plus "This token works once and expires in 15 minutes."
- A list of devices: name, `id`, created, last seen (or "never"), and either a Revoke button calling `onRevoke(device.id)` or the word `Revoked`.
- When `legacyKeyConfigured` is true, a row named `legacy shared key` with the note that it authenticates every client that holds it and should be retired once machines are linked individually.

- [ ] **Step 5: Wire it into SettingsPage**

In `apps/frontend/src/pages/SettingsPage.tsx`, add `devices` and `pairing` state
alongside the existing `mcpSettings` state (line 54), a `fetchDevices` callback
following the shape of `fetchMcpSettings` (line 151) and added to the effect's
dependency list (line 167-168), and render `<DevicesPanel />` inside the existing
agent-access `Section` (line 505). `onLink` calls `issuePairingToken` and stores the
result; `onRevoke` calls `revokeMcpDevice` then `fetchDevices`.

- [ ] **Step 6: Run tests and the build**

Run: `npm test --workspace apps/frontend && npm run build --workspace apps/frontend`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/DevicesPanel.tsx \
  apps/frontend/src/components/DevicesPanel.test.tsx \
  apps/frontend/src/api.ts apps/frontend/src/pages/SettingsPage.tsx
git commit -m "feat(settings): link and revoke devices from the mcp settings panel"
```

---

## Task 8: Documentation rewrite

**Files:**
- Modify: `README.md:1-12,60-97,132-149`, `AGENTS.md:14-27`, `docs/architecture/agent-access.md`, `docs/architecture/deploy.md:50-53`, `.env.example`

**Interfaces:**
- Consumes: everything above. No code changes.

- [ ] **Step 1: Rewrite the README opening**

Replace lines 1-12 with the positioning from the spec. The opening sentence becomes:

> Archivum is memory your coding agents use and you can audit. What your agents learn about your repos — decisions, fixes, architecture — lives in one vault you own, reviewed and cited, and follows you to every machine you work on.

Follow it with the four supporting claims in spec order: same memory everywhere; less context for the same answer; you can see and correct what it knows; your data on your disk. Do not claim lower latency than local markdown — the honest comparison is context size and portability.

- [ ] **Step 2: Restructure the quickstart around linking**

Replace the MCP client JSON blocks (lines 72-97) with:

````markdown
## Link your agents

Once the stack is up, open Settings → MCP and click **Link a device**. Then on
each machine you code from — including the one running the server:

```bash
npx archivum@latest connect arch1_...
```

That writes MCP config for Claude Code, Cursor, and Codex, installs the memory
skill, and verifies it can reach the vault. The token works once and expires in
fifteen minutes; issue a new one per machine.

`archivum connect --status` reports what is linked here.
`archivum connect --revoke` unlinks this machine and revokes its key.
````

Keep the hand-written JSON as a short "configuring a client by hand" subsection below, and state that the `docker exec` stdio form only works on the machine running the container.

- [ ] **Step 3: Amend the AGENTS.md product direction**

Replace lines 16-27. The direction becomes coding-agent memory; wiki, graph, and backlinks are described as how memory is inspected. Keep the existing prohibition on Archductor and Archgraph framing. Add the constraint that public docs must not claim lower recall latency than local markdown.

- [ ] **Step 4: Rewrite agent-access.md**

Lead with `archivum connect`. Add a "one machine or many" section stating plainly that stdio is single-machine only. Move the raw JSON into a manual-configuration appendix. Replace the `cp -r skills/archivum-memory ~/.claude/skills/` instruction with `connect`, noting the copy has no update path. Update the security paragraph: HTTP now always authenticates, and per-device keys are revocable individually.

- [ ] **Step 5: Correct deploy.md**

In the section at lines 50-53, add pairing to the post-deploy steps. Correct the capture note to say that on a hosted deployment, transcripts on your laptops are not visible to the server, so automatic capture does not cover them — `record_work` is the path that works today, and transcript shipping is planned.

- [ ] **Step 6: Prune the Features list**

Apply the spec's cut table to `README.md:132-149`. Remove Life OS workflows from
Features (the MCP tools stay; the README itself already calls it "not the main
positioning"). Move local media transcription into an optional-extras subsection.
Promote semantic search and cited Q&A above sharing and export. Reframe the graph
bullet as inspecting what your agents know rather than exploring a graph.

Nothing is deleted from the codebase — this changes only what the docs lead with.

- [ ] **Step 7: Note the legacy key in .env.example**

Where `MCP_API_KEY` is documented, add that it is now a legacy shared credential kept for existing installs; new machines should use `archivum connect`, and the key can be left unset once every client is linked individually.

- [ ] **Step 8: Verify**

```bash
rg -n "scripts/[b]ootstrap|[N]eo4j|[y]ou@youremail|[L]ast updated: 2026-06|[f]eature complete" -g "*.md" -g "!node_modules/**" -g "!apps/backend/.venv/**"
rg -n "Obsidian-style" README.md AGENTS.md docs/
```

Expected: both silent.

- [ ] **Step 9: Run the full suite**

```bash
npm test --workspace apps/frontend
npm run build --workspace apps/frontend
npm test --workspace packages/archivum-cli
cd apps/backend && uv run --group dev pytest ../../tests -q
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add README.md AGENTS.md docs/architecture/agent-access.md \
  docs/architecture/deploy.md .env.example
git commit -m "docs: lead with coding-agent memory and one-command linking"
```

---

## Deferred to later phases

Not in this plan, per the spec's phasing:

- **Phase 2** — `archivum agent watch`, the transcript shipper that makes capture work from machines other than the server's. Task 8 step 5 documents the gap honestly in the meantime.
- **Phase 3** — the context pack screen, the before/after benchmark, and the tokens-versus-success metric.

## Open questions carried from the spec

These are implemented under the spec's stated assumptions. Revisit if the assumption proves wrong:

1. Pairing token lifetime is 15 minutes (`DEFAULT_TTL_SECONDS = 900`).
2. `connect` configures clients **globally** (`~/.claude.json`, `~/.cursor/mcp.json`, `~/.codex/config.toml`). A `--project` variant is not built.
3. Transcript shipping is Phase 2; the raw-versus-filtered question is not decided here.
4. Revoking a device does not delete memories it wrote. Recording which device wrote what is not implemented in this phase.
