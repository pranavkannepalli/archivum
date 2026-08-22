"""Folder routes: /api/folders/*"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.indexing import forget_page, reindex_page, repoint_page
from archivum.db import graph, qdrant_client as qdrant, sqlite
from archivum.security.markdown import sanitize_markdown

router = APIRouter(prefix="/api/folders", tags=["folders"])


class Folder(BaseModel):
    path: str
    name: str
    created_at: str
    updated_at: str


class CreateFolderRequest(BaseModel):
    path: str


class MoveFolderRequest(BaseModel):
    new_path: str | None = None
    name: str | None = None
    recursive: bool = False


class FolderMutationResult(BaseModel):
    path: str
    pages: int
    folders: int


_PATH_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _invalid_path() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"detail": "Invalid folder path", "code": "invalid_path"},
    )


def validate_folder_path(path: str) -> str:
    if not path or path.strip() != path:
        raise _invalid_path()
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        raise _invalid_path()
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise _invalid_path()
    if any(not _PATH_SEGMENT_RE.match(p) for p in parts):
        raise _invalid_path()
    return path


async def _ensure_parent_folders(path: str, wiki_id: str) -> None:
    parts = path.split("/")[:-1]
    acc = ""
    for part in parts:
        acc = f"{acc}/{part}" if acc else part
        await sqlite.upsert_folder(acc, wiki_id)


def _join_parent(path: str, name: str) -> str:
    parent = path.split("/")[:-1]
    return "/".join([*parent, name]) if parent else name


def _remap_slug(slug: str, old_prefix: str, new_prefix: str) -> str:
    suffix = slug[len(old_prefix):]
    return f"{new_prefix}{suffix}"


async def _rewrite_wikilinks(
    mapping: dict[str, str],
    wiki_id: str,
    settings: Settings,
) -> None:
    if not mapping:
        return
    for row in await sqlite.list_pages(wiki_id):
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
        rewritten = sanitize_markdown(rewritten)
        wiki_path = settings.wiki_dir / f"{detail['slug']}.md"
        wiki_path.parent.mkdir(parents=True, exist_ok=True)
        wiki_path.write_text(rewritten, encoding="utf-8")
        # One indexing path. This used to upsert the row and the embedding only,
        # so a folder rename left the graph pointing at the old link targets.
        await reindex_page(
            detail["slug"],
            wiki_id=wiki_id,
            settings=settings,
            force=True,
            authored_by=detail["authored_by"],
            reason="folder-wikilink-rewrite",
            distill=False,
        )


def _deserialize_tags(tags_raw: str | list) -> list[str]:
    if isinstance(tags_raw, list):
        return tags_raw
    import json

    try:
        return json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        return []


async def move_folder_tree(
    old_path: str,
    new_path: str,
    recursive: bool,
    wiki_id: str,
    settings: Settings,
) -> dict[str, int | str]:
    old_path = validate_folder_path(old_path)
    new_path = validate_folder_path(new_path)
    if old_path == new_path:
        folders = await sqlite.list_folders_under(old_path, wiki_id)
        pages = await sqlite.list_pages_under(old_path, wiki_id)
        return {"path": new_path, "pages": len(pages), "folders": len(folders)}
    if new_path.startswith(f"{old_path}/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Cannot move a folder inside itself", "code": "invalid_move"},
        )
    if not await sqlite.get_folder(old_path, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Folder '{old_path}' not found", "code": "folder_not_found"},
        )
    if await sqlite.get_folder(new_path, wiki_id) or await sqlite.get_page(new_path, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": f"Path '{new_path}' already exists", "code": "path_collision"},
        )

    child_folders = await sqlite.list_folders_under(old_path, wiki_id)
    pages = await sqlite.list_pages_under(old_path, wiki_id)
    if not recursive and (len(child_folders) > 1 or pages):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": "Folder is not empty", "code": "folder_not_empty"},
        )

    await _ensure_parent_folders(new_path, wiki_id)
    mapping = {p["slug"]: _remap_slug(p["slug"], old_path, new_path) for p in pages}

    for old_slug, new_slug in mapping.items():
        old_file = settings.wiki_dir / f"{old_slug}.md"
        new_file = settings.wiki_dir / f"{new_slug}.md"
        if old_file.exists():
            new_file.parent.mkdir(parents=True, exist_ok=True)
            old_file.rename(new_file)

    for old_slug, new_slug in mapping.items():
        page = next((p for p in pages if p["slug"] == old_slug), None)
        await sqlite.update_page_slug(old_slug, new_slug, wiki_id)
        await sqlite.update_share_targets({old_slug: new_slug}, wiki_id)
        await qdrant.delete_page(old_slug, wiki_id, settings)
        await graph.rename_page_node(old_slug, new_slug, wiki_id)
        await repoint_page(old_slug=old_slug, new_slug=new_slug, wiki_id=wiki_id)
        if page:
            await reindex_page(
                new_slug,
                wiki_id=wiki_id,
                settings=settings,
                force=True,
                reason="folder-rename",
                distill=False,
            )

    folder_count = await sqlite.move_folder_paths(old_path, new_path, wiki_id)
    await _rewrite_wikilinks(mapping, wiki_id, settings)
    return {"path": new_path, "pages": len(mapping), "folders": folder_count}


async def delete_folder_tree(
    path: str,
    recursive: bool,
    wiki_id: str,
    settings: Settings,
) -> dict[str, int | str]:
    path = validate_folder_path(path)
    if not await sqlite.get_folder(path, wiki_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Folder '{path}' not found", "code": "folder_not_found"},
        )

    child_folders = await sqlite.list_folders_under(path, wiki_id)
    pages = await sqlite.list_pages_under(path, wiki_id)
    if not recursive and (len(child_folders) > 1 or pages):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": "Folder is not empty", "code": "folder_not_empty"},
        )

    # Delete each page the same way deleting it on its own would. This used to
    # drop the file, the embedding and the graph node by hand and then bulk-
    # delete the rows, which skipped canonical knowledge, memory assets and the
    # review queue — and because the row was already gone, neither the vault
    # watcher nor the reconcile pass could ever repair the leftovers.
    for page in pages:
        slug = page["slug"]
        (settings.wiki_dir / f"{slug}.md").unlink(missing_ok=True)
        await forget_page(slug, wiki_id=wiki_id, settings=settings)

    # Any row still under the path had no file to forget; drop it so an empty
    # folder cannot leave a page behind.
    await sqlite.delete_pages_under(path, wiki_id)
    folder_count = await sqlite.delete_folders_under(path, wiki_id)
    return {"path": path, "pages": len(pages), "folders": folder_count}


@router.get("", response_model=list[Folder])
async def list_folders(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[Folder]:
    return [Folder(**row) for row in await sqlite.list_folders(current_user.wiki_id)]


@router.post("", response_model=Folder, status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: CreateFolderRequest,
    current_user: CurrentUser = Depends(require_writer),
) -> Folder:
    path = validate_folder_path(body.path)
    if await sqlite.get_folder(path, current_user.wiki_id) or await sqlite.get_page(path, current_user.wiki_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": f"Path '{path}' already exists", "code": "path_collision"},
        )
    await _ensure_parent_folders(path, current_user.wiki_id)
    row = await sqlite.create_folder(path, current_user.wiki_id)
    return Folder(**row)


@router.patch("/{path:path}", response_model=FolderMutationResult)
async def move_or_rename_folder(
    path: str,
    body: MoveFolderRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> FolderMutationResult:
    path = validate_folder_path(path)
    if body.new_path:
        new_path = validate_folder_path(body.new_path)
    elif body.name:
        new_path = validate_folder_path(_join_parent(path, body.name))
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "new_path or name is required", "code": "missing_target"},
        )
    result = await move_folder_tree(path, new_path, body.recursive, current_user.wiki_id, settings)
    return FolderMutationResult(**result)


@router.delete("/{path:path}", response_model=FolderMutationResult)
async def delete_folder(
    path: str,
    recursive: bool = Query(False),
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> FolderMutationResult:
    result = await delete_folder_tree(path, recursive, current_user.wiki_id, settings)
    return FolderMutationResult(**result)
