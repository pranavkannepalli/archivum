"""Recipient-side reading.

This is the only router that accepts a share recipient's identity, and every
handler in it resolves through `sharing.resolver` before returning anything.
The separation is the enforcement model: `get_current_user` refuses recipient
tokens, so a recipient cannot reach an owner route at all, and a new owner
route therefore cannot leak to them by forgetting a check.
"""

from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from archivum.auth import (
    RECIPIENT_COOKIE,
    RecipientIdentity,
    create_recipient_token,
    decode_recipient_token,
)
from archivum.config import Settings, get_settings
from archivum.db import sqlite
from archivum.knowledge.suggestions import SuggestionRepository
from archivum.memory.registry import MemoryAssetRegistry
from archivum.rate_limit import InMemoryRateLimiter, RateLimitPolicy
from archivum.sharing.models import Access, Subject, hash_token
from archivum.sharing.repository import SharingRepository
from archivum.sharing.resolver import list_visible, resolve
from archivum.sharing.urn import UrnError, parse

router = APIRouter(prefix="/api/shared", tags=["shared"])
logger = logging.getLogger(__name__)

# Claim attempts are limited per token rather than per IP. `get_client_ip`
# collapses everyone behind a proxy into a single bucket, so IP keying would
# let one recipient's retries lock every other recipient out of claiming.
_claim_limiter = InMemoryRateLimiter()
_CLAIM_POLICY = RateLimitPolicy(limit=10, window_seconds=300)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ClaimRequest(BaseModel):
    claim_token: str


class ClaimResponse(BaseModel):
    principal_id: str
    display_name: str
    wiki_id: str


class SharedCitation(BaseModel):
    title: str
    # Only set when the cited source was itself shared; otherwise the citation
    # renders as an inert title rather than a link into a login wall.
    urn: str | None = None


class SharedResource(BaseModel):
    urn: str
    kind: str
    role: str
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    citations: list[SharedCitation] = Field(default_factory=list)
    children: list["SharedListing"] = Field(default_factory=list)
    may_comment: bool = False
    shared_by_inheritance: str | None = None


class SharedListing(BaseModel):
    urn: str
    kind: str
    title: str
    role: str


SharedResource.model_rebuild()


class CommentRequest(BaseModel):
    urn: str
    text: str


# ── Identity ──────────────────────────────────────────────────────────────────

async def get_recipient(
    share_session: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
) -> RecipientIdentity:
    if not share_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"detail": "No share session", "code": "no_share_session"},
        )
    return decode_recipient_token(share_session, settings)


async def resolve_subject(
    token: str | None = Query(default=None, description="A share link token"),
    share_session: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
) -> tuple[Subject, str]:
    """Return the asking subject and the wiki it belongs to.

    A link token wins over a session cookie so that an owner or a claimed
    recipient opening someone else's link sees exactly what that link grants,
    rather than silently getting their own broader access.
    """
    if token:
        return Subject.link_from_token(token), ""

    if share_session:
        identity = decode_recipient_token(share_session, settings)
        return Subject.principal(identity.principal_id), identity.wiki_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"detail": "No share session", "code": "no_share_session"},
    )


def _not_found() -> HTTPException:
    """One shape for every denial.

    A recipient must not be able to tell "does not exist" from "exists but is
    not shared with you" from "is being held for review" — the difference is an
    oracle for probing the vault.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"detail": "Not found", "code": "not_found"},
    )


# ── Claiming ──────────────────────────────────────────────────────────────────

@router.post("/claim", response_model=ClaimResponse)
async def claim(
    body: ClaimRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> ClaimResponse:
    """Bind a claim token to its holder and hand back a recipient session."""
    allowed, retry_after = await _claim_limiter.check(
        hash_token(body.claim_token), _CLAIM_POLICY
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"detail": "Too many attempts", "code": "claim_rate_limited"},
            headers={"Retry-After": str(retry_after)},
        )

    async with sqlite.get_db() as conn:
        principal = await SharingRepository(conn).claim_principal(body.claim_token)

    if principal is None:
        raise _not_found()

    session = create_recipient_token(principal.id, principal.wiki_id, settings)
    max_age = settings.refresh_token_expire_days * 86400
    response.set_cookie(
        key=RECIPIENT_COOKIE,
        value=session,
        httponly=True,
        samesite="strict",
        secure=False,  # set True behind HTTPS, matching the owner cookies
        max_age=max_age,
        path="/",
    )
    # A recipient session is cookie auth, so their writes go through the same
    # double-submit CSRF check the owner's do. Issue the token here or the
    # first comment would be rejected.
    response.set_cookie(
        key="csrf_token",
        value=secrets.token_urlsafe(32),
        httponly=False,
        samesite="strict",
        secure=False,
        max_age=max_age,
        path="/",
    )

    return ClaimResponse(
        principal_id=principal.id,
        display_name=principal.display_name,
        wiki_id=principal.wiki_id,
    )


# ── Reading ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SharedListing])
async def list_shared(
    subject_and_wiki: tuple[Subject, str] = Depends(resolve_subject),
) -> list[SharedListing]:
    """Everything shared with the caller."""
    subject, _wiki = subject_and_wiki

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        accesses = await list_visible(repo, subject)
        return [
            SharedListing(
                urn=access.resource_urn,
                kind=parse(access.resource_urn).kind,
                title=await _title_for(conn, repo, access.resource_urn),
                role=access.role,
            )
            for access in accesses
        ]


@router.get("/by-token/{token}", response_model=SharedResource)
async def open_link(token: str) -> SharedResource:
    """Open whatever a share link points at.

    The entry point for `/share/{token}`: the holder of a link knows the token
    but not the urn behind it, so the token itself has to name the resource.
    """
    subject = Subject.link_from_token(token)

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        grants = await repo.list_grants_for_subject(subject)
        if not grants:
            raise _not_found()
        urn = grants[0].resource_urn

    return await get_shared_resource(urn=urn, subject_and_wiki=(subject, ""))


@router.get("/resource", response_model=SharedResource)
async def get_shared_resource(
    urn: str = Query(..., description="The resource urn to open"),
    subject_and_wiki: tuple[Subject, str] = Depends(resolve_subject),
) -> SharedResource:
    subject, _wiki = subject_and_wiki

    try:
        resource = parse(urn)
    except UrnError:
        raise _not_found() from None

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        access = await resolve(repo, subject, urn)
        if access is None:
            raise _not_found()

        if resource.kind == "entry":
            return await _entry_resource(conn, repo, subject, resource, access)
        if resource.kind == "view":
            return await _view_resource(repo, resource, access)
        if resource.kind == "asset":
            return await _asset_resource(conn, repo, subject, resource, access)
        if resource.kind == "folder":
            return await _folder_resource(conn, repo, subject, resource, access)

    raise _not_found()


@router.post("/comment", status_code=status.HTTP_201_CREATED)
async def comment(
    body: CommentRequest,
    identity: RecipientIdentity = Depends(get_recipient),
) -> dict[str, str]:
    """A recipient's proposal. Lands in the owner's review queue, never the vault."""
    text = body.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "A comment needs text", "code": "empty_comment"},
        )

    try:
        resource = parse(body.urn)
    except UrnError:
        raise _not_found() from None

    subject = Subject.principal(identity.principal_id)

    async with sqlite.get_db() as conn:
        access = await resolve(SharingRepository(conn), subject, body.urn)
        if access is None:
            raise _not_found()
        if not access.may_comment():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"detail": "You have read-only access", "code": "read_only"},
            )

        suggestion = await SuggestionRepository(conn).create_suggestion(
            target_id=f"page:{resource.wiki_id}:{resource.local_id}",
            suggestion_type="recipient_comment",
            proposed_markdown=text,
            proposed_objects=[],
            citations=[],
            rationale=f"Comment from a share recipient on {body.urn}",
            author_principal_id=identity.principal_id,
        )

    return {"detail": "submitted", "suggestion_id": suggestion.id}


# ── Content assembly ──────────────────────────────────────────────────────────

def _deserialize_tags(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


async def _title_for(conn, repo: SharingRepository, urn: str) -> str:
    """A display title for a listing, without leaking anything not granted."""
    resource = parse(urn)
    if resource.kind == "folder":
        return resource.local_id.rsplit("/", 1)[-1] or "Vault"
    if resource.kind == "view":
        view = await repo.get_view(urn)
        return str(view["title"]) if view else "Shared query"
    if resource.kind == "entry":
        page = await sqlite.get_page(resource.local_id, resource.wiki_id)
        if page:
            return str(page.get("title") or resource.local_id)
    if resource.kind == "asset":
        asset = await MemoryAssetRegistry(conn).get_asset(resource.local_id)
        if asset:
            return asset.name
    return resource.local_id


async def _entry_resource(conn, repo, subject, resource, access: Access) -> SharedResource:
    page = await sqlite.get_page(resource.local_id, resource.wiki_id)
    if not page:
        raise _not_found()

    return SharedResource(
        urn=str(resource),
        kind="entry",
        role=access.role,
        title=str(page.get("title") or resource.local_id),
        body=str(page.get("content") or ""),
        tags=_deserialize_tags(page.get("tags")),
        may_comment=access.may_comment(),
        shared_by_inheritance=access.inherited_from,
    )


async def _view_resource(repo, resource, access: Access) -> SharedResource:
    view = await repo.get_view(str(resource))
    if not view:
        raise _not_found()

    payload = view.get("payload") or {}
    citations = [
        SharedCitation(title=str(item.get("title") or item.get("slug") or ""))
        for item in payload.get("citations", [])
        if isinstance(item, dict)
    ]

    return SharedResource(
        urn=str(resource),
        kind="view",
        role=access.role,
        title=str(view.get("title") or "Shared query"),
        body=str(payload.get("answer") or ""),
        citations=citations,
        may_comment=False,
        shared_by_inheritance=access.inherited_from,
    )


async def _asset_resource(conn, repo, subject, resource, access: Access) -> SharedResource:
    asset = await MemoryAssetRegistry(conn).get_asset(resource.local_id)
    if asset is None or asset.wiki_id != resource.wiki_id:
        raise _not_found()

    # Citation *titles* always travel with the asset; the cited source itself
    # only becomes reachable if it was granted in its own right. Otherwise one
    # share of a distilled memory would quietly drag its evidence along.
    citations: list[SharedCitation] = []
    for citation in asset.citations:
        source_urn = f"source:{resource.wiki_id}:{citation.source_id}"
        reachable = await resolve(repo, subject, source_urn)
        citations.append(
            SharedCitation(
                title=citation.quote[:80] or citation.source_id,
                urn=source_urn if reachable else None,
            )
        )

    return SharedResource(
        urn=str(resource),
        kind="asset",
        role=access.role,
        title=asset.name,
        body=asset.body or asset.summary,
        tags=asset.tags,
        citations=citations,
        may_comment=access.may_comment(),
        shared_by_inheritance=access.inherited_from,
    )


async def _folder_resource(conn, repo, subject, resource, access: Access) -> SharedResource:
    """A shared folder lists the children the caller may actually open.

    Each child is resolved individually rather than assumed visible, so a held
    page inside a shared folder simply is not listed.
    """
    pages = await sqlite.list_pages_under(resource.local_id, resource.wiki_id)

    children: list[SharedListing] = []
    for page in pages:
        child_urn = f"entry:{resource.wiki_id}:{page['slug']}"
        child_access = await resolve(repo, subject, child_urn)
        if child_access is None:
            continue
        children.append(
            SharedListing(
                urn=child_urn,
                kind="entry",
                title=str(page.get("title") or page["slug"]),
                role=child_access.role,
            )
        )

    return SharedResource(
        urn=str(resource),
        kind="folder",
        role=access.role,
        title=resource.local_id.rsplit("/", 1)[-1] or "Vault",
        children=children,
        may_comment=access.may_comment(),
        shared_by_inheritance=access.inherited_from,
    )
