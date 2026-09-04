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


def _sse_url(settings: Settings) -> str:
    # Same idiom as `api/system.py:409`, which already resolves this endpoint.
    return settings.mcp_public_url.strip() or f"http://localhost:{settings.mcp_port}/sse"


def _api_base(request: Request, settings: Settings) -> str:
    configured = settings.api_public_url.strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


async def require_device(request: Request) -> dict[str, Any]:
    """Authenticate a caller by its own device key.

    Owner routes decode a JWT; a device key is an opaque bearer that only
    `DeviceRepository.verify` understands, so a device authenticating as
    itself needs its own dependency.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, raw_key = header.partition(" ")
    if scheme.lower() != "bearer" or not raw_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device key required")
    async with sqlite.get_db() as conn:
        device = await DeviceRepository(conn).verify(raw_key)
    if device is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device key required")
    return device


@router.post("/pairing-tokens")
async def issue_pairing_token(
    request: Request,
    current_user: CurrentUser = Depends(require_owner),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        token, expires_at = await PairingService(conn).issue(
            _api_base(request, settings), wiki_id=current_user.wiki_id
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
    return {
        "device_id": device["id"],
        "key": raw_key,
        "sse_url": _sse_url(settings),
        "vault_name": settings.owner_username or "Archivum",
        "skill_url": f"{_api_base(request, settings)}/api/mcp/skill",
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


@router.get("/devices/self")
async def get_own_device(
    device: dict[str, Any] = Depends(require_device),
) -> dict[str, Any]:
    """What `--status` calls: a 200 here means the caller's own key still authenticates.

    Registered ahead of `/devices/{device_id}` so "self" is never captured as
    a device id path parameter.
    """
    return _public(device)


@router.delete("/devices/self")
async def revoke_own_device(
    device: dict[str, Any] = Depends(require_device),
) -> dict[str, bool]:
    """What `--revoke` calls: a device key can revoke only itself.

    Registered ahead of `/devices/{device_id}` for the same routing reason as
    `get_own_device` above.
    """
    async with sqlite.get_db() as conn:
        revoked = await DeviceRepository(conn).revoke(device["id"])
    return {"revoked": revoked}


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
        repo = DeviceRepository(conn)
        # DeviceRepository.revoke() matches by id alone, with no wiki scoping
        # of its own — mirror the check sharing.py's revoke_grant/revoke_principal
        # make, so an owner of one wiki cannot revoke another wiki's device by
        # guessing its id.
        device = await repo.get(device_id)
        if device is None or device["wiki_id"] != current_user.wiki_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such device", "code": "device_not_found"},
            )
        revoked = await repo.revoke(device_id)
    return {"revoked": revoked}
