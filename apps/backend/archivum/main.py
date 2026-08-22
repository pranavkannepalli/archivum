from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from archivum.api import activity as activity_routes
from archivum.api import entries as entries_routes
from archivum.api import auth as auth_routes
from archivum.api import export as export_routes
from archivum.api import folders as folders_routes
from archivum.api import ingest as ingest_routes
from archivum.api import life_os as life_os_routes
from archivum.api import memory as memory_routes
from archivum.api import pages as pages_routes
from archivum.api import public as public_routes
from archivum.api import share as share_routes
from archivum.api import shared as shared_routes
from archivum.api import sharing as sharing_routes
from archivum.api import suggestions as suggestions_routes
from archivum.api.graph import router as graph_router
from archivum.api.context import router as context_router
from archivum.api.query import router as query_router
from archivum.api.search import router as search_router
from archivum.api.capture import router as capture_router
from archivum.api.capture_preview import router as capture_preview_router
from archivum.api.sources import router as sources_router
from archivum.api.repos import router as repos_router
from archivum.api.system import router as system_router
from archivum.config import Settings, get_settings
from archivum.db import qdrant_client as qdrant
from archivum.db import sqlite
from archivum.db import graph
from archivum.auth import hash_password
from archivum.logging_config import setup_logging
from archivum.observability import new_trace_id, set_trace_id
from archivum.memory.retention import run_retention_worker
from archivum.page_write_queue import run_page_write_worker
from archivum.capture.transcript_watch import run_transcript_watcher
from archivum.code_repos import run_code_repo_worker
from archivum.summaries import run_summary_worker
from archivum.distillation import run_distill_worker
from archivum.indexing import reconcile_vault
from archivum.vault_scaffold import claim_vault_dir, ensure_default_folders
from archivum.vault_watch import run_vault_watcher
from archivum.rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)


class _TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        trace_id = request.headers.get("x-trace-id") or new_trace_id("http")
        set_trace_id(trace_id)
        response = await call_next(request)
        response.headers["x-trace-id"] = trace_id
        return response


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


class _CSRFProtection(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        method = request.method.upper()
        if method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            # These establish a session, so they cannot present a CSRF token
            # they have not been issued yet. Both are guarded by an unguessable
            # token in the body and a per-token rate limit instead.
            if request.url.path in {"/api/auth/refresh", "/api/shared/claim"}:
                return await call_next(request)

            auth_header = request.headers.get("authorization")
            # Non-browser clients (MCP/CLI) use Bearer tokens; skip CSRF.
            if not (auth_header and auth_header.startswith("Bearer ")):
                has_cookie_auth = (
                    "access_token" in request.cookies
                    or "refresh_token" in request.cookies
                    # A share recipient's session is a cookie too, so their
                    # writes (comments) need the same double-submit check.
                    or "share_session" in request.cookies
                )
                if has_cookie_auth:
                    csrf_cookie = request.cookies.get("csrf_token")
                    csrf_header = request.headers.get("x-csrf-token")
                    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
                        return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)

        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()

    setup_logging()
    set_trace_id(new_trace_id("startup"))
    logger.info(
        "Starting Archivum API",
        extra={"qdrant_url": settings.qdrant_url, "db_path": str(settings.db_path), "wiki_dir": str(settings.wiki_dir)},
    )

    # Ensure directories exist
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    # Before anything writes: one vault directory belongs to one wiki. Page
    # paths do not carry the wiki, so two backends sharing a WIKI_DIR would
    # overwrite each other's markdown without either noticing.
    claim_vault_dir(settings.wiki_dir, wiki_id=settings.wiki_id)
    settings.code_cache_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.kuzu_path.mkdir(parents=True, exist_ok=True)
    settings.blob_dir.mkdir(parents=True, exist_ok=True)

    # Init derived stores
    await sqlite.init_db(settings)
    await qdrant.init_collection(settings)
    await graph.init_graph(settings)

    # Ensure owner exists
    owner_pw = settings.owner_password or secrets.token_urlsafe(24)
    await sqlite.ensure_owner_exists(settings.owner_username, hash_password(owner_pw))

    # A vault with no folders makes every capture a filing decision; give it a
    # starting shape. Only ever on a vault that has none of its own.
    try:
        await ensure_default_folders(wiki_id="default", settings=settings)
    except Exception as exc:  # noqa: BLE001 - folders are not worth a failed boot
        logger.warning("Could not seed default folders: %s", exc)

    # Catch up on anything edited while the app was down, before serving.
    if settings.vault_reconcile_on_start:
        try:
            results = await reconcile_vault(wiki_id="default", settings=settings)
            touched = [r for r in results if r.action != "unchanged"]
            if touched:
                logger.info(
                    "Reconciled vault at startup",
                    extra={"changed": len(touched), "total": len(results)},
                )
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            logger.warning("Vault reconcile at startup failed: %s", exc)

    vault_watch_task: asyncio.Task[None] | None = None
    if settings.vault_watch_enabled:
        vault_watch_task = asyncio.create_task(run_vault_watcher(settings))

    distill_task: asyncio.Task[None] | None = None
    if settings.distill_worker_enabled:
        distill_task = asyncio.create_task(run_distill_worker(settings))

    write_worker_task: asyncio.Task[None] | None = None
    if settings.page_write_worker_enabled:
        write_worker_task = asyncio.create_task(run_page_write_worker(settings))

    retention_task: asyncio.Task[None] | None = None
    if settings.retention_sweep_enabled:
        retention_task = asyncio.create_task(run_retention_worker(settings))

    # Repositories index on a queue for the same reason distillation does:
    # reading a repo is slow, and the wiki has to stay responsive meanwhile.
    code_repo_task: asyncio.Task[None] | None = None
    if settings.code_repo_worker_enabled:
        code_repo_task = asyncio.create_task(run_code_repo_worker(settings))

    # Sessions arrive without being asked for. Everything downstream of capture
    # reads conversations, and nothing was ever capturing them.
    transcript_task: asyncio.Task[None] | None = None
    if settings.transcript_watch_enabled:
        transcript_task = asyncio.create_task(run_transcript_watcher(settings))

    summary_task: asyncio.Task[None] | None = None
    if settings.summary_worker_enabled:
        summary_task = asyncio.create_task(run_summary_worker(settings))

    try:
        yield
    finally:
        for task in (
            write_worker_task,
            retention_task,
            vault_watch_task,
            distill_task,
            code_repo_task,
            transcript_task,
            summary_task,
        ):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()

    app = FastAPI(title="Archivum API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(_TraceMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_CSRFProtection)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routes
    app.include_router(activity_routes.router)
    app.include_router(entries_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(export_routes.router)
    app.include_router(folders_routes.router)
    app.include_router(pages_routes.router)
    app.include_router(public_routes.router)
    app.include_router(ingest_routes.router)
    app.include_router(life_os_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(search_router)
    app.include_router(context_router)
    app.include_router(query_router)
    app.include_router(graph_router)
    app.include_router(sources_router)
    app.include_router(repos_router)
    app.include_router(capture_router)
    app.include_router(capture_preview_router)
    app.include_router(system_router)
    app.include_router(share_routes.router)
    app.include_router(share_routes.mgmt_router)
    app.include_router(sharing_routes.router)
    app.include_router(shared_routes.router)
    app.include_router(suggestions_routes.router)

    return app


app = create_app()
