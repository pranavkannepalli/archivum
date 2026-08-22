"""Pages routes: /api/pages/*"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.db import sqlite, qdrant_client as qdrant, graph
from archivum.ingest.agent import slugify
from archivum.knowledge.repository import KnowledgeRepository
from archivum.indexing import (
    ensure_frontmatter,
    forget_page,
    reindex_page,
    repoint_page,
)
from archivum.pages_to_knowledge import rename_page_in_knowledge
from archivum.security.markdown import sanitize_markdown

router = APIRouter(prefix="/api/pages", tags=["pages"])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class PageSummary(BaseModel):
    slug: str
    title: str
    tags: list[str]
    created_at: str
    updated_at: str
    authored_by: str


class PageDetail(PageSummary):
    content: str
    id: int


class CreatePageRequest(BaseModel):
    title: str
    content: str = ""
    tags: list[str] = []
    slug: str | None = None


class UpdatePageRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None


class MovePageRequest(BaseModel):
    new_slug: str


class DuplicatePageRequest(BaseModel):
    new_slug: str
    title: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

_SLUG_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validate_slug(slug: str) -> str:
    """
    Allow folder-like slugs: "projects/archivum/notes".
    Disallow traversal and weird separators.
    """
    if not slug or slug.strip() != slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Invalid slug", "code": "invalid_slug"},
        )
    if slug.startswith("/") or slug.endswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Invalid slug", "code": "invalid_slug"},
        )
    if "\\" in slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Invalid slug", "code": "invalid_slug"},
        )

    parts = slug.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Invalid slug", "code": "invalid_slug"},
        )
    for p in parts:
        if not _SLUG_SEGMENT_RE.match(p):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"detail": f"Invalid slug segment '{p}'", "code": "invalid_slug"},
            )
    return slug


def _deserialize_tags(tags_raw: str | list) -> list[str]:
    if isinstance(tags_raw, list):
        return tags_raw
    try:
        return json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _row_to_summary(row: dict) -> PageSummary:
    return PageSummary(
        slug=row["slug"],
        title=row["title"],
        tags=_deserialize_tags(row["tags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        authored_by=row["authored_by"],
    )


def _row_to_detail(row: dict) -> PageDetail:
    return PageDetail(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        content=row["content"],
        tags=_deserialize_tags(row["tags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        authored_by=row["authored_by"],
    )


async def _ensure_parent_folders(slug: str, wiki_id: str) -> None:
    parts = slug.split("/")[:-1]
    acc = ""
    for part in parts:
        acc = f"{acc}/{part}" if acc else part
        await sqlite.upsert_folder(acc, wiki_id)


async def _rewrite_wikilinks(
    mapping: dict[str, str],
    wiki_id: str,
    settings: Settings,
) -> None:
    if not mapping:
        return

    rows = await sqlite.list_pages(wiki_id)
    for row in rows:
        detail = await sqlite.get_page(row["slug"], wiki_id)
        if not detail:
            continue
        content = detail["content"]
        rewritten = content
        for old_slug, new_slug in mapping.items():
            rewritten = rewritten.replace(f"[[{old_slug}]]", f"[[{new_slug}]]")
            rewritten = rewritten.replace(f"[[{old_slug}|", f"[[{new_slug}|")
        if rewritten == content:
            continue
        tags = _deserialize_tags(detail["tags"])
        wiki_path = settings.wiki_dir / f"{detail['slug']}.md"
        wiki_path.parent.mkdir(parents=True, exist_ok=True)
        wiki_path.write_text(rewritten, encoding="utf-8")
        await sqlite.upsert_page(
            slug=detail["slug"],
            title=detail["title"],
            content=rewritten,
            tags=tags,
            authored_by=detail["authored_by"],
            wiki_id=wiki_id,
        )
        # The file changed, so everything derived from it has to catch up. No
        # distillation: rewriting a link is not new thinking to remember.
        await reindex_page(
            detail["slug"],
            wiki_id=wiki_id,
            settings=settings,
            force=True,
            authored_by=detail["authored_by"],
            reason="wikilink-rewrite",
            distill=False,
        )


async def _rename_page_knowledge(
    old_slug: str, new_slug: str, title: str, content: str, wiki_id: str
) -> None:
    async with sqlite.get_db() as conn:
        await rename_page_in_knowledge(
            KnowledgeRepository(conn),
            old_slug=old_slug,
            new_slug=new_slug,
            title=title,
            markdown=content,
            wiki_id=wiki_id,
        )


async def move_page_to_slug(
    old_slug: str,
    new_slug: str,
    wiki_id: str,
    settings: Settings,
) -> dict:
    old_slug = _validate_slug(old_slug)
    new_slug = _validate_slug(new_slug)
    if old_slug == new_slug:
        row = await sqlite.get_page(old_slug, wiki_id)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"detail": f"Page '{old_slug}' not found", "code": "page_not_found"},
            )
        return row

    existing = await sqlite.get_page(old_slug, wiki_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{old_slug}' not found", "code": "page_not_found"},
        )
    if await sqlite.get_page(new_slug, wiki_id) or await sqlite.get_folder(new_slug, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": f"Slug '{new_slug}' already exists", "code": "slug_collision"},
        )

    await _ensure_parent_folders(new_slug, wiki_id)

    old_path = settings.wiki_dir / f"{old_slug}.md"
    new_path = settings.wiki_dir / f"{new_slug}.md"
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": f"Markdown file for '{new_slug}' already exists", "code": "file_collision"},
        )
    new_path.parent.mkdir(parents=True, exist_ok=True)
    if old_path.exists():
        old_path.rename(new_path)
    else:
        new_path.write_text(existing["content"], encoding="utf-8")

    await sqlite.update_page_slug(old_slug, new_slug, wiki_id)
    await repoint_page(old_slug=old_slug, new_slug=new_slug, wiki_id=wiki_id)
    await _rename_page_knowledge(
        old_slug, new_slug, existing["title"], existing["content"], wiki_id
    )
    await sqlite.update_share_targets({old_slug: new_slug}, wiki_id)
    await qdrant.delete_page(old_slug, wiki_id, settings)
    await graph.rename_page_node(old_slug, new_slug, wiki_id)
    await reindex_page(
        new_slug,
        wiki_id=wiki_id,
        settings=settings,
        force=True,
        reason="rename",
        distill=False,
    )
    await _rewrite_wikilinks({old_slug: new_slug}, wiki_id, settings)

    row = await sqlite.get_page(new_slug, wiki_id)
    return row  # type: ignore[return-value]


async def duplicate_page_to_slug(
    source_slug: str,
    new_slug: str,
    title: str | None,
    wiki_id: str,
    settings: Settings,
) -> dict:
    source_slug = _validate_slug(source_slug)
    new_slug = _validate_slug(new_slug)
    source = await sqlite.get_page(source_slug, wiki_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{source_slug}' not found", "code": "page_not_found"},
        )
    if await sqlite.get_page(new_slug, wiki_id) or await sqlite.get_folder(new_slug, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": f"Slug '{new_slug}' already exists", "code": "slug_collision"},
        )

    await _ensure_parent_folders(new_slug, wiki_id)
    duplicate_title = title or f"{source['title']} copy"
    content = source["content"]
    tags = _deserialize_tags(source["tags"])
    wiki_path = settings.wiki_dir / f"{new_slug}.md"
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(content, encoding="utf-8")

    await reindex_page(
        new_slug,
        wiki_id=wiki_id,
        settings=settings,
        force=True,
        reason="duplicate",
    )
    row = await sqlite.get_page(new_slug, wiki_id)
    return row  # type: ignore[return-value]


# ── Routes ────────────────────────────────────────────────────────────────────

class ReindexResponse(BaseModel):
    """What a reindex did, including anything it could not reach."""

    slug: str | None = None
    action: str
    degraded: list[str] = Field(default_factory=list)
    pages: int = 0


@router.post("/{slug:path}/reindex", response_model=ReindexResponse)
async def reindex_one_page(
    slug: str,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> ReindexResponse:
    """Re-read this page from disk and rebuild everything derived from it.

    The vault is editable by hand, so this is the manual counterpart to the
    watcher: the file is the truth and the indexes are told to catch up.
    """
    slug = _validate_slug(slug)
    result = await reindex_page(
        slug,
        wiki_id=current_user.wiki_id,
        settings=settings,
        force=True,
        reason="manual",
    )
    if result.action == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"No markdown file for '{slug}'", "code": "page_not_found"},
        )
    return ReindexResponse(
        slug=result.slug, action=result.action, degraded=result.degraded
    )


@router.get("", response_model=list[PageSummary])
async def list_pages(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[PageSummary]:
    rows = await sqlite.list_pages(current_user.wiki_id)
    return [_row_to_summary(r) for r in rows]


@router.get("/{slug:path}/backlinks", response_model=list[dict])
async def get_backlinks(
    slug: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    slug = _validate_slug(slug)
    # Verify page exists
    existing = await sqlite.get_page(slug, current_user.wiki_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{slug}' not found", "code": "page_not_found"},
        )
    return await graph.get_backlinks(slug, current_user.wiki_id)


@router.patch("/{slug:path}/move", response_model=PageDetail)
async def move_page(
    slug: str,
    body: MovePageRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> PageDetail:
    row = await move_page_to_slug(slug, body.new_slug, current_user.wiki_id, settings)
    return _row_to_detail(row)


@router.post("/{slug:path}/duplicate", response_model=PageDetail, status_code=status.HTTP_201_CREATED)
async def duplicate_page(
    slug: str,
    body: DuplicatePageRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> PageDetail:
    row = await duplicate_page_to_slug(slug, body.new_slug, body.title, current_user.wiki_id, settings)
    return _row_to_detail(row)


@router.get("/{slug:path}", response_model=PageDetail)
async def get_page(
    slug: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> PageDetail:
    slug = _validate_slug(slug)
    row = await sqlite.get_page(slug, current_user.wiki_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{slug}' not found", "code": "page_not_found"},
        )
    return _row_to_detail(row)


@router.post("", response_model=PageDetail, status_code=status.HTTP_201_CREATED)
async def create_page(
    body: CreatePageRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> PageDetail:
    logger.info(
        "API create_page",
        extra={"wiki_id": current_user.wiki_id, "title_chars": len(body.title or ""), "content_chars": len(body.content or "")},
    )
    raw_content = body.content or ""
    clean_content = sanitize_markdown(raw_content)

    # Derive slug
    slug = body.slug or slugify(body.title)
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Could not generate slug from title", "code": "invalid_title"},
        )
    slug = _validate_slug(slug)

    # Ensure uniqueness
    base_slug = slug
    counter = 2
    while await sqlite.get_page(slug, current_user.wiki_id):
        slug = f"{base_slug}-{counter}"
        counter += 1

    # Write the file, metadata and all, then let the one indexing path derive
    # everything else from it. The API used to fan out to four stores by hand,
    # which is how they drifted apart.
    stored = ensure_frontmatter(clean_content, title=body.title, tags=body.tags)
    wiki_path = settings.wiki_dir / f"{slug}.md"
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(stored, encoding="utf-8")

    await reindex_page(
        slug,
        wiki_id=current_user.wiki_id,
        settings=settings,
        force=True,
        authored_by="user",
        reason="create",
    )

    row = await sqlite.get_page(slug, current_user.wiki_id)
    return _row_to_detail(row)  # type: ignore[arg-type]


@router.put("/{slug:path}", response_model=PageDetail)
async def update_page(
    slug: str,
    body: UpdatePageRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> PageDetail:
    slug = _validate_slug(slug)
    logger.info(
        "API update_page",
        extra={"wiki_id": current_user.wiki_id, "slug": slug, "has_title": body.title is not None, "has_content": body.content is not None},
    )
    existing = await sqlite.get_page(slug, current_user.wiki_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{slug}' not found", "code": "page_not_found"},
        )

    new_title = body.title if body.title is not None else existing["title"]
    new_content_raw = body.content if body.content is not None else existing["content"]
    new_content = sanitize_markdown(new_content_raw)
    new_tags = body.tags if body.tags is not None else _deserialize_tags(existing["tags"])

    stored = ensure_frontmatter(new_content, title=new_title, tags=new_tags)
    wiki_path = settings.wiki_dir / f"{slug}.md"
    wiki_path.write_text(stored, encoding="utf-8")

    await reindex_page(
        slug,
        wiki_id=current_user.wiki_id,
        settings=settings,
        force=True,
        authored_by="user",
        reason="update",
    )

    row = await sqlite.get_page(slug, current_user.wiki_id)
    return _row_to_detail(row)  # type: ignore[arg-type]


@router.delete("/{slug:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    slug: str,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> None:
    slug = _validate_slug(slug)
    logger.info("API delete_page", extra={"wiki_id": current_user.wiki_id, "slug": slug})
    existing = await sqlite.get_page(slug, current_user.wiki_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Page '{slug}' not found", "code": "page_not_found"},
        )

    wiki_path = settings.wiki_dir / f"{slug}.md"
    wiki_path.unlink(missing_ok=True)

    await forget_page(slug, wiki_id=current_user.wiki_id, settings=settings)
