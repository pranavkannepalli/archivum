"""Memory asset registry, distillation, and agent loadout routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.db import qdrant_client as qdrant
from archivum.db import sqlite
from archivum.indexing import ensure_frontmatter, reindex_page
from archivum.knowledge.models import Citation
from archivum.knowledge.repository import KnowledgeRepository
from archivum.knowledge.suggestions import SuggestionRepository
from archivum.memory.catalog import sync_catalog
from archivum.memory.loadouts import resolve_loadout
from archivum.memory.models import (
    AgentProfile,
    AssetBinding,
    LoadoutPackage,
    MemoryAsset,
    MemoryAssetVersion,
    MemoryScope,
)
from archivum.memory.registry import MemoryAssetRegistry
from archivum.memory.service import (
    DistillationError,
    DistillationReport,
    activate_asset,
    distill_conversation,
    load_conversation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])


# ── Request/response models ───────────────────────────────────────────────


class RegisterAssetRequest(BaseModel):
    id: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    layer: str = "L1"
    name: str = Field(min_length=1)
    summary: str = ""
    body: str = ""
    page_slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    approved_by: str | None = None
    reviewed_at: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    conflict_lineage: list[str] = Field(default_factory=list)
    retired_at: str | None = None
    status: str = "draft"
    visibility: str = "private"
    change_note: str = ""


class AssetStatusRequest(BaseModel):
    status: str = Field(min_length=1)


class AssetVisibilityRequest(BaseModel):
    visibility: str = Field(min_length=1)


class ScopeRequest(BaseModel):
    id: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parent_scope_id: str | None = None
    budget_tokens: int = Field(default=4_000, ge=0)
    budget_items: int = Field(default=20, ge=0)
    retention_policy: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    agent_key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""


class BindingRequest(BaseModel):
    asset_id: str = Field(min_length=1)
    mode: str = "always"
    priority: int = Field(default=100, ge=0, le=10_000)


class DistillRequest(BaseModel):
    source_id: str = Field(min_length=1)
    scenario_key: str | None = None
    scenario_name: str | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    write_pages: bool | None = None


class CatalogResponse(BaseModel):
    wiki_assets: int
    source_assets: int
    codegraph_assets: int
    asset_ids: list[str]


class DistillResponse(BaseModel):
    source_id: str
    session_id: str
    scope: str
    atoms_total: int
    atoms_accepted: int
    atoms_pending_review: int
    conflicts_flagged: int
    sentences_scanned: int
    asset_ids: list[str]
    scenario_id: str | None
    persona_updated: bool
    skill_id: str | None
    skill_reason: str | None
    pages_written: list[str]


# ── Assets ────────────────────────────────────────────────────────────────


@router.get("/scopes", response_model=list[MemoryScope])
async def list_scopes(
    scope_type: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MemoryScope]:
    async with sqlite.get_db() as conn:
        return await MemoryAssetRegistry(conn).list_scopes(
            current_user.wiki_id, scope_type=scope_type
        )


@router.post("/scopes", response_model=MemoryScope, status_code=status.HTTP_201_CREATED)
async def upsert_scope(
    body: ScopeRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemoryScope:
    async with sqlite.get_db() as conn:
        try:
            return await MemoryAssetRegistry(conn).upsert_scope(
                id=body.id,
                wiki_id=current_user.wiki_id,
                scope_type=body.scope_type,
                name=body.name,
                parent_scope_id=body.parent_scope_id,
                budget_tokens=body.budget_tokens,
                budget_items=body.budget_items,
                retention_policy=body.retention_policy,
            )
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_memory_scope") from exc


class MemoryStats(BaseModel):
    """Vault-wide counts behind the memory pipeline view.

    Raw counts only: the funnel in the UI is candidates -> kept -> live -> off,
    and each of those is a real number from this payload rather than a ratio
    computed here.
    """

    suggestions_total: int = 0
    suggestions_pending: int = 0
    suggestions_kept: int = 0
    suggestions_dropped: int = 0
    suggestions_by_status: dict[str, int] = Field(default_factory=dict)
    assets_total: int = 0
    assets_active: int = 0
    assets_draft: int = 0
    assets_archived: int = 0
    assets_disputed: int = 0
    assets_by_layer: dict[str, int] = Field(default_factory=dict)


# Review outcomes that mean the claim earned a place in memory, versus the ones
# that mean it did not. `pending` is neither: it is still waiting on a human.
_KEPT_STATUSES = {"accepted", "edited", "merged", "replaced", "kept"}
_DROPPED_STATUSES = {"rejected", "retired", "expired"}


@router.get("/stats", response_model=MemoryStats)
async def memory_stats(
    current_user: CurrentUser = Depends(get_current_user),
) -> MemoryStats:
    async with sqlite.get_db() as conn:
        suggestion_counts = await SuggestionRepository(conn).suggestion_counts(
            wiki_id=current_user.wiki_id
        )
        asset_counts = await MemoryAssetRegistry(conn).asset_counts(
            wiki_id=current_user.wiki_id
        )

    by_status = asset_counts["by_status"]
    return MemoryStats(
        suggestions_total=sum(suggestion_counts.values()),
        suggestions_pending=suggestion_counts.get("pending", 0),
        suggestions_kept=sum(
            n for status, n in suggestion_counts.items() if status in _KEPT_STATUSES
        ),
        suggestions_dropped=sum(
            n for status, n in suggestion_counts.items() if status in _DROPPED_STATUSES
        ),
        suggestions_by_status=suggestion_counts,
        assets_total=asset_counts["total"],
        assets_active=by_status.get("active", 0),
        assets_draft=by_status.get("draft", 0),
        assets_archived=by_status.get("archived", 0),
        assets_disputed=asset_counts["disputed"],
        assets_by_layer=asset_counts["by_layer"],
    )


@router.get("/assets", response_model=list[MemoryAsset])
async def list_assets(
    asset_type: str | None = Query(default=None),
    layer: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    owner: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    page_slug: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MemoryAsset]:
    async with sqlite.get_db() as conn:
        return await MemoryAssetRegistry(conn).list_assets(
            wiki_id=current_user.wiki_id,
            asset_type=asset_type,
            layer=layer,
            status=status_filter,
            owner=owner,
            scope=scope,
            page_slug=page_slug,
            limit=limit,
        )


@router.post("/assets", response_model=MemoryAsset, status_code=status.HTTP_201_CREATED)
async def register_asset(
    body: RegisterAssetRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemoryAsset:
    async with sqlite.get_db() as conn:
        try:
            return await MemoryAssetRegistry(conn).register_asset(
                id=body.id,
                wiki_id=current_user.wiki_id,
                asset_type=body.asset_type,
                layer=body.layer,
                name=body.name,
                scope=f"wiki:{current_user.wiki_id}",
                status=body.status,
                visibility=body.visibility,
                page_slug=body.page_slug,
                summary=body.summary,
                body=body.body,
                tags=body.tags,
                metadata=body.metadata,
                citations=body.citations,
                approved_by=body.approved_by,
                reviewed_at=body.reviewed_at,
                supersedes=body.supersedes,
                superseded_by=body.superseded_by,
                conflict_lineage=body.conflict_lineage,
                retired_at=body.retired_at,
                change_note=body.change_note,
            )
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_memory_asset") from exc


@router.get("/assets/{asset_id:path}/versions", response_model=list[MemoryAssetVersion])
async def list_asset_versions(
    asset_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MemoryAssetVersion]:
    async with sqlite.get_db() as conn:
        registry = MemoryAssetRegistry(conn)
        await _require_asset(registry, asset_id, current_user.wiki_id)
        return await registry.list_versions(asset_id)


@router.post("/assets/{asset_id:path}/status", response_model=MemoryAsset)
async def set_asset_status(
    asset_id: str,
    body: AssetStatusRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemoryAsset:
    async with sqlite.get_db() as conn:
        registry = MemoryAssetRegistry(conn)
        await _require_asset(registry, asset_id, current_user.wiki_id)
        try:
            if body.status == "active":
                return await activate_asset(
                    conn,
                    KnowledgeRepository(conn),
                    asset_id,
                    approved_by=current_user.username,
                )
            return await registry.set_status(asset_id, body.status)
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_asset_status") from exc


@router.post("/assets/{asset_id:path}/visibility", response_model=MemoryAsset)
async def set_asset_visibility(
    asset_id: str,
    body: AssetVisibilityRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> MemoryAsset:
    async with sqlite.get_db() as conn:
        registry = MemoryAssetRegistry(conn)
        await _require_asset(registry, asset_id, current_user.wiki_id)
        try:
            return await registry.set_visibility(asset_id, body.visibility)
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_asset_visibility") from exc


@router.get("/assets/{asset_id:path}", response_model=MemoryAsset)
async def get_asset(
    asset_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> MemoryAsset:
    async with sqlite.get_db() as conn:
        return await _require_asset(
            MemoryAssetRegistry(conn), asset_id, current_user.wiki_id
        )


@router.post("/catalog", response_model=CatalogResponse)
async def catalog(
    current_user: CurrentUser = Depends(require_writer),
) -> CatalogResponse:
    """Register existing pages, sources, and code graphs as governed assets."""
    async with sqlite.get_db() as conn:
        report = await sync_catalog(conn, wiki_id=current_user.wiki_id)
    return CatalogResponse(
        wiki_assets=report.wiki_assets,
        source_assets=report.source_assets,
        codegraph_assets=report.codegraph_assets,
        asset_ids=report.asset_ids,
    )


# ── Agents and loadouts ───────────────────────────────────────────────────


@router.get("/agents", response_model=list[AgentProfile])
async def list_agents(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[AgentProfile]:
    async with sqlite.get_db() as conn:
        return await MemoryAssetRegistry(conn).list_agents(current_user.wiki_id)


@router.post("/agents", response_model=AgentProfile, status_code=status.HTTP_201_CREATED)
async def upsert_agent(
    body: AgentRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> AgentProfile:
    async with sqlite.get_db() as conn:
        return await MemoryAssetRegistry(conn).upsert_agent(
            agent_key=body.agent_key,
            wiki_id=current_user.wiki_id,
            name=body.name,
            description=body.description,
        )


@router.get("/agents/{agent_key}/bindings", response_model=list[AssetBinding])
async def list_bindings(
    agent_key: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[AssetBinding]:
    async with sqlite.get_db() as conn:
        return await MemoryAssetRegistry(conn).list_bindings(
            agent_key=agent_key, wiki_id=current_user.wiki_id
        )


@router.post(
    "/agents/{agent_key}/bindings",
    response_model=AssetBinding,
    status_code=status.HTTP_201_CREATED,
)
async def bind_asset(
    agent_key: str,
    body: BindingRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> AssetBinding:
    async with sqlite.get_db() as conn:
        try:
            return await MemoryAssetRegistry(conn).bind_asset(
                agent_key=agent_key,
                wiki_id=current_user.wiki_id,
                asset_id=body.asset_id,
                mode=body.mode,
                priority=body.priority,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": str(exc), "code": "binding_target_not_found"},
            ) from exc
        except ValueError as exc:
            raise _bad_request(str(exc), "invalid_binding_mode") from exc


@router.delete("/agents/{agent_key}/bindings/{asset_id:path}")
async def unbind_asset(
    agent_key: str,
    asset_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> dict[str, bool]:
    async with sqlite.get_db() as conn:
        removed = await MemoryAssetRegistry(conn).unbind_asset(
            agent_key=agent_key, wiki_id=current_user.wiki_id, asset_id=asset_id
        )
    return {"removed": removed}


@router.get("/agents/{agent_key}/loadout", response_model=LoadoutPackage)
async def get_loadout(
    agent_key: str,
    query: str = Query(default=""),
    limit: int = Query(default=12, ge=1, le=50),
    current_user: CurrentUser = Depends(get_current_user),
) -> LoadoutPackage:
    async with sqlite.get_db() as conn:
        return await resolve_loadout(
            MemoryAssetRegistry(conn),
            agent_key=agent_key,
            wiki_id=current_user.wiki_id,
            query=query,
            limit=limit,
        )


# ── Distillation ──────────────────────────────────────────────────────────


@router.post("/distill", response_model=DistillResponse)
async def distill(
    body: DistillRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> DistillResponse:
    """Promote a captured conversation into layered, cited memory."""
    try:
        loaded = await load_conversation(
            body.source_id, wiki_id=current_user.wiki_id, settings=settings
        )
    except DistillationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": str(exc), "code": "source_not_distillable"},
        ) from exc

    write_pages = (
        settings.memory_page_views_enabled if body.write_pages is None else body.write_pages
    )
    async with sqlite.get_db() as conn:
        report = await distill_conversation(
            conn,
            loaded,
            wiki_id=current_user.wiki_id,
            threshold=(
                body.threshold
                if body.threshold is not None
                else settings.memory_atom_confidence_threshold
            ),
            scenario_key=body.scenario_key,
            scenario_name=body.scenario_name,
            persona_min_sessions=settings.memory_persona_min_sessions,
            skill_min_tool_calls=settings.memory_skill_min_tool_calls,
            page_writer=(
                _page_writer(current_user.wiki_id, settings) if write_pages else None
            ),
        )
    return _to_distill_response(report)


def _to_distill_response(report: DistillationReport) -> DistillResponse:
    return DistillResponse(
        source_id=report.source_id,
        session_id=report.session_id,
        scope=report.scope,
        atoms_total=report.atoms_total,
        atoms_accepted=report.atoms_accepted,
        atoms_pending_review=report.atoms_pending_review,
        conflicts_flagged=report.conflicts_flagged,
        sentences_scanned=report.sentences_scanned,
        asset_ids=report.asset_ids,
        scenario_id=report.scenario_id,
        persona_updated=report.persona_updated,
        skill_id=report.skill_id,
        skill_reason=report.skill_reason,
        pages_written=report.pages_written,
    )


def _page_writer(wiki_id: str, settings: Settings):
    """Keep distilled memory editable as ordinary markdown pages."""

    async def write(slug: str, title: str, markdown: str, tags: list[str]) -> None:
        path = settings.wiki_dir / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ensure_frontmatter(markdown, title=title, tags=tags), encoding="utf-8")
        # Same indexing path as a page you write by hand, minus distillation:
        # this page came *out* of memory, so feeding it back in would loop.
        result = await reindex_page(
            slug,
            wiki_id=wiki_id,
            settings=settings,
            force=True,
            authored_by="agent",
            reason="memory-page",
            distill=False,
        )
        if result.degraded:
            logger.warning(
                "Memory page index degraded",
                extra={"slug": slug, "degraded": ",".join(result.degraded)},
            )

    return write


# ── Helpers ───────────────────────────────────────────────────────────────


async def _require_asset(
    registry: MemoryAssetRegistry, asset_id: str, wiki_id: str
) -> MemoryAsset:
    asset = await registry.get_asset(asset_id)
    if asset is None or asset.wiki_id != wiki_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "Memory asset not found", "code": "asset_not_found"},
        )
    return asset


def _bad_request(detail: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"detail": detail, "code": code},
    )
