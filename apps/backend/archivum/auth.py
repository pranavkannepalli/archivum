"""JWT creation/validation, password hashing, FastAPI dependencies."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel

from archivum.config import Settings, get_settings


# ── Models ──────────────────────────────────────────────────────────────────

class TokenPayload(BaseModel):
    sub: str          # username
    role: str
    wiki_id: str
    exp: datetime


class CurrentUser(BaseModel):
    username: str
    role: str
    wiki_id: str


# A share recipient is not a wiki member. Their token carries its own `type`,
# so it fails `decode_access_token`'s type check and cannot authenticate
# against any owner route even if one forgets to look at the role.
RECIPIENT_ROLE = "recipient"
RECIPIENT_TOKEN_TYPE = "share_recipient"
RECIPIENT_COOKIE = "share_session"


class RecipientIdentity(BaseModel):
    """Whoever claimed a share link. Holds no wiki role at all."""

    principal_id: str
    wiki_id: str


# ── Password helpers ─────────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """Return bcrypt hash of *plaintext*."""
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches *hashed*."""
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


# ── Token helpers ─────────────────────────────────────────────────────────────

def create_access_token(
    username: str,
    role: str,
    wiki_id: str,
    settings: Settings,
) -> str:
    expire = datetime.now(UTC) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {
        "sub": username,
        "role": role,
        "wiki_id": wiki_id,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token() -> tuple[str, str]:
    """Return (raw_token, hashed_token) pair."""
    raw = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def create_recipient_token(
    principal_id: str,
    wiki_id: str,
    settings: Settings,
) -> str:
    """Mint the session a claimed share principal carries."""
    expire = datetime.now(UTC) + timedelta(
        days=settings.refresh_token_expire_days
    )
    payload = {
        "sub": principal_id,
        "wiki_id": wiki_id,
        "role": RECIPIENT_ROLE,
        "exp": expire,
        "type": RECIPIENT_TOKEN_TYPE,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_recipient_token(token: str, settings: Settings) -> RecipientIdentity:
    """Decode a recipient session. Raises HTTPException on anything else."""
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired share session",
        ) from exc

    if data.get("type") != RECIPIENT_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type",
        )

    return RecipientIdentity(
        principal_id=data["sub"],
        wiki_id=data.get("wiki_id", "default"),
    )


def decode_access_token(token: str, settings: Settings) -> TokenPayload:
    """Decode and validate an access JWT. Raises HTTPException on failure."""
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if data.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type",
        )

    # Belt and braces. The type check above already excludes recipient tokens;
    # this second gate means a future token type that forgets to set `type`
    # still cannot smuggle a share recipient onto an owner route.
    if data.get("role") == RECIPIENT_ROLE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Share recipients cannot access this API",
        )

    return TokenPayload(
        sub=data["sub"],
        role=data["role"],
        wiki_id=data.get("wiki_id", "default"),
        exp=datetime.fromtimestamp(data["exp"], tz=UTC),
    )


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """
    Resolve the current user from:
    1. httpOnly cookie `access_token`
    2. Authorization: Bearer <token> header  (for MCP / CLI clients)
    """
    token: str | None = None

    if access_token:
        token = access_token
    elif authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token, settings)
    return CurrentUser(
        username=payload.sub,
        role=payload.role,
        wiki_id=payload.wiki_id,
    )


async def require_owner(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role required",
        )
    return user


async def require_writer(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if user.role not in {"owner", "collaborator"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required",
        )
    return user
