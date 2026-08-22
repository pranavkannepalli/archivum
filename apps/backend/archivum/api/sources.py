"""Sources routes: /api/sources/* — deterministic ingestion + read-back."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.store.ingest import ingest_source, read_origin_bytes
from archivum.store.models import IngestResult, Source
from archivum.db import sqlite
from archivum.knowledge.repository import KnowledgeRepository
from archivum.store.repository import SourceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceIngestRequest(BaseModel):
    origin_uri: str
    scope: str = "personal"
    source_type: str | None = None


class SourceResponse(BaseModel):
    id: str
    content_hash: str
    version: int
    source_type: str
    origin_uri: str
    scope: str
    deduplicated: bool
    chunk_count: int


class DerivedRecord(BaseModel):
    id: str
    kind: str
    label: str
    slug: str | None = None
    confidence: float = 0.0


class DerivedResponse(BaseModel):
    source_id: str
    records: list[DerivedRecord]
    pages: int = 0


class SourceDetailResponse(BaseModel):
    source: SourceResponse
    chunk_count: int


async def _read_bytes(origin_uri: str) -> bytes:
    """Fetch the raw bytes for an origin (local file path/URI or http(s))."""
    try:
        return await read_origin_bytes(origin_uri)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": f"cannot read source: {origin_uri}", "code": "unreadable_source"},
        ) from exc


def _to_response(result: IngestResult) -> SourceResponse:
    return SourceResponse(
        id=result.source.id,
        content_hash=result.source.content_hash,
        version=result.source.version,
        source_type=result.source.source_type.value,
        origin_uri=result.source.origin_uri,
        scope=result.source.scope,
        deduplicated=result.deduplicated,
        chunk_count=len(result.chunks),
    )


@router.post("/ingest", response_model=SourceResponse)
async def ingest_endpoint(
    body: SourceIngestRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> SourceResponse:
    logger.info("API sources.ingest", extra={"origin_uri": body.origin_uri})
    raw_bytes = await _read_bytes(body.origin_uri)
    result = await ingest_source(
        origin_uri=body.origin_uri,
        raw_bytes=raw_bytes,
        scope=body.scope,
        wiki_id=current_user.wiki_id,
        explicit_type=body.source_type,
        settings=settings,
    )
    return _to_response(result)


@router.get("/{source_id:path}/derived", response_model=DerivedResponse)
async def source_derived(
    source_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> DerivedResponse:
    """What this source actually produced.

    Sources and pages looked unrelated because nothing joined them, even though
    ingest cites the source on every record it derives. This walks that
    provenance, so a source can answer for its own output.
    """
    scope = f"wiki:{current_user.wiki_id}"
    async with sqlite.get_db() as conn:
        objects = await KnowledgeRepository(conn).list_objects_from_source(
            source_id, scope=scope
        )

    records = [
        DerivedRecord(
            id=obj.id,
            kind=obj.kind,
            label=obj.label,
            slug=str(obj.properties.get("slug")) if obj.properties.get("slug") else None,
            confidence=obj.confidence,
        )
        for obj in objects
    ]
    return DerivedResponse(
        source_id=source_id,
        records=records,
        pages=sum(1 for record in records if record.kind == "page"),
    )


@router.get("/{source_id}", response_model=SourceDetailResponse)
async def get_source_endpoint(
    source_id: str,
    current_user: CurrentUser = Depends(require_writer),
) -> SourceDetailResponse:
    store = SourceStore()
    source: Source | None = await store.get_source(
        source_id, wiki_id=current_user.wiki_id
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": "source not found", "code": "source_not_found"},
        )
    document = await store.get_document_for_source(source.id)
    chunk_count = len(await store.list_chunks(document.id)) if document else 0
    return SourceDetailResponse(
        source=SourceResponse(
            id=source.id,
            content_hash=source.content_hash,
            version=source.version,
            source_type=source.source_type.value,
            origin_uri=source.origin_uri,
            scope=source.scope,
            deduplicated=False,
            chunk_count=chunk_count,
        ),
        chunk_count=chunk_count,
    )
