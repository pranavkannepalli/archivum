"""Owner-side sharing management.

Everything here is about *deciding* who sees what. Serving the content to a
recipient is `api/shared.py`, and the split is deliberate: this router requires
write access, that one never accepts an owner session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from archivum.auth import CurrentUser, require_writer
from archivum.db import sqlite
from archivum.sharing.models import SHARE_ROLES, Grant, Principal, Subject
from archivum.sharing.repository import SharingRepository
from archivum.sharing.urn import UrnError, build, parse

router = APIRouter(prefix="/api/sharing", tags=["sharing"])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreatePrincipalRequest(BaseModel):
    display_name: str


class CreatePrincipalResponse(BaseModel):
    principal: Principal
    claim_token: str
    claim_url: str


class CreateGrantRequest(BaseModel):
    # Either a full urn, or the kind plus local id — the caller's own wiki is
    # filled in from the session. A browser sharing its own page should not
    # have to know its tenant id to name the thing on screen.
    resource_urn: str | None = None
    resource_kind: str | None = None
    resource_id: str | None = None
    # Either a principal id, or subject_kind='link' for anyone-with-the-link.
    principal_id: str | None = None
    subject_kind: str = "principal"
    role: str = "viewer"
    expires_in_days: int | None = None
    include_cited: bool = False


class CreateGrantResponse(BaseModel):
    grant: Grant
    # Present exactly once, at creation, for link grants only.
    share_token: str | None = None
    share_url: str | None = None


class GrantListing(BaseModel):
    id: str
    resource_urn: str
    role: str
    subject_kind: str
    display_name: str | None = None
    created_at: str = ""
    expires_at: str | None = None


class CreateHoldRequest(BaseModel):
    grant_id: str
    resource_urn: str
    reason: str = "agent_authored"


class ReleaseHoldRequest(BaseModel):
    resource_urn: str


class HoldListing(BaseModel):
    grant_id: str
    resource_urn: str
    grant_urn: str
    reason: str
    role: str
    display_name: str | None = None
    created_at: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bad_request(detail: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"detail": detail, "code": code},
    )


def _require_own_wiki(resource_urn: str, wiki_id: str) -> None:
    """Reject a urn that is malformed or addresses somebody else's wiki."""
    try:
        resource = parse(resource_urn)
    except UrnError as exc:
        raise _bad_request(str(exc), "invalid_resource_urn") from exc
    if resource.wiki_id != wiki_id:
        raise _bad_request(
            f"Resource {resource_urn!r} does not belong to this vault",
            "resource_wrong_wiki",
        )


def _resource_urn_from(
    resource_urn: str | None,
    resource_kind: str | None,
    resource_id: str | None,
    wiki_id: str,
) -> str:
    """Accept either addressing form and return one validated urn."""
    if resource_urn:
        _require_own_wiki(resource_urn, wiki_id)
        return resource_urn

    if not resource_kind:
        raise _bad_request(
            "Name the resource by urn, or by kind and id", "missing_resource"
        )
    try:
        return build(resource_kind, wiki_id, resource_id or "")
    except UrnError as exc:
        raise _bad_request(str(exc), "invalid_resource_urn") from exc


# ── Principals ────────────────────────────────────────────────────────────────

@router.post(
    "/principals",
    response_model=CreatePrincipalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_principal(
    body: CreatePrincipalRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> CreatePrincipalResponse:
    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        try:
            principal, claim_token = await repo.create_principal(
                current_user.wiki_id, body.display_name
            )
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_principal") from exc

    return CreatePrincipalResponse(
        principal=principal,
        claim_token=claim_token,
        claim_url=f"/claim/{claim_token}",
    )


@router.get("/principals", response_model=list[Principal])
async def list_principals(
    current_user: CurrentUser = Depends(require_writer),
) -> list[Principal]:
    async with sqlite.get_db() as conn:
        return await SharingRepository(conn).list_principals(current_user.wiki_id)


@router.delete("/principals/{principal_id}")
async def revoke_principal(
    principal_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        principal = await repo.get_principal(principal_id)
        if principal is None or principal.wiki_id != current_user.wiki_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such person", "code": "principal_not_found"},
            )
        await repo.revoke_principal(principal_id)

    return {"detail": "revoked"}


# ── Grants ────────────────────────────────────────────────────────────────────

@router.post(
    "/grants", response_model=CreateGrantResponse, status_code=status.HTTP_201_CREATED
)
async def create_grant(
    body: CreateGrantRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> CreateGrantResponse:
    resource_urn = _resource_urn_from(
        body.resource_urn, body.resource_kind, body.resource_id, current_user.wiki_id
    )

    if body.role not in SHARE_ROLES:
        raise _bad_request(
            f"Role must be one of {sorted(SHARE_ROLES)}", "invalid_role"
        )

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)

        if body.subject_kind == "link":
            grant, token = await repo.create_link_grant(
                wiki_id=current_user.wiki_id,
                resource_urn=resource_urn,
                role=body.role,
                created_by=current_user.username,
                expires_in_days=body.expires_in_days,
                include_cited=body.include_cited,
            )
            return CreateGrantResponse(
                grant=grant, share_token=token, share_url=f"/share/{token}"
            )

        if body.subject_kind != "principal":
            raise _bad_request(
                "Share subject must be a principal or a link", "invalid_subject_kind"
            )
        if not body.principal_id:
            raise _bad_request(
                "Sharing with a person needs principal_id", "missing_principal"
            )

        principal = await repo.get_principal(body.principal_id)
        if (
            principal is None
            or principal.revoked
            or principal.wiki_id != current_user.wiki_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such person", "code": "principal_not_found"},
            )

        grant = await repo.create_grant(
            wiki_id=current_user.wiki_id,
            subject=Subject.principal(principal.id),
            resource_urn=resource_urn,
            role=body.role,
            created_by=current_user.username,
            expires_in_days=body.expires_in_days,
            include_cited=body.include_cited,
        )

    return CreateGrantResponse(grant=grant)


@router.get("/grants", response_model=list[GrantListing])
async def list_grants(
    resource_urn: str | None = Query(default=None, description="Full resource urn"),
    resource_kind: str | None = Query(default=None, description="e.g. entry, folder"),
    resource_id: str | None = Query(default=None, description="Slug or id"),
    current_user: CurrentUser = Depends(require_writer),
) -> list[GrantListing]:
    """Who can see this resource. Never returns share tokens."""
    resource_urn = _resource_urn_from(
        resource_urn, resource_kind, resource_id, current_user.wiki_id
    )

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        grants = await repo.list_grants_for_resource(resource_urn)

        listings: list[GrantListing] = []
        for grant in grants:
            display_name: str | None = None
            if grant.subject_kind == "principal":
                principal = await repo.get_principal(grant.subject_id)
                if principal is None or principal.revoked:
                    continue
                display_name = principal.display_name

            listings.append(
                GrantListing(
                    id=grant.id,
                    resource_urn=grant.resource_urn,
                    role=grant.role,
                    subject_kind=grant.subject_kind,
                    display_name=display_name,
                    created_at=grant.created_at,
                    expires_at=grant.expires_at,
                )
            )

    return listings


@router.delete("/grants/{grant_id}")
async def revoke_grant(
    grant_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        grant = await repo.get_grant(grant_id)
        if grant is None or grant.wiki_id != current_user.wiki_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such grant", "code": "grant_not_found"},
            )
        await repo.revoke_grant(grant_id)

    return {"detail": "revoked"}


# ── Holds ─────────────────────────────────────────────────────────────────────

@router.post("/holds", status_code=status.HTTP_201_CREATED)
async def create_hold(
    body: CreateHoldRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> dict[str, str]:
    """Withhold one resource from a grant that would otherwise cover it."""
    _require_own_wiki(body.resource_urn, current_user.wiki_id)

    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        grant = await repo.get_grant(body.grant_id)
        if grant is None or grant.wiki_id != current_user.wiki_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such grant", "code": "grant_not_found"},
            )
        await repo.hold(body.grant_id, body.resource_urn, body.reason)

    return {"detail": "held"}


@router.get("/holds", response_model=list[HoldListing])
async def list_holds(
    current_user: CurrentUser = Depends(require_writer),
) -> list[HoldListing]:
    """Everything waiting on you before a recipient can see it."""
    async with sqlite.get_db() as conn:
        rows = await SharingRepository(conn).list_holds(current_user.wiki_id)

    return [
        HoldListing(
            grant_id=row["grant_id"],
            resource_urn=row["resource_urn"],
            grant_urn=row["grant_urn"],
            reason=row["reason"],
            role=row["role"],
            display_name=row["display_name"],
            created_at=row["created_at"] or "",
        )
        for row in rows
    ]


@router.post("/holds/{grant_id}/approve")
async def approve_hold(
    grant_id: str,
    body: ReleaseHoldRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> dict[str, str]:
    async with sqlite.get_db() as conn:
        repo = SharingRepository(conn)
        grant = await repo.get_grant(grant_id)
        if grant is None or grant.wiki_id != current_user.wiki_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": "No such grant", "code": "grant_not_found"},
            )
        released = await repo.release_hold(grant_id, body.resource_urn)

    if not released:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "Nothing was being held", "code": "hold_not_found"},
        )
    return {"detail": "approved"}
