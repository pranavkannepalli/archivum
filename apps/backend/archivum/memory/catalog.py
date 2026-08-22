"""Bring pre-existing memory under the asset registry.

Wiki pages, ingested sources, and code graphs were memory before the registry
existed. Cataloguing registers them as typed, governed assets so every memory
kind — not just distilled conversation memory — can be versioned, reviewed, and
bound to an agent. Re-running is idempotent: ids are derived, not generated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import re

import aiosqlite

from archivum.markdown_text import lede as page_lede
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.personal_root import ensure_personal_root, link_to_self
from archivum.knowledge.repository import KnowledgeRepository
from archivum.memory.registry import MemoryAssetRegistry

CODEGRAPH_SCOPE_PREFIX = "repo:"

# Pages that already back a distilled memory asset must not be re-registered as
# plain wiki assets, or one unit of memory would appear under two ids.
EXCLUDED_PAGE_PREFIXES = ("memory/", "skills/")

_CODEGRAPH_SAMPLE_CITATIONS = 5


@dataclass
class CatalogReport:
    wiki_assets: int = 0
    source_assets: int = 0
    codegraph_assets: int = 0
    asset_ids: list[str] = field(default_factory=list)


async def sync_catalog(
    conn: aiosqlite.Connection, *, wiki_id: str
) -> CatalogReport:
    """Register wiki pages, sources, and code graphs as memory assets."""
    registry = MemoryAssetRegistry(conn)
    repo = KnowledgeRepository(conn)
    report = CatalogReport()

    await ensure_personal_root(repo, wiki_id=wiki_id)
    await _catalog_pages(conn, registry, repo, wiki_id=wiki_id, report=report)
    await _catalog_sources(conn, registry, repo, wiki_id=wiki_id, report=report)
    await _catalog_codegraphs(conn, registry, repo, wiki_id=wiki_id, report=report)
    return report


# ── Wiki pages ────────────────────────────────────────────────────────────


async def _catalog_pages(
    conn: aiosqlite.Connection,
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    report: CatalogReport,
) -> None:
    async with conn.execute(
        "SELECT slug, title, content FROM pages WHERE wiki_id=? ORDER BY slug ASC",
        (wiki_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        object_id = await register_page_asset(
            registry,
            repo,
            wiki_id=wiki_id,
            slug=row["slug"],
            title=row["title"],
            content=row["content"] or "",
            change_note="Catalogued from the markdown vault",
        )
        if object_id is None:
            continue
        report.wiki_assets += 1
        report.asset_ids.append(object_id)


async def register_page_asset(
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    slug: str,
    title: str,
    content: str = "",
    change_note: str = "Registered from the markdown vault",
) -> str | None:
    """Bring one page under governance. Returns its id, or None if it is exempt.

    Called on every index as well as by the catalog pass, so a page is memory
    an agent can be given from the moment it exists rather than from whenever
    someone remembers to run a backfill.
    """
    if slug.startswith(EXCLUDED_PAGE_PREFIXES):
        return None
    # The canonical page object already exists and is already owner-linked,
    # so the asset shares its id rather than duplicating the record.
    object_id = f"page:{wiki_id}:{slug}"
    canonical = await repo.get_object(object_id)
    # Prefer the page as it is on disk. Only ingested pages keep a copy of
    # their markdown on the canonical record, so relying on that alone left
    # every hand-written page with nothing to summarise.
    lede = page_lede(content or (str(canonical.properties.get("markdown", "")) if canonical else ""))
    citations = (
        canonical.citations
        if canonical is not None and canonical.citations
        # Quote the page's own opening line. The quote used to be the record id,
        # which cited nothing — it only restated that the record exists.
        else [_self_citation(object_id, lede or title)]
    )
    await registry.register_asset(
        id=object_id,
        wiki_id=wiki_id,
        asset_type="wiki",
        layer="L1",
        name=title,
        scope=f"wiki:{wiki_id}",
        status="active",
        page_slug=slug,
        # What this page says, not what every page is. The old text was the
        # same sentence on all of them, and the surface renders summary over
        # name, so it hid the title behind a fact about the file format.
        summary=lede or title,
        tags=["wiki"],
        metadata={"slug": slug},
        citations=citations,
        change_note=change_note,
    )
    return object_id


# ── Ingested sources ──────────────────────────────────────────────────────


async def _catalog_sources(
    conn: aiosqlite.Connection,
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    report: CatalogReport,
) -> None:
    async with conn.execute(
        "SELECT s.id, s.source_type, s.origin_uri, s.content_hash, s.version, "
        "       s.ingested_at, d.id AS document_id "
        "FROM sources AS s LEFT JOIN documents AS d ON d.source_id = s.id "
        "ORDER BY s.id ASC"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        asset_id = await register_source_asset(
            registry,
            repo,
            wiki_id=wiki_id,
            source_id=row["id"],
            source_type=row["source_type"],
            origin_uri=row["origin_uri"],
            content_hash=row["content_hash"],
            version=row["version"],
            ingested_at=row["ingested_at"],
            document_id=row["document_id"],
            change_note="Catalogued from the evidence store",
        )
        report.source_assets += 1
        report.asset_ids.append(asset_id)


def source_asset_id(source_id: str) -> str:
    """The one id a stored source is known by, everywhere."""
    return f"source:{source_id}"


async def register_source_asset(
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    source_id: str,
    source_type: str,
    origin_uri: str,
    content_hash: str,
    version: int,
    ingested_at: str,
    document_id: str | None = None,
    change_note: str = "Registered from the evidence store",
) -> str:
    """Bring one stored source under governance as an L0 asset.

    Shared by the catalog pass and by ingest itself. They used to describe the
    same source differently — a different id and a different kind — which meant
    ingesting a file and then cataloguing it produced two records for one piece
    of evidence. Deriving both from here is what keeps that impossible.
    """
    asset_id = source_asset_id(source_id)
    citation = Citation(
        source_id=source_id,
        chunk_id=document_id or source_id,
        span_start=None,
        span_end=None,
        quote=origin_uri,
    )
    obj = KnowledgeObject(
        id=asset_id,
        kind="source",
        label=origin_uri or source_id,
        scope=f"wiki:{wiki_id}",
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=[citation],
        properties={
            "layer": "L0",
            "source_id": source_id,
            "source_type": source_type,
            "content_hash": content_hash,
            "version": version,
            "origin_uri": origin_uri,
        },
    )
    await repo.upsert_object(obj)
    await link_to_self(repo, asset_id, "owns_asset", citation=citation)
    await registry.register_asset(
        id=asset_id,
        wiki_id=wiki_id,
        asset_type="source",
        layer="L0",
        name=obj.label,
        scope=obj.scope,
        status="active",
        summary=f"{source_type} source, version {version}.",
        tags=["source", source_type],
        metadata={
            "source_id": source_id,
            "content_hash": content_hash,
            "ingested_at": ingested_at,
        },
        citations=[citation],
        change_note=change_note,
    )
    return asset_id


# ── Code graphs ───────────────────────────────────────────────────────────


async def _catalog_codegraphs(
    conn: aiosqlite.Connection,
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    report: CatalogReport,
) -> None:
    async with conn.execute(
        "SELECT DISTINCT scope FROM knowledge_objects WHERE scope LIKE ? ORDER BY scope ASC",
        (f"{CODEGRAPH_SCOPE_PREFIX}%",),
    ) as cursor:
        scopes = [row["scope"] for row in await cursor.fetchall()]

    for scope in scopes:
        asset_id = await register_codegraph_asset(
            registry,
            repo,
            wiki_id=wiki_id,
            repo_scope=scope,
            change_note="Catalogued from the code graph",
        )
        if asset_id is None:
            continue
        report.codegraph_assets += 1
        report.asset_ids.append(asset_id)


async def register_codegraph_asset(
    registry: MemoryAssetRegistry,
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    repo_scope: str,
    change_note: str = "Registered from the code graph",
) -> str | None:
    """Bring one repository's code graph under governance as an L2 asset.

    Shared by the catalog pass and by repository indexing, so a repo indexed
    through the app and one catalogued afterwards cannot end up described twice.
    """
    nodes = await repo.list_objects(scope=repo_scope, limit=10_000)
    edges = await repo.list_relationships(scope=repo_scope)
    if not nodes:
        return None

    asset_id = f"codegraph:{repo_scope}"
    citations = [
        node.citations[0]
        for node in nodes[:_CODEGRAPH_SAMPLE_CITATIONS]
        if node.citations
    ] or [_self_citation(asset_id, repo_scope)]
    obj = KnowledgeObject(
        id=asset_id,
        kind="memory_codegraph",
        label=f"Code graph — {repo_scope}",
        scope=f"wiki:{wiki_id}",
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=citations,
        properties={
            "layer": "L2",
            "repo_scope": repo_scope,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
    )
    await repo.upsert_object(obj)
    await link_to_self(repo, asset_id, "uses_code", citation=citations[0])
    await registry.register_asset(
        id=asset_id,
        wiki_id=wiki_id,
        asset_type="codegraph",
        layer="L2",
        name=obj.label,
        scope=obj.scope,
        status="active",
        summary=f"{len(nodes)} code records and {len(edges)} relationships.",
        tags=["codegraph"],
        metadata={
            "repo_scope": repo_scope,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        citations=citations,
        change_note=change_note,
    )
    return asset_id


def _self_citation(object_id: str, quote: str) -> Citation:
    return Citation(
        source_id=object_id,
        chunk_id=object_id,
        span_start=None,
        span_end=None,
        quote=quote,
    )
