from __future__ import annotations

import asyncio
import importlib
import logging
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from importlib.util import find_spec

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from archivum.auth import CurrentUser, get_current_user, require_owner, require_writer
from archivum.config import Settings, get_settings
from archivum.db import graph, qdrant_client as qdrant, sqlite
from archivum.ingest.agent import slugify
from archivum.indexing import reconcile_vault
from archivum.knowledge.backfill import link_entities_to_their_pages
from archivum.knowledge.projections import rebuild_knowledge_projections
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema
from archivum.knowledge.personal_root import SELF_SCOPE
from archivum.knowledge.suggestions import SuggestionRepository
from archivum.memory.registry import MemoryAssetRegistry
from archivum.linting import WIKILINK_RE, analyze_wiki_pages, normalize_wikilink_target
from archivum.llm.cli_client import (
    CliModelError,
    cli_status,
    codex_login_status,
    start_codex_device_login,
)
from archivum.pages_to_knowledge import sync_page_to_knowledge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])

class LlmSettingsRequest(BaseModel):
    llm_extraction_provider: str
    llm_synthesis_provider: str
    llm_model: str
    llm_synthesis_model: str
    ollama_base_url: str
    ollama_api_key: str | None = None


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


def _llm_settings_response(settings: Settings) -> dict[str, Any]:
    ollama_api_key = settings.ollama_api_key or ""
    return {
        "llm_extraction_provider": settings.llm_extraction_provider,
        "llm_synthesis_provider": settings.llm_synthesis_provider,
        "llm_model": settings.llm_model,
        "llm_synthesis_model": settings.llm_synthesis_model,
        "ollama_base_url": settings.ollama_base_url,
        "ollama_api_key_configured": bool(ollama_api_key),
        "ollama_api_key_masked": _mask_secret(ollama_api_key),
        "cli_providers": cli_status(),
    }


def _env_line(key: str, value: str) -> str:
    escaped = value.replace("\n", "").replace("\r", "")
    return f"{key}={escaped}\n"


def _write_env_updates(updates: dict[str, str], env_path: str = ".env") -> None:
    path = Path(env_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=.*$")

    for line in lines:
        match = pattern.match(line.strip())
        if not match:
            output.append(line)
            continue

        key = match.group(1)
        if key in updates:
            output.append(_env_line(key, updates[key]))
            seen.add(key)
        else:
            output.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        if output:
            output.append("\n")
        output.append("# UI-managed LLM settings\n")
        for key in missing:
            output.append(_env_line(key, updates[key]))

    path.write_text("".join(output), encoding="utf-8")


def _apply_runtime_env(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value


def get_audio_feature_status() -> dict[str, Any]:
    whisper_available = find_spec("whisper") is not None
    ffmpeg_path = shutil.which("ffmpeg")
    ffmpeg_available = ffmpeg_path is not None
    missing = []
    if not whisper_available:
        missing.append("openai-whisper")
    if not ffmpeg_available:
        missing.append("ffmpeg")

    return {
        "available": whisper_available and ffmpeg_available,
        "audio_available": whisper_available,
        "video_available": whisper_available and ffmpeg_available,
        "dependencies": {
            "openai_whisper": whisper_available,
            "ffmpeg": ffmpeg_available,
        },
        "missing": missing,
        "notes": [
            "The default published Docker images omit Whisper, Torch, and ffmpeg to keep installs smaller.",
            "Installing packages inside a running Docker container is not durable across upgrades.",
        ],
    }


def _install_action(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


async def _run_install_command(
    name: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _install_action(name, "failed", f"Timed out after {timeout_seconds} seconds")
    except OSError as exc:
        return _install_action(name, "failed", str(exc))

    output = "\n".join(
        part.decode("utf-8", errors="replace").strip()
        for part in (stdout, stderr)
        if part
    ).strip()
    detail = output[-4000:] if output else "Command completed"
    if proc.returncode == 0:
        return _install_action(name, "installed", "Installed successfully")
    return _install_action(
        name,
        "failed",
        f"Automatic install failed. Review backend logs for {name} install details.",
    )


async def _install_openai_whisper() -> dict[str, str]:
    if find_spec("whisper") is not None:
        return _install_action(
            "openai-whisper",
            "already_available",
            "Whisper is already installed",
        )

    backend_dir = Path(__file__).resolve().parents[2]
    uv = shutil.which("uv")
    if uv:
        result = await _run_install_command(
            "openai-whisper",
            [uv, "sync", "--extra", "audio"],
            cwd=backend_dir,
        )
    else:
        result = await _run_install_command(
            "openai-whisper",
            [sys.executable, "-m", "pip", "install", "openai-whisper"],
        )
    importlib.invalidate_caches()
    return result


def _privileged_command(base: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return base
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, "-n", *base]
    return base


async def _install_ffmpeg() -> dict[str, str]:
    if shutil.which("ffmpeg"):
        return _install_action("ffmpeg", "already_available", "ffmpeg is already installed")

    system = platform.system().lower()
    brew = shutil.which("brew")
    if system == "darwin" and brew:
        return await _run_install_command("ffmpeg", [brew, "install", "ffmpeg"])

    if shutil.which("apt-get"):
        update = await _run_install_command(
            "ffmpeg",
            _privileged_command(["apt-get", "update"]),
        )
        if update["status"] == "failed":
            return update
        return await _run_install_command(
            "ffmpeg",
            _privileged_command(["apt-get", "install", "-y", "ffmpeg"]),
        )

    if shutil.which("apk"):
        return await _run_install_command(
            "ffmpeg",
            _privileged_command(["apk", "add", "ffmpeg"]),
        )

    if shutil.which("dnf"):
        return await _run_install_command(
            "ffmpeg",
            _privileged_command(["dnf", "install", "-y", "ffmpeg"]),
        )

    return _install_action(
        "ffmpeg",
        "failed",
        "Automatic install is not available in this environment",
    )


async def install_audio_support() -> dict[str, Any]:
    before = get_audio_feature_status()
    actions: list[dict[str, str]] = []

    if not before["dependencies"]["openai_whisper"]:
        actions.append(await _install_openai_whisper())
    else:
        actions.append(
            _install_action(
                "openai-whisper",
                "already_available",
                "Whisper is already installed",
            )
        )

    if not before["dependencies"]["ffmpeg"]:
        actions.append(await _install_ffmpeg())
    else:
        actions.append(
            _install_action("ffmpeg", "already_available", "ffmpeg is already installed")
        )

    status = get_audio_feature_status()
    return {
        "ok": status["available"],
        "actions": actions,
        "status": status,
    }


class OwnerProfile(BaseModel):
    """Who the vault belongs to, plus the counts the profile page shows.

    Archivum models one human. The display name lives on the `person:self`
    memory scope rather than in a second identity table, so renaming yourself is
    the same operation as renaming any other scope.
    """

    wiki_id: str
    scope_id: str = SELF_SCOPE
    name: str
    initials: str
    role: str
    since: str | None = None
    # True while person:self still carries the schema's placeholder name, i.e.
    # the owner has never introduced themselves. Drives the setup flow.
    needs_setup: bool = False
    pages: int = 0
    memories_active: int = 0
    memories_total: int = 0
    agents: int = 0
    pending_review: int = 0


# memory/schema.py seeds person:self with the name "Self" as a placeholder, so
# the name doubles as "has the owner introduced themselves?". That misfires for
# anyone actually called Self, but the alternative is a schema change to carry a
# boolean; the seeded row is also recognisable by never having been updated.
_PLACEHOLDER_SELF_NAMES = {"", "Self"}


def _initials(name: str) -> str:
    parts = [part for part in re.split(r"[\s._-]+", name.strip()) if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@router.get("/me", response_model=OwnerProfile)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> OwnerProfile:
    wiki_id = current_user.wiki_id
    async with sqlite.get_db() as conn:
        registry = MemoryAssetRegistry(conn)
        scope = await registry.get_scope(SELF_SCOPE, wiki_id)
        counts = await registry.asset_counts(wiki_id=wiki_id)
        agents = await registry.list_agents(wiki_id)
        suggestion_counts = await SuggestionRepository(conn).suggestion_counts(wiki_id=wiki_id)

    pages = await sqlite.list_pages(wiki_id)
    # The schema seeds person:self with the placeholder name "Self", so treat
    # that as unnamed and fall back to the configured owner until setup renames it.
    scope_name = (scope.name if scope else "").strip()
    if scope_name in _PLACEHOLDER_SELF_NAMES:
        scope_name = ""
    name = scope_name or settings.owner_username or "You"

    return OwnerProfile(
        wiki_id=wiki_id,
        needs_setup=not scope_name,
        name=name,
        initials=_initials(name),
        role=current_user.role,
        since=scope.created_at if scope else None,
        pages=len(pages),
        memories_active=counts["by_status"].get("active", 0),
        memories_total=counts["total"],
        agents=len(agents),
        pending_review=suggestion_counts.get("pending", 0),
    )


@router.get("/audio-support")
async def audio_support(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    return get_audio_feature_status()


@router.post("/audio-support/install")
async def install_audio_support_endpoint(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    return await install_audio_support()


@router.get("/settings/llm")
async def llm_settings(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    return _llm_settings_response(get_settings())


@router.get("/settings/cli-auth/codex")
async def codex_cli_auth_status(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    return await codex_login_status()


@router.post("/settings/cli-auth/codex/start")
async def start_codex_cli_auth(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    try:
        return await start_codex_device_login()
    except CliModelError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": "cli_auth_unavailable"},
        ) from exc


@router.get("/settings/mcp")
async def mcp_settings(
    current_user: CurrentUser = Depends(require_owner),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    endpoint = settings.mcp_public_url.strip() or f"http://localhost:{settings.mcp_port}/sse"
    headers = {"Authorization": "Bearer <MCP_API_KEY>"} if settings.mcp_api_key else {}
    client_config: dict[str, Any] = {
        "mcpServers": {
            "archivum": {
                "url": endpoint,
            }
        }
    }
    if headers:
        client_config["mcpServers"]["archivum"]["headers"] = headers
    return {
        "endpoint": endpoint,
        "auth_required": bool(settings.mcp_api_key),
        "api_key_configured": bool(settings.mcp_api_key),
        "client_config": client_config,
    }


@router.put("/settings/llm")
async def update_llm_settings(
    body: LlmSettingsRequest,
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    previous = get_settings()
    extraction_providers = {"anthropic", "openrouter", "openai_compat", "ollama"}
    synthesis_providers = extraction_providers | {"codex_cli", "claude_cli"}
    if body.llm_extraction_provider not in extraction_providers or body.llm_synthesis_provider not in synthesis_providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Unsupported LLM provider", "code": "invalid_provider"},
        )

    updates = {
        "LLM_EXTRACTION_PROVIDER": body.llm_extraction_provider,
        "LLM_SYNTHESIS_PROVIDER": body.llm_synthesis_provider,
        "LLM_MODEL": body.llm_model.strip(),
        "LLM_SYNTHESIS_MODEL": body.llm_synthesis_model.strip(),
        "OLLAMA_BASE_URL": body.ollama_base_url.strip(),
    }
    if body.ollama_api_key is not None:
        updates["OLLAMA_API_KEY"] = body.ollama_api_key.strip()

    missing = [key for key, value in updates.items() if key != "OLLAMA_API_KEY" and not value]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": f"Missing required setting: {', '.join(missing)}", "code": "missing_setting"},
        )

    _apply_runtime_env(updates)
    _write_env_updates(updates)
    get_settings.cache_clear()
    response_ollama_api_key = updates.get("OLLAMA_API_KEY", previous.ollama_api_key)
    return {
        "llm_extraction_provider": updates["LLM_EXTRACTION_PROVIDER"],
        "llm_synthesis_provider": updates["LLM_SYNTHESIS_PROVIDER"],
        "llm_model": updates["LLM_MODEL"],
        "llm_synthesis_model": updates["LLM_SYNTHESIS_MODEL"],
        "ollama_base_url": updates["OLLAMA_BASE_URL"],
        "ollama_api_key_configured": bool(response_ollama_api_key),
        "ollama_api_key_masked": _mask_secret(response_ollama_api_key),
    }


@router.post("/rebuild-indexes")
async def rebuild_indexes(
    current_user: CurrentUser = Depends(require_owner),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    pages = await sqlite.list_pages(current_user.wiki_id)

    # Re-init derived stores
    await qdrant.init_collection(settings)
    await graph.init_graph(settings)

    for p in pages:
        await qdrant.upsert_page(p["slug"], p["title"], p.get("content", ""), current_user.wiki_id, settings)
        await graph.upsert_page(p["slug"], p["title"], current_user.wiki_id)

        # Rebuild REFERENCES edges from wikilinks
        content = p.get("content", "") or ""
        for target in WIKILINK_RE.findall(content):
            target_slug = normalize_wikilink_target(target)
            if not target_slug or target_slug == p["slug"]:
                continue
            existing = await sqlite.get_page(target_slug, current_user.wiki_id)
            if existing:
                await graph.add_reference(p["slug"], target_slug, current_user.wiki_id)

    async with sqlite.get_db() as connection:
        await init_knowledge_schema(connection)
        repo = KnowledgeRepository(connection)
        current_page_ids = {
            f"page:{current_user.wiki_id}:{p['slug']}"
            for p in pages
        }
        page_prefix = f"page:{current_user.wiki_id}:%"
        async with connection.execute(
            """
            SELECT id FROM knowledge_objects
            WHERE kind='page' AND scope=? AND id LIKE ?
            ORDER BY id
            """,
            (f"wiki:{current_user.wiki_id}", page_prefix),
        ) as cursor:
            canonical_page_ids = [row["id"] for row in await cursor.fetchall()]
        for page_id in canonical_page_ids:
            if page_id not in current_page_ids:
                await repo.delete_object(page_id)
        for p in pages:
            await sync_page_to_knowledge(
                repo,
                slug=p["slug"],
                title=p["title"],
                markdown=p.get("content", "") or "",
                wiki_id=current_user.wiki_id,
            )
        projection = await rebuild_knowledge_projections(repo, current_user.wiki_id)

    return {
        "detail": "Rebuilt indexes",
        "pages": len(pages),
        "canonical_objects": projection.objects,
        "canonical_relationships": projection.relationships,
        "qdrant_indexed": projection.qdrant_indexed,
        "kuzu_nodes": projection.kuzu_nodes,
        "kuzu_edges": projection.kuzu_edges,
    }


@router.post("/reindex")
async def reindex_vault(
    force: bool = False,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Re-read the whole vault from disk and rebuild what derives from it.

    Files are the truth: anything added, edited or deleted outside the app is
    picked up here. `force` re-indexes even unchanged pages, which is the repair
    path when a projection was lost rather than the content changed.
    """
    results = await reconcile_vault(
        wiki_id=current_user.wiki_id, settings=settings, force=force
    )
    # Re-reading pages cannot recreate links that ingest makes, so the repair
    # path rebuilds them from provenance rather than leaving a graph that was
    # written before the link existed permanently disconnected.
    rejoined = 0
    try:
        async with sqlite.get_db() as conn:
            rejoined = await link_entities_to_their_pages(
                KnowledgeRepository(conn), wiki_id=current_user.wiki_id
            )
    except Exception as exc:  # noqa: BLE001 - a reindex that indexed is still a win
        logger.warning("Could not rejoin entities to their pages: %s", exc)
    counts: dict[str, int] = {}
    degraded: set[str] = set()
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
        degraded.update(result.degraded)
    return {
        "pages": len(results),
        "actions": counts,
        "degraded": sorted(degraded),
        "entities_rejoined": rejoined,
    }


@router.get("/lint")
async def lint_wiki(
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    pages = await sqlite.list_pages(current_user.wiki_id)
    issues = analyze_wiki_pages(pages)["issues"]
    return {"issues": issues, "counts": {"issues": len(issues)}}


class LintFixRequest(BaseModel):
    type: str  # broken_wikilink | orphan
    source_slug: str | None = None
    link_target: str | None = None
    slug: str | None = None


@router.post("/lint/fix")
async def apply_lint_fix(
    body: LintFixRequest,
    current_user: CurrentUser = Depends(require_owner),
) -> dict[str, Any]:
    if body.type == "orphan":
        return {
            "detail": "no_auto_fix",
            "message": "Orphan pages cannot be auto-fixed; delete manually or add a wikilink to them.",
        }

    if body.type == "broken_wikilink":
        if not body.source_slug or not body.link_target:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_slug and link_target are required for broken_wikilink fixes",
            )

        page = await sqlite.get_page(body.source_slug, current_user.wiki_id)
        if not page:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Page '{body.source_slug}' not found",
            )

        content = page.get("content", "") or ""
        # Replace [[broken-link]] and [[broken-link|alias]] with plain text (the target slug)
        pattern = re.compile(
            r"\[\[" + re.escape(body.link_target) + r"(?:\|[^\]]+)?\]\]"
        )
        new_content = pattern.sub(body.link_target, content)

        if new_content == content:
            return {"detail": "no_change", "message": "Link not found in page content"}

        raw_tags = page.get("tags", "[]")
        if isinstance(raw_tags, list):
            tags: list[str] = raw_tags
        else:
            try:
                tags = json.loads(raw_tags)
            except Exception:
                tags = []

        await sqlite.upsert_page(
            slug=body.source_slug,
            title=page["title"],
            content=new_content,
            tags=tags,
            authored_by=page.get("authored_by", "agent"),
            wiki_id=current_user.wiki_id,
        )
        return {"detail": "fixed", "source_slug": body.source_slug, "link_target": body.link_target}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown fix type: {body.type}",
    )
