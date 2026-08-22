"""One path from a markdown file to every index that derives from it.

Archivum is a living store: the vault is plain markdown you own, and you are
expected to create and edit those files by hand, with git, or with another
editor. That makes the file on disk canonical and every index — SQLite rows,
canonical knowledge, the Kuzu projection, embeddings — a derivative that has to
be able to catch up.

So indexing is expressed as one idempotent operation, `reindex_page`, rather
than as a fan-out duplicated across the create, update, ingest and queue paths.
Anything that notices a page changed calls it: the API on write, the vault
watcher on a file event, the reconcile pass at startup, or you, by hand.

Two rules hold throughout:

1. **Canonical writes fail loudly; projections degrade quietly.** Losing the
   file or its SQLite row is data loss. Losing an embedding or a graph node is
   an inconvenience that a later reindex repairs, so a projection error is
   recorded and reported, never raised.
2. **Re-running is free.** Reindexing an unchanged page is detected and skipped,
   so a watcher may be noisy and a reconcile pass may be wholesale.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archivum.config import Settings
from archivum.db import graph, sqlite
from archivum.db import qdrant_client as qdrant
from archivum.knowledge.projections import project_page
from archivum.knowledge.repository import KnowledgeRepository
from archivum.knowledge.suggestions import SuggestionRepository, init_suggestion_schema
from archivum.linting import WIKILINK_RE, normalize_wikilink_target
from archivum.memory.catalog import register_page_asset
from archivum.memory.registry import MemoryAssetRegistry
from archivum.pages_to_knowledge import remove_page_from_knowledge, sync_page_to_knowledge

logger = logging.getLogger(__name__)


@dataclass
class ReindexResult:
    """What a reindex did, and what it could not do.

    `degraded` is not an error: it lists the projections that were unavailable,
    so a caller can report honestly and a later pass can repair them.
    """

    slug: str
    action: str = "unchanged"  # indexed | unchanged | removed | missing
    reason: str = ""
    degraded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.degraded


def page_path(settings: Settings, slug: str) -> Path:
    return settings.wiki_dir / f"{slug}.md"


def slug_for_path(settings: Settings, path: Path) -> str | None:
    """The vault-relative slug for a markdown path, or None if it is outside."""
    try:
        relative = path.resolve().relative_to(settings.wiki_dir.resolve())
    except (ValueError, OSError):
        return None
    if relative.suffix != ".md":
        return None
    return relative.with_suffix("").as_posix()


def _title_from(markdown: str, slug: str) -> str:
    """Prefer frontmatter, then the first heading, then the file name."""
    body = markdown.lstrip()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            for line in body[3:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() == "title":
                    title = value.strip().strip("\"'")
                    if title:
                        return title
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or slug.rsplit("/", 1)[-1]
    return slug.rsplit("/", 1)[-1]


def _tags_from(markdown: str) -> list[str]:
    body = markdown.lstrip()
    if not body.startswith("---"):
        return []
    end = body.find("\n---", 3)
    if end == -1:
        return []
    for line in body[3:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != "tags":
            continue
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        return [tag.strip().strip("\"'") for tag in raw.split(",") if tag.strip()]
    return []


def ensure_frontmatter(markdown: str, *, title: str, tags: list[str] | None = None) -> str:
    """Make the file carry its own title and tags.

    The API takes metadata from the request body while the vault is edited by
    hand, so without this the title would live only in SQLite: hand-edit the
    file afterwards and a reindex, deriving from the file, would rename the page
    out from under you. Writing it into frontmatter keeps one source of truth —
    the file — which is the whole premise of a vault you own.

    Existing frontmatter is respected: it is the file already speaking for
    itself, and silently rewriting someone's YAML would be worse than the
    problem.
    """
    tags = tags or []
    body = markdown.lstrip("\n")
    if body.lstrip().startswith("---"):
        return markdown

    lines = [f"title: {title}"] if title else []
    if tags:
        rendered = ", ".join(tags)
        lines.append(f"tags: [{rendered}]")
    if not lines:
        return markdown
    return "---\n" + "\n".join(lines) + "\n---\n\n" + markdown.lstrip("\n")


async def _project_graph(
    result: ReindexResult, operation, *args: Any, label: str
) -> None:
    """Run a Kuzu projection step, recording rather than raising on failure."""
    try:
        await operation(*args)
    except Exception as exc:  # noqa: BLE001 - projections must never fail a write
        logger.warning("graph projection %s failed: %s", label, exc)
        if label not in result.degraded:
            result.degraded.append(label)


async def project_page_graph(
    slug: str, title: str, markdown: str, wiki_id: str, result: ReindexResult
) -> None:
    """Project one page and its outgoing links into the graph.

    Incremental on purpose: the full rebuild exists for repair, but a vault that
    only converges when someone runs a rebuild is not a living store.
    """
    await _project_graph(result, graph.upsert_page, slug, title, wiki_id, label="graph.node")
    await _project_graph(
        result, graph.clear_references_from_page, slug, wiki_id, label="graph.edges"
    )

    targets = {
        normalize_wikilink_target(target)
        for target in WIKILINK_RE.findall(markdown or "")
        if target.strip()
    }
    for target in sorted(targets):
        if not target or target == slug:
            continue
        # Only link to pages that exist: a wikilink to nothing is a lint finding,
        # not a graph edge.
        if await sqlite.get_page(target, wiki_id):
            await _project_graph(
                result, graph.add_reference, slug, target, wiki_id, label="graph.edges"
            )


async def reindex_page(
    slug: str,
    *,
    wiki_id: str,
    settings: Settings,
    force: bool = False,
    authored_by: str = "user",
    reason: str = "",
    distill: bool = True,
) -> ReindexResult:
    """Bring every index in line with the markdown file for `slug`.

    A missing file means the page was deleted outside the app, so the row and
    its projections are removed rather than left pointing at nothing.
    """
    result = ReindexResult(slug=slug, reason=reason)
    path = page_path(settings, slug)

    if not path.exists():
        existing = await sqlite.get_page(slug, wiki_id)
        if not existing:
            result.action = "missing"
            return result
        await forget_page(slug, wiki_id=wiki_id, settings=settings, result=result)
        result.action = "removed"
        return result

    markdown = path.read_text(encoding="utf-8")
    title = _title_from(markdown, slug)
    tags = _tags_from(markdown)

    existing = await sqlite.get_page(slug, wiki_id)
    if existing and not force and existing.get("content") == markdown:
        result.action = "unchanged"
        return result

    # Canonical first: if this fails the caller should hear about it.
    await sqlite.upsert_page(slug, title, markdown, tags, authored_by, wiki_id)

    async with sqlite.get_db() as conn:
        await init_suggestion_schema(conn)
        repo = KnowledgeRepository(conn)
        await sync_page_to_knowledge(
            repo,
            slug=slug,
            title=title,
            markdown=markdown,
            wiki_id=wiki_id,
        )
        # Project the canonical record straight away. The graph is a derived
        # view, and one that only converges on a manual rebuild is not a view
        # anyone can trust.
        try:
            await project_page(repo, page_id=f"page:{wiki_id}:{slug}", wiki_id=wiki_id)
        except Exception as exc:  # noqa: BLE001 - the graph is rebuildable
            logger.warning("knowledge projection for %s failed: %s", slug, exc)
            result.degraded.append("graph.knowledge")

        # Governance travels with the page. Registration only ever happened in
        # the catalog pass, which nothing in the app ran, so pages stayed
        # invisible to the asset registry that agent loadouts read from.
        await register_page_asset(
            MemoryAssetRegistry(conn),
            repo,
            wiki_id=wiki_id,
            slug=slug,
            title=title,
            content=markdown,
            change_note=reason or "Indexed",
        )

    await project_page_graph(slug, title, markdown, wiki_id, result)

    try:
        await qdrant.upsert_page(slug, title, markdown, wiki_id, settings)
    except Exception as exc:  # noqa: BLE001 - search is rebuildable
        logger.warning("qdrant upsert for %s failed: %s", slug, exc)
        result.degraded.append("search")

    if distill:
        # Queued, never inline: distillation may reach a model, and indexing has
        # to stay fast enough to run on every keystroke-driven save and every
        # watcher sweep.
        await sqlite.enqueue_distillation(slug, wiki_id, kind="page")

    result.action = "indexed"
    return result


async def repoint_page(
    *, old_slug: str, new_slug: str, wiki_id: str
) -> dict[str, int]:
    """Follow a rename through everything that referenced the old slug.

    Renaming used to fix the row, the knowledge record, shares, embeddings and
    inbound wikilinks — but not memory or the review queue, both of which key
    off the slug. They were left pointing at a page that no longer existed.
    """
    async with sqlite.get_db() as conn:
        await init_suggestion_schema(conn)
        moved_assets = await MemoryAssetRegistry(conn).repoint_page(
            wiki_id=wiki_id, old_slug=old_slug, new_slug=new_slug
        )
        moved_suggestions = await SuggestionRepository(conn).repoint_page(
            wiki_id=wiki_id, old_slug=old_slug, new_slug=new_slug
        )
    return {"assets": moved_assets, "suggestions": moved_suggestions}


async def forget_page(
    slug: str,
    *,
    wiki_id: str,
    settings: Settings,
    result: ReindexResult | None = None,
) -> ReindexResult:
    """Drop a page from every index. The file itself is the caller's business."""
    result = result or ReindexResult(slug=slug)

    await sqlite.delete_page(slug, wiki_id)
    async with sqlite.get_db() as conn:
        await init_suggestion_schema(conn)
        await remove_page_from_knowledge(KnowledgeRepository(conn), slug=slug, wiki_id=wiki_id)
        # Memory outlives the page it came from, but must stop claiming to live
        # there; a pending suggestion against a deleted page can never be
        # accepted, so it is retired rather than left in the queue forever.
        await MemoryAssetRegistry(conn).detach_page(wiki_id=wiki_id, slug=slug)
        await SuggestionRepository(conn).expire_for_page(wiki_id=wiki_id, slug=slug)

    try:
        await qdrant.delete_page(slug, wiki_id, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant delete for %s failed: %s", slug, exc)
        result.degraded.append("search")

    await _project_graph(result, graph.delete_page_node, slug, label="graph.node")
    await _project_graph(
        result, graph.cleanup_abandoned_nodes, wiki_id, label="graph.cleanup"
    )
    return result


# One sweep at a time, per process. Startup reconciles when
# `vault_reconcile_on_start` is set, the vault watcher reconciles what changed,
# and the reindex endpoint runs the same pass on demand — so without this, asking
# for a reindex while the startup sweep was still going put two full passes over
# the same pages at once and the loser died on `database is locked`.
_RECONCILE_LOCK = asyncio.Lock()


async def reconcile_vault(
    *, wiki_id: str, settings: Settings, force: bool = False
) -> list[ReindexResult]:
    """Make the indexes match what is on disk, in both directions.

    Files that changed while the app was not looking get reindexed; rows whose
    file has since disappeared get dropped. This is what makes editing the vault
    with an external tool safe.

    Serialised against other sweeps: a caller that arrives mid-sweep waits for
    it rather than racing it. Two passes over the same pages contend for the one
    write lock and neither is doing anything the other is not.
    """
    async with _RECONCILE_LOCK:
        return await _reconcile_vault(wiki_id=wiki_id, settings=settings, force=force)


async def _reconcile_vault(
    *, wiki_id: str, settings: Settings, force: bool = False
) -> list[ReindexResult]:
    results: list[ReindexResult] = []
    seen: set[str] = set()

    if settings.wiki_dir.exists():
        for path in sorted(settings.wiki_dir.rglob("*.md")):
            slug = slug_for_path(settings, path)
            if not slug:
                continue
            seen.add(slug)
            results.append(
                await reindex_page(
                    slug,
                    wiki_id=wiki_id,
                    settings=settings,
                    force=force,
                    # `force` is the repair path — rebuilding projections that
                    # were lost, not reacting to new content. Queueing a model
                    # pass over the whole vault for that would be wasteful.
                    distill=not force,
                    reason="reconcile",
                )
            )

    for row in await sqlite.list_pages(wiki_id):
        if row["slug"] in seen:
            continue
        result = ReindexResult(slug=row["slug"], reason="reconcile")
        await forget_page(row["slug"], wiki_id=wiki_id, settings=settings, result=result)
        result.action = "removed"
        results.append(result)

    changed = [r for r in results if r.action != "unchanged"]
    if changed:
        logger.info(
            "vault reconcile touched %d of %d pages", len(changed), len(results)
        )
    return results
