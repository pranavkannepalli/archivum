import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.capture.schema import Conversation, Turn
from archivum.capture.store import CaptureStore
from archivum.config import Settings
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository
from archivum.memory.catalog import sync_catalog
from archivum.memory.registry import MemoryAssetRegistry
from archivum.pages_to_knowledge import sync_page_to_knowledge
from archivum.store.blobs import BlobStore
from archivum.store.repository import SourceStore


@pytest.fixture
async def settings(tmp_path):
    settings = Settings(db_path=tmp_path / "archivum.db", blob_dir=tmp_path / "blobs")
    await sqlite_mod.init_db(settings)
    return settings


def _code_node(node_id, scope="repo:archivum"):
    return KnowledgeObject(
        id=node_id,
        kind="function",
        label=node_id,
        scope=scope,
        confidence=0.9,
        extraction_method="EXTRACTED",
        citations=[
            Citation(
                source_id="file:main.py",
                chunk_id="file:main.py",
                span_start=0,
                span_end=4,
                quote="def main",
            )
        ],
        properties={},
    )


@pytest.mark.asyncio
async def test_catalog_registers_markdown_pages_sharing_the_canonical_id(settings):
    await sqlite_mod.upsert_page("notes", "Notes", "# Notes", ["note"], "user")
    async with sqlite_mod.get_db() as conn:
        await sync_page_to_knowledge(
            KnowledgeRepository(conn),
            slug="notes",
            title="Notes",
            markdown="# Notes",
            wiki_id="default",
        )
        report = await sync_catalog(conn, wiki_id="default")
        asset = await MemoryAssetRegistry(conn).get_asset("page:default:notes")

    assert report.wiki_assets == 1
    assert asset.asset_type == "wiki"
    assert asset.status == "active"
    assert asset.page_slug == "notes"
    assert asset.citations  # inherited from the canonical page object


@pytest.mark.asyncio
async def test_catalog_skips_pages_that_already_back_a_memory_asset(settings):
    await sqlite_mod.upsert_page(
        "memory/sessions/s1", "Session", "# Session", [], "agent"
    )
    await sqlite_mod.upsert_page("skills/deploy", "Deploy", "# Deploy", [], "agent")
    await sqlite_mod.upsert_page("notes", "Notes", "# Notes", [], "user")

    async with sqlite_mod.get_db() as conn:
        report = await sync_catalog(conn, wiki_id="default")

    assert report.wiki_assets == 1
    assert report.asset_ids == ["page:default:notes"]


@pytest.mark.asyncio
async def test_catalog_registers_captured_sources_and_links_them_to_the_owner(settings):
    store = CaptureStore(
        store=SourceStore(), blob_store=BlobStore(settings.blob_dir), settings=settings
    )
    captured = await store.capture(
        Conversation(
            session_id="s1",
            interface="claude_code_native",
            started_at="2026-08-12T00:00:00Z",
            turns=(Turn(role="user", text="hello there"),),
        )
    )

    async with sqlite_mod.get_db() as conn:
        report = await sync_catalog(conn, wiki_id="default")
        repo = KnowledgeRepository(conn)
        asset = await MemoryAssetRegistry(conn).get_asset(f"source:{captured.source_id}")
        edges = await repo.list_relationships(node_id=SELF_ID)

    assert report.source_assets == 1
    assert asset.asset_type == "source"
    assert asset.layer == "L0"
    assert asset.metadata["content_hash"] == captured.content_hash
    assert any(
        edge.rel_type == "owns_asset" and edge.dst_id == asset.id for edge in edges
    )


@pytest.mark.asyncio
async def test_catalog_registers_one_asset_per_code_graph_scope(settings):
    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(_code_node("repo:archivum:main"))
        await repo.upsert_object(_code_node("repo:archivum:helper"))
        await repo.upsert_object(_code_node("repo:other:main", scope="repo:other"))
        await repo.upsert_relationship(
            KnowledgeRelationship(
                id="rel:main:calls:helper",
                src_id="repo:archivum:main",
                dst_id="repo:archivum:helper",
                rel_type="calls",
                scope="repo:archivum",
                confidence=0.8,
                extraction_method="INFERRED",
                citations=[
                    Citation(
                        source_id="file:main.py",
                        chunk_id="file:main.py",
                        span_start=0,
                        span_end=4,
                        quote="main",
                    )
                ],
                properties={},
            )
        )
        report = await sync_catalog(conn, wiki_id="default")
        registry = MemoryAssetRegistry(conn)
        archivum = await registry.get_asset("codegraph:repo:archivum")
        other = await registry.get_asset("codegraph:repo:other")

    assert report.codegraph_assets == 2
    assert archivum.metadata == {
        "repo_scope": "repo:archivum",
        "node_count": 2,
        "edge_count": 1,
    }
    assert other.metadata["node_count"] == 1
    assert archivum.citations


@pytest.mark.asyncio
async def test_catalog_is_idempotent(settings):
    await sqlite_mod.upsert_page("notes", "Notes", "# Notes", [], "user")

    async with sqlite_mod.get_db() as conn:
        first = await sync_catalog(conn, wiki_id="default")
        second = await sync_catalog(conn, wiki_id="default")
        versions = await MemoryAssetRegistry(conn).list_versions("page:default:notes")

    assert first.asset_ids == second.asset_ids
    assert [version.version for version in versions] == [1]


@pytest.mark.asyncio
async def test_catalog_scopes_pages_to_the_requested_wiki(settings):
    await sqlite_mod.upsert_page("notes", "Notes", "# Notes", [], "user", "default")
    await sqlite_mod.upsert_page("other", "Other", "# Other", [], "user", "second")

    async with sqlite_mod.get_db() as conn:
        report = await sync_catalog(conn, wiki_id="second")

    assert report.asset_ids == ["page:second:other"]


@pytest.mark.asyncio
async def test_a_page_asset_summarises_the_page_rather_than_the_file_format(settings):
    """"Editable markdown page." was the summary of all 31 pages in the vault.

    Every wiki asset carried the same sentence, and the surface renders
    `summary || name` — so the boilerplate hid the page title behind a fact that
    is true of every page and tells you nothing about any of them. The point of
    a summary is to say what this particular memory holds.

    The self-citation had the same shape of problem: its quote was the record's
    own id, so a citation proved only that the record existed.
    """
    from archivum.memory.catalog import register_page_asset

    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        registry = MemoryAssetRegistry(conn)
        await sync_page_to_knowledge(
            repo,
            slug="projects/perceo/archivum",
            title="Archivum",
            markdown=(
                "---\ntitle: Archivum\ntags: [memory]\n---\n\n"
                "# Archivum\n\n"
                "Archivum is a local-first knowledge vault that keeps its pages as "
                "plain markdown on disk.\n\nMore detail follows."
            ),
            wiki_id="default",
        )
        asset_id = await register_page_asset(
            registry,
            repo,
            wiki_id="default",
            slug="projects/perceo/archivum",
            title="Archivum",
            content=(
                "---\ntitle: Archivum\ntags: [memory]\n---\n\n"
                "# Archivum\n\n"
                "Archivum is a local-first knowledge vault that keeps its pages as "
                "plain markdown on disk.\n\nMore detail follows."
            ),
        )
        asset = await registry.get_asset(asset_id)

    assert asset is not None
    assert asset.summary != "Editable markdown page."
    assert "local-first knowledge vault" in asset.summary
    # Frontmatter and the title heading are not what the page is about.
    assert "---" not in asset.summary
    assert not asset.summary.startswith("#")
