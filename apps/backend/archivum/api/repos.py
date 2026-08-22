"""Repository routes: /api/repos/*

Registering a repository points Archivum at a directory on the machine it is
running on, and then publishes what it finds as readable pages. That is a
host-level capability, so it is owner-only: a collaborator has write access to
the vault's *content*, which is not the same as being able to make the backend
read an arbitrary directory and hand back the result.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from archivum.auth import CurrentUser, get_current_user, require_owner
from archivum.code_repos import (
    CodeRepo,
    RepoError,
    get_repo,
    list_repos,
    register_repo,
    scope_for,
)
from archivum.db import sqlite

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repos", tags=["repos"])


class RegisterRepoRequest(BaseModel):
    path: str
    name: str | None = None


class RepoSummary(BaseModel):
    scope: str
    name: str
    path: str
    status: str
    files: int = 0
    nodes: int = 0
    edges: int = 0
    pages: int = 0
    error: str | None = None
    indexed_at: str | None = None


def _to_summary(repo: CodeRepo) -> RepoSummary:
    return RepoSummary(
        scope=repo.scope,
        name=repo.name,
        path=repo.path,
        status=repo.status,
        files=repo.files,
        nodes=repo.nodes,
        edges=repo.edges,
        pages=repo.pages,
        error=repo.error,
        indexed_at=repo.indexed_at,
    )


@router.get("", response_model=list[RepoSummary])
async def list_registered_repos(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[RepoSummary]:
    return [_to_summary(repo) for repo in await list_repos(wiki_id=current_user.wiki_id)]


@router.post("", response_model=RepoSummary, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRepoRequest,
    current_user: CurrentUser = Depends(require_owner),
) -> RepoSummary:
    """Register a repository and queue it for indexing.

    Indexing is queued rather than run here: parsing a repository is slow and
    CPU-bound, and doing it in the request would hold the whole server while a
    large repo is read.
    """
    logger.info("API register_repo", extra={"wiki_id": current_user.wiki_id})
    try:
        repo = await register_repo(
            path=Path(body.path), wiki_id=current_user.wiki_id, name=body.name
        )
    except RepoError as exc:
        code = "invalid_repo_name" if "name" in str(exc) else "repo_not_found"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": code},
        ) from exc
    return _to_summary(repo)


@router.post("/{name}/reindex", response_model=RepoSummary)
async def reindex(
    name: str,
    current_user: CurrentUser = Depends(require_owner),
) -> RepoSummary:
    """Queue an already-registered repository to be read again."""
    repo = await get_repo(scope_for(name, wiki_id=current_user.wiki_id), wiki_id=current_user.wiki_id)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Repository '{name}' is not registered", "code": "repo_not_registered"},
        )
    requeued = await register_repo(
        path=Path(repo.path), wiki_id=current_user.wiki_id, name=repo.name
    )
    return _to_summary(requeued)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def forget(
    name: str,
    current_user: CurrentUser = Depends(require_owner),
) -> None:
    """Stop tracking a repository.

    The canonical code records and the pages already written stay: they are
    memory of work that happened, and deleting the register entry is a
    statement about what to keep indexing, not about what to forget.
    """
    scope = scope_for(name, wiki_id=current_user.wiki_id)
    repo = await get_repo(scope, wiki_id=current_user.wiki_id)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"Repository '{name}' is not registered", "code": "repo_not_registered"},
        )
    async with sqlite.get_db() as conn:
        await conn.execute(
            "DELETE FROM code_repos WHERE scope=? AND wiki_id=?",
            (scope, current_user.wiki_id),
        )
        await conn.commit()
