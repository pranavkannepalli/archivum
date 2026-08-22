"""Ingest and the evidence store have to describe the same source.

Archivum had two ingest paths that never met. `/api/ingest` parsed a file into
markdown pages and minted a *fabricated* provenance id, while `/api/sources`
stored raw bytes as L0 evidence under a real source id. Nothing joined them, so
a file dropped into the app produced pages that cited an id no store had ever
heard of: no raw evidence to go back to, no `sources` row for the library to
list, and nothing distillation would accept.

These tests pin the join: one ingest, one source id, evidence underneath it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings
from archivum.ingest.agent import ExtractionResult, WikiPage
from archivum.ingest.parsers import ParsedDoc
from archivum.ingest.pipeline import ingest
from archivum.knowledge.repository import KnowledgeRepository
from archivum.memory.catalog import sync_catalog
from archivum.memory.registry import MemoryAssetRegistry
from archivum.store.blobs import BlobStore
from archivum.store.hashing import sha256_bytes
from archivum.store.repository import SourceStore

RAW = b"Jane Doe built Archivum with Kuzu.\n"


@pytest.fixture
def vault(tmp_path):
    return Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        llm_extraction_provider="test",
    )


class _FakeAgent:
    async def extract(self, doc):
        return ExtractionResult(
            pages=[
                WikiPage(
                    slug="jane-doe",
                    title="Jane Doe",
                    content="# Jane Doe\n\nBuilt Archivum.",
                    tags=["person"],
                )
            ],
            entities=[{"name": "Jane Doe", "type": "person"}],
            relationships=[],
        )


async def _run_ingest(settings, *, source_name="notes.md"):
    source_file = settings.db_path.parent / "upload.tmp"
    source_file.write_bytes(RAW)

    async def fake_parse_source(source):
        return ParsedDoc(
            text=RAW.decode(), source=str(source), metadata={"title": "Notes", "type": "md"}
        )

    with (
        patch("archivum.ingest.pipeline.parse_source", side_effect=fake_parse_source),
        patch("archivum.ingest.pipeline.get_agent", return_value=_FakeAgent()),
        patch("archivum.ingest.pipeline.graph.upsert_entity", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_entity_relation", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_mention", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_reference", new=AsyncMock()),
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        return await ingest(source_file, "default", None, settings, source_name=source_name)


@pytest.mark.asyncio
async def test_ingest_stores_the_raw_bytes_as_l0_evidence(vault, mock_kuzu_conn):
    await sqlite_mod.init_db(vault)

    summary = await _run_ingest(vault)
    assert summary["type"] == "done", summary

    sources = await SourceStore().list_sources(wiki_id="default")
    assert len(sources) == 1
    # The origin is what the user brought in, not the upload's temp path.
    assert sources[0].origin_uri == "notes.md"
    assert sources[0].content_hash == sha256_bytes(RAW)
    assert BlobStore(vault.blob_dir).get(sha256_bytes(RAW)) == RAW


@pytest.mark.asyncio
async def test_ingest_chunks_the_source_so_citations_have_somewhere_to_point(vault, mock_kuzu_conn):
    await sqlite_mod.init_db(vault)
    await _run_ingest(vault)

    store = SourceStore()
    source = (await store.list_sources(wiki_id="default"))[0]
    document = await store.get_document_for_source(source.id)
    assert document is not None
    assert await store.list_chunks(document.id)


@pytest.mark.asyncio
async def test_derived_records_cite_the_stored_source_id(vault, mock_kuzu_conn):
    await sqlite_mod.init_db(vault)
    await _run_ingest(vault)

    source = (await SourceStore().list_sources(wiki_id="default"))[0]
    async with sqlite_mod.get_db() as conn:
        derived = await KnowledgeRepository(conn).list_objects_from_source(
            source.id, scope="wiki:default"
        )

    # The page ingest produced must be reachable from the source it came from.
    assert any(obj.kind == "page" and obj.properties.get("slug") == "jane-doe" for obj in derived)

    store = SourceStore()
    document = await store.get_document_for_source(source.id)
    chunk_ids = {chunk.id for chunk in await store.list_chunks(document.id)}
    # The source's own record stands for the whole document; everything derived
    # from it must name the chunk its evidence actually sits in.
    cited = {
        citation.chunk_id
        for obj in derived
        if obj.id != f"source:{source.id}"
        for citation in obj.citations
    }
    assert cited, "derived records must carry citations"
    assert cited <= chunk_ids, f"citations point at chunks that do not exist: {cited - chunk_ids}"


@pytest.mark.asyncio
async def test_ingest_registers_the_source_as_a_governed_memory_asset(vault, mock_kuzu_conn):
    """A source the user brought in is memory, so it arrives already governed.

    Cataloguing used to be the only way an ingested source became an asset, and
    nothing in the app ever called it, so sources sat outside the registry that
    the profile page and agent loadouts read from.
    """
    await sqlite_mod.init_db(vault)
    await _run_ingest(vault)

    source = (await SourceStore().list_sources(wiki_id="default"))[0]
    async with sqlite_mod.get_db() as conn:
        assets = await MemoryAssetRegistry(conn).list_assets(
            wiki_id="default", asset_type="source"
        )

    assert [asset.id for asset in assets] == [f"source:{source.id}"]
    assert assets[0].layer == "L0"
    assert assets[0].owner == "person:self"
    assert assets[0].citations


@pytest.mark.asyncio
async def test_cataloguing_after_ingest_does_not_fork_the_source_record(vault, mock_kuzu_conn):
    """Ingest and catalog must agree on one id and one kind, or memory doubles."""
    await sqlite_mod.init_db(vault)
    await _run_ingest(vault)

    source = (await SourceStore().list_sources(wiki_id="default"))[0]
    async with sqlite_mod.get_db() as conn:
        before = await MemoryAssetRegistry(conn).list_assets(
            wiki_id="default", asset_type="source"
        )
        await sync_catalog(conn, wiki_id="default")
        after = await MemoryAssetRegistry(conn).list_assets(
            wiki_id="default", asset_type="source"
        )
        objects = await KnowledgeRepository(conn).list_objects(scope="wiki:default")

    assert [asset.id for asset in after] == [f"source:{source.id}"]
    # Re-cataloguing identical content is a no-op, not a new version.
    assert [asset.version for asset in after] == [before[0].version]
    source_objects = [obj for obj in objects if obj.id.startswith("source:")]
    assert [obj.id for obj in source_objects] == [f"source:{source.id}"]
    assert {obj.kind for obj in source_objects} == {"source"}


@pytest.mark.asyncio
async def test_re_ingesting_identical_bytes_does_not_fork_the_evidence(vault, mock_kuzu_conn):
    await sqlite_mod.init_db(vault)

    await _run_ingest(vault)
    await _run_ingest(vault)

    sources = await SourceStore().list_sources(wiki_id="default")
    assert [source.version for source in sources] == [1]


@pytest.mark.asyncio
async def test_an_ingested_file_shows_up_in_the_library_as_a_source(vault, mock_kuzu_conn):
    """The payoff: one drop-in produces both the source and the pages it made.

    The library lists sources from the evidence store and pages from the vault.
    With the two ingest paths unjoined, dropping a file in produced pages that
    appeared to come from nowhere — the source itself was never listed.
    """
    await sqlite_mod.init_db(vault)
    await _run_ingest(vault)

    from archivum.api.entries import list_entries
    from archivum.auth import CurrentUser

    listing = await list_entries(
        kind=None,
        needs_review=False,
        limit=200,
        current_user=CurrentUser(username="owner", role="owner", wiki_id="default"),
    )
    by_kind = {entry.kind: entry for entry in listing.entries}

    assert by_kind["source"].title == "notes.md"
    # The page the extraction produced, filed by its own tag.
    assert by_kind["person"].slug == "jane-doe"
