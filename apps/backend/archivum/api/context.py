"""Agent-facing scoped context and hybrid retrieval endpoints."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from archivum.code_repos import REPO_SCOPE_PREFIX, owned_repo_scopes
from archivum.auth import CurrentUser, get_current_user
from archivum.config import Settings, get_settings
from archivum.db import sqlite
from archivum.knowledge.models import Citation, ContextPackage, ExtractionMethod
from archivum.knowledge.repository import KnowledgeRepository
from archivum.retrieval.context import ContextRequest, build_context_package
from archivum.retrieval.hybrid import hybrid_retrieve

router = APIRouter(prefix="/api", tags=["context"])


class ContextPackageRequest(BaseModel):
    query: str = ""
    scope: str | None = None
    source_type: str | None = None
    depth: int = Field(default=2, ge=0, le=6)
    max_nodes: int = Field(default=10, ge=1, le=50)
    relations: list[str] | None = None
    seed_ids: list[str] | None = None

    def to_context_request(self, scope: str, wiki_id: str) -> ContextRequest:
        return ContextRequest(
            query=self.query,
            scope=scope,
            wiki_id=wiki_id,
            source_type=self.source_type,
            depth=self.depth,
            max_nodes=self.max_nodes,
            relations=self.relations,
            seed_ids=self.seed_ids,
        )


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)


class RetrievalHitResponse(BaseModel):
    id: str
    label: str
    score: float
    source: str
    citation: Citation
    citations: list[Citation]
    extraction_method: ExtractionMethod | Literal["DERIVED"] | None
    confidence: float | None
    provenance: Literal["canonical", "derived"]


class RetrieveResponse(BaseModel):
    query: str
    hits: list[RetrievalHitResponse]
    citations: list[Citation]
    insufficient_evidence: bool
    reason: str | None


@router.post("/context-package", response_model=ContextPackage)
async def context_package(
    body: ContextPackageRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ContextPackage:
    """Build a bounded cited subgraph for an authenticated wiki."""
    request = await _context_request_for_user(body, current_user)
    async with sqlite.get_db() as connection:
        return await build_context_package(
            KnowledgeRepository(connection), request
        )


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    body: RetrieveRequest,
    current_user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> RetrieveResponse:
    """Return compact, cited hybrid evidence for an authenticated wiki."""
    query = body.query.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Query cannot be empty", "code": "empty_query"},
        )
    hits = await hybrid_retrieve(
        query, current_user.wiki_id, limit=body.limit, settings=settings
    )
    citations = _unique_citations(
        citation for hit in hits for citation in _evidence_citations(hit)
    )
    return RetrieveResponse(
        query=query,
        hits=[
            RetrievalHitResponse(
                id=hit.id,
                label=hit.label,
                score=hit.score,
                source=hit.source,
                citation=hit.citation,
                citations=list(hit.citations),
                **_provenance_payload(hit),
            )
            for hit in hits
        ],
        citations=citations,
        insufficient_evidence=not bool(citations),
        reason=(
            "No cited knowledge matched the retrieval query." if not citations else None
        ),
    )


def _provenance_payload(hit) -> dict[str, ExtractionMethod | str | float | None]:
    if hit.provenance == "canonical":
        return {
            "extraction_method": hit.extraction_method,
            "confidence": hit.confidence,
            "provenance": "canonical",
        }
    return {
        "extraction_method": "DERIVED",
        "confidence": None,
        "provenance": "derived",
    }


async def _context_request_for_user(
    body: ContextPackageRequest, current_user: CurrentUser
) -> ContextRequest:
    default_scope = f"wiki:{current_user.wiki_id}"
    allowed_scopes = {default_scope}
    # person:self is the owner's personal memory; collaborators only see
    # their wiki's scope.
    if current_user.role == "owner":
        allowed_scopes.add("person:self")

    requested_scope = body.scope.strip() if body.scope is not None else None
    # A repository is authorised by having been registered, since code scopes
    # carry no wiki id. Without this an agent could ask for code context over
    # MCP but was refused the same request over REST. Only consulted when a
    # repository was actually asked for, so the ordinary path stays one query.
    if requested_scope and requested_scope.startswith(REPO_SCOPE_PREFIX):
        allowed_scopes |= await owned_repo_scopes(wiki_id=current_user.wiki_id)

    if requested_scope and requested_scope not in allowed_scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Context scope is not authorized for this wiki",
                "code": "unauthorized_context_scope",
            },
        )
    return body.to_context_request(
        requested_scope or default_scope, current_user.wiki_id
    )


def _evidence_citations(hit) -> tuple[Citation, ...]:
    if hit.provenance == "canonical":
        return hit.citations
    return (hit.citation,)


def _unique_citations(citations: Iterable[Citation]) -> list[Citation]:
    unique: list[Citation] = []
    seen: set[tuple[str, str, int | None, int | None, str | None]] = set()
    for citation in citations:
        key = (
            citation.source_id,
            citation.chunk_id,
            citation.span_start,
            citation.span_end,
            citation.quote,
        )
        if key not in seen:
            seen.add(key)
            unique.append(citation)
    return unique
