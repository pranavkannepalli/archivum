"""Human review lifecycle routes for agent-proposed memory suggestions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from pydantic import ValidationError

from archivum.api.pages import _validate_slug
from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.db import sqlite
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.repository import KnowledgeRepository
from archivum.knowledge.suggestions import (
    MemorySuggestion,
    SuggestionAction,
    SuggestionRepository,
    SuggestionStatus,
)
from archivum.memory.registry import MemoryAssetRegistry

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


class CreateSuggestionRequest(BaseModel):
    target_id: str | None = None
    page_slug: str | None = None
    suggestion_type: str = Field(min_length=1)
    proposed_markdown: str = ""
    proposed_objects: list[Any] = Field(default_factory=list)
    citations: list[Any] = Field(default_factory=list)
    proposed_scopes: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    duplicates: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    retention_tier: str = "candidate"
    agent_visibility: str = "review_required"
    rationale: str = ""
    estimated_durability: str = ""
    expires_at: str | None = None

    @model_validator(mode="after")
    def target_is_unambiguous(self) -> "CreateSuggestionRequest":
        if bool(self.target_id) == bool(self.page_slug):
            raise ValueError("Provide exactly one of target_id or page_slug")
        return self


class ReviewSuggestionRequest(BaseModel):
    action: SuggestionAction
    asset_id: str | None = None
    scope: str | None = None
    visibility: str | None = None
    edited_markdown: str | None = None


class ExpireSuggestionsRequest(BaseModel):
    now: str = Field(min_length=1)


@router.get("", response_model=list[MemorySuggestion])
async def list_suggestions(
    page_slug: str | None = Query(default=None),
    status_filter: SuggestionStatus | None = Query(default="pending", alias="status"),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MemorySuggestion]:
    """List pending review suggestions visible to the authenticated wiki."""
    async with sqlite.get_db() as conn:
        repo = SuggestionRepository(conn)
        if page_slug is not None:
            target_id = _page_target_id(current_user.wiki_id, page_slug)
            return await repo.list_suggestions(target_id=target_id, status=status_filter)
        return await repo.list_suggestions(
            target_ids=[_wiki_target_id(current_user.wiki_id)],
            target_prefixes=[
                _page_target_prefix(current_user.wiki_id),
                _wiki_target_prefix(current_user.wiki_id),
            ],
            status=status_filter,
        )


@router.post("", response_model=MemorySuggestion, status_code=status.HTTP_201_CREATED)
async def create_suggestion(
    body: CreateSuggestionRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemorySuggestion:
    """Create a wiki-scoped suggestion for internal authenticated callers."""
    target_id = (
        _page_target_id(current_user.wiki_id, body.page_slug)
        if body.page_slug is not None
        else body.target_id
    )
    assert target_id is not None
    _require_authorized_target(target_id, current_user.wiki_id)
    async with sqlite.get_db() as conn:
        return await SuggestionRepository(conn).create_suggestion(
            target_id=target_id,
            suggestion_type=body.suggestion_type,
            proposed_markdown=body.proposed_markdown,
            proposed_objects=body.proposed_objects,
            citations=body.citations,
            proposed_scopes=body.proposed_scopes,
            scores=body.scores,
            duplicates=body.duplicates,
            conflicts=body.conflicts,
            retention_tier=body.retention_tier,
            agent_visibility=body.agent_visibility,
            rationale=body.rationale,
            estimated_durability=body.estimated_durability,
            expires_at=body.expires_at,
        )


@router.post("/expire", response_model=list[MemorySuggestion])
async def expire_suggestions(
    body: ExpireSuggestionsRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> list[MemorySuggestion]:
    async with sqlite.get_db() as conn:
        repo = SuggestionRepository(conn)
        expired = await repo.expire_due_candidates(body.now)
        return [
            suggestion
            for suggestion in expired
            if _is_authorized_target(suggestion.target_id, current_user.wiki_id)
        ]


@router.post("/{suggestion_id:path}/accept", response_model=MemorySuggestion)
async def accept_suggestion(
    suggestion_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> MemorySuggestion:
    return await _review_suggestion(
        suggestion_id,
        ReviewSuggestionRequest(action="accept"),
        current_user,
    )


@router.post("/{suggestion_id:path}/reject", response_model=MemorySuggestion)
async def reject_suggestion(
    suggestion_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> MemorySuggestion:
    return await _review_suggestion(
        suggestion_id,
        ReviewSuggestionRequest(action="reject"),
        current_user,
    )


@router.post("/{suggestion_id:path}/review", response_model=MemorySuggestion)
async def review_suggestion(
    suggestion_id: str,
    body: ReviewSuggestionRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemorySuggestion:
    return await _review_suggestion(suggestion_id, body, current_user)


async def _review_suggestion(
    suggestion_id: str,
    body: ReviewSuggestionRequest,
    current_user: CurrentUser,
) -> MemorySuggestion:
    async with sqlite.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        repo = SuggestionRepository(conn)
        try:
            suggestion = await repo.get_suggestion(suggestion_id)
            if suggestion is None or not _is_authorized_target(
                suggestion.target_id, current_user.wiki_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "detail": "Suggestion not found",
                        "code": "suggestion_not_found",
                    },
                )
            if suggestion.status != "pending":
                raise ValueError(
                    f"Suggestion '{suggestion_id}' is not pending and cannot be transitioned"
                )
            await _apply_review_effect(conn, suggestion, body, current_user)
            await repo.transition_suggestion(suggestion_id, body.action, commit=False)
            await conn.commit()
        except ValueError as exc:
            await conn.rollback()
            conflict = "is not pending" in str(exc)
            error_status = (
                status.HTTP_409_CONFLICT
                if conflict
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(
                status_code=error_status,
                detail={
                    "detail": str(exc),
                    "code": "suggestion_conflict" if conflict else "invalid_review_action",
                },
            ) from exc
        except KeyError as exc:
            await conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": str(exc), "code": "review_target_not_found"},
            ) from exc
        except HTTPException:
            await conn.rollback()
            raise
        except Exception:
            await conn.rollback()
            raise
        loaded = await repo.get_suggestion(suggestion_id)
        assert loaded is not None
        return loaded


async def _apply_review_effect(
    conn: Any,
    suggestion: MemorySuggestion,
    body: ReviewSuggestionRequest,
    current_user: CurrentUser,
) -> None:
    registry = MemoryAssetRegistry(conn, autocommit=False)
    if body.action in {"accept", "edit", "keep_both"}:
        if body.action == "edit" and body.edited_markdown is None:
            raise ValueError("Edit review actions require edited_markdown")
        await _register_suggestion_asset(
            registry,
            suggestion,
            current_user,
            supersedes=[],
            markdown=body.edited_markdown,
            scope=body.scope,
            visibility=body.visibility,
        )
        await _promote_proposed_objects(conn, suggestion, current_user)
        return
    if body.action in {"merge", "replace"}:
        supersedes = await _review_target_asset_ids(
            registry,
            suggestion,
            body,
            current_user.wiki_id,
        )
        if not supersedes:
            raise ValueError(
                f"{body.action.capitalize()} review actions require a target "
                "memory asset (asset_id, or a card with conflicts/duplicates)"
            )
        await _register_suggestion_asset(
            registry,
            suggestion,
            current_user,
            supersedes=supersedes,
            markdown=None,
            scope=body.scope,
            visibility=body.visibility,
        )
        await _promote_proposed_objects(conn, suggestion, current_user)
        for asset_id in supersedes:
            await registry.set_status_for_wiki(
                asset_id,
                current_user.wiki_id,
                "archived",
            )
        return
    if body.action == "retire":
        asset_ids = await _review_target_asset_ids(
            registry,
            suggestion,
            body,
            current_user.wiki_id,
        )
        if not asset_ids:
            raise ValueError(
                "Retire review actions require a target memory asset "
                "(asset_id, or a card with conflicts/duplicates)"
            )
        for asset_id in asset_ids:
            await registry.set_status_for_wiki(
                asset_id,
                current_user.wiki_id,
                "archived",
            )
        return
    if body.action == "change_scope":
        if body.asset_id is None or body.scope is None:
            raise ValueError("Scope review actions require asset_id and scope")
        await _require_review_asset(registry, body.asset_id, current_user.wiki_id)
        await registry.set_scope_for_wiki(
            body.asset_id,
            current_user.wiki_id,
            body.scope,
        )
        return
    if body.action == "change_visibility":
        if body.asset_id is None or body.visibility is None:
            raise ValueError("Visibility review actions require asset_id and visibility")
        await _require_review_asset(registry, body.asset_id, current_user.wiki_id)
        await registry.set_visibility_for_wiki(
            body.asset_id,
            current_user.wiki_id,
            body.visibility,
        )


async def _register_suggestion_asset(
    registry: MemoryAssetRegistry,
    suggestion: MemorySuggestion,
    current_user: CurrentUser,
    *,
    supersedes: list[str],
    markdown: str | None,
    scope: str | None = None,
    visibility: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    body = suggestion.proposed_markdown if markdown is None else markdown
    await registry.register_asset(
        id=_suggestion_asset_id(suggestion),
        wiki_id=current_user.wiki_id,
        asset_type="wiki",
        layer="L1",
        name=_suggestion_asset_name(suggestion, body),
        scope=scope or _suggestion_scope(suggestion, current_user.wiki_id),
        status="active",
        visibility=visibility or _suggestion_visibility(suggestion),
        # What is remembered, not why it was queued. The rationale is boilerplate
        # on everything distillation proposes, and it used to win over the
        # content — so a shelf of memories all described the review process
        # instead of themselves. It is kept, one field over.
        summary=_truncate(body, 160) or suggestion.rationale,
        body=body,
        tags=["suggestion", suggestion.suggestion_type],
        metadata={
            "suggestion_id": suggestion.id,
            "target_id": suggestion.target_id,
            "scores": suggestion.scores,
            "duplicates": suggestion.duplicates,
            "conflicts": suggestion.conflicts,
            "retention_tier": suggestion.retention_tier,
            "agent_visibility": suggestion.agent_visibility,
            "estimated_durability": suggestion.estimated_durability,
            "rationale": suggestion.rationale,
        },
        citations=_suggestion_citations(suggestion),
        approved_by=current_user.username,
        reviewed_at=now,
        supersedes=supersedes,
        conflict_lineage=[suggestion.id] if supersedes else [],
        change_note=f"Accepted review suggestion {suggestion.id}",
    )


async def _promote_proposed_objects(
    conn: Any,
    suggestion: MemorySuggestion,
    current_user: CurrentUser,
) -> None:
    """Write reviewed proposed objects into canonical knowledge as accepted.

    Promotion never crosses wiki boundaries: a proposed object is only
    written when both its own scope and any existing canonical object it
    would overwrite belong to the acting wiki. The owner scope
    (`person:self`) is personal memory, so only the owner can promote
    into or over it.

    Must run inside the caller's open transaction (`_review_suggestion`
    holds BEGIN IMMEDIATE); every upsert here uses commit=False so the
    review transition and canonical writes commit atomically.
    """
    repo = KnowledgeRepository(conn)
    allowed_scopes = {f"wiki:{current_user.wiki_id}"}
    if current_user.role == "owner":
        # person:self is a deployment-wide singleton: Archivum models one
        # human, and every owner-role account curates that same personal
        # memory across wikis. Supporting multiple distinct humans would
        # require namespacing the person scope, not a check here.
        allowed_scopes.add("person:self")
    for raw in suggestion.proposed_objects:
        if not isinstance(raw, dict):
            continue
        try:
            obj = KnowledgeObject.model_validate(raw)
        except ValidationError:
            continue
        if obj.scope not in allowed_scopes:
            continue
        existing = await repo.get_object(obj.id)
        if existing is not None and existing.scope not in allowed_scopes:
            continue
        obj.properties["review_state"] = "accepted"
        obj.properties["approved_by"] = current_user.username
        await repo.upsert_object(obj, commit=False)


def _suggestion_asset_id(suggestion: MemorySuggestion) -> str:
    return f"memory:suggestion:{suggestion.id}"


def _suggestion_asset_name(suggestion: MemorySuggestion, markdown: str) -> str:
    for line in markdown.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return _truncate(cleaned, 80)
    return suggestion.suggestion_type


def _suggestion_scope(suggestion: MemorySuggestion, wiki_id: str) -> str:
    if suggestion.proposed_scopes:
        return suggestion.proposed_scopes[0]
    if suggestion.target_id.startswith("wiki:"):
        return f"wiki:{wiki_id}"
    return "person:self"


def _suggestion_visibility(suggestion: MemorySuggestion) -> str:
    if suggestion.agent_visibility in {"private", "shared", "public"}:
        return suggestion.agent_visibility
    return "private"


def _suggestion_citations(suggestion: MemorySuggestion) -> list[Citation]:
    citations: list[Citation] = []
    for raw in suggestion.citations:
        if isinstance(raw, Citation):
            citations.append(raw)
        elif isinstance(raw, dict):
            try:
                citations.append(Citation.model_validate(raw))
            except ValueError:
                continue
    return citations


async def _review_target_asset_ids(
    registry: MemoryAssetRegistry,
    suggestion: MemorySuggestion,
    body: ReviewSuggestionRequest,
    wiki_id: str,
) -> list[str]:
    """Resolve the assets a merge/replace/retire acts on.

    An explicit asset_id must exist. Implicit candidates from the card's
    conflicts/duplicates may reference canonical records (e.g. atoms) that are
    not registered assets, so they are filtered rather than treated as errors.
    """
    asset_ids: list[str] = []
    if body.asset_id is not None:
        await _require_review_asset(registry, body.asset_id, wiki_id)
        asset_ids.append(body.asset_id)
    implicit: list[str] = []
    if suggestion.target_id.startswith("memory:"):
        implicit.append(suggestion.target_id)
    implicit.extend(suggestion.conflicts)
    implicit.extend(suggestion.duplicates)
    for candidate in implicit:
        if not candidate.startswith("memory:") or candidate in asset_ids:
            continue
        if await registry.get_asset_for_wiki(candidate, wiki_id) is not None:
            asset_ids.append(candidate)
    return asset_ids


async def _require_review_asset(
    registry: MemoryAssetRegistry,
    asset_id: str,
    wiki_id: str,
) -> None:
    asset = await registry.get_asset_for_wiki(asset_id, wiki_id)
    if asset is None:
        raise KeyError(f"Memory asset '{asset_id}' not found")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def _page_target_id(wiki_id: str, slug: str) -> str:
    return f"{_page_target_prefix(wiki_id)}{_validate_slug(slug)}"


def _page_target_prefix(wiki_id: str) -> str:
    return f"page:{wiki_id}:"


def _wiki_target_id(wiki_id: str) -> str:
    return f"wiki:{wiki_id}"


def _wiki_target_prefix(wiki_id: str) -> str:
    return f"wiki:{wiki_id}:"


def _is_authorized_target(target_id: str, wiki_id: str) -> bool:
    return (
        target_id == _wiki_target_id(wiki_id)
        or target_id.startswith(_wiki_target_prefix(wiki_id))
        or target_id.startswith(_page_target_prefix(wiki_id))
    )


def _require_authorized_target(target_id: str, wiki_id: str) -> None:
    if not _is_authorized_target(target_id, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Suggestion target is not authorized for this wiki",
                "code": "unauthorized_suggestion_target",
            },
        )
