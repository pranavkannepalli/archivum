import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings
from archivum.ingest.agent import ExtractionResult, WikiAgent, WikiPage
from archivum.ingest.parsers import ParsedDoc
from archivum.ingest.pipeline import (
    SourceAnchor,
    ingest,
    _sync_extracted_result_to_knowledge,
)
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema


class IngestPipelineTests(unittest.TestCase):
    def test_fallback_extraction_adds_entities_and_relationships_for_graph(self):
        agent = WikiAgent(SimpleNamespace(llm_extraction_provider="test"))
        doc = ParsedDoc(
            text=(
                "Jane Doe is a product designer at Archivum. "
                "Jane Doe built Knowledge Graph Search with OpenAI."
            ),
            source="resume.pdf",
            metadata={"title": "resume", "filename": "resume.pdf"},
        )

        result = agent._fallback_extraction(doc)

        entity_names = {entity["name"] for entity in result.entities}
        self.assertIn("Jane Doe", entity_names)
        self.assertIn("Archivum", entity_names)
        self.assertIn("Knowledge Graph Search", entity_names)
        self.assertGreaterEqual(len(result.relationships), 2)

    def test_ingest_uses_display_source_for_logs_events_and_extraction(self):
        async def run_test():
            events = []
            # Real settings, not a stub: ingest keeps the raw bytes as L0
            # evidence before deriving anything, so it needs somewhere to put
            # them.
            root = Path(tempfile.mkdtemp())
            settings = Settings(
                db_path=root / "archivum.db",
                blob_dir=root / "blobs",
                wiki_dir=root / "wiki",
                llm_extraction_provider="test",
                llm_model="test-model",
            )
            await sqlite_mod.init_db(settings)
            parsed_doc_holder = {}

            async def fake_parse_source(source):
                from archivum.ingest.parsers import ParsedDoc

                return ParsedDoc(
                    text="Jane Doe built Archivum.",
                    source=str(source),
                    metadata={"type": "pdf"},
                )

            class FakeAgent:
                async def extract(self, doc):
                    parsed_doc_holder["doc"] = doc
                    return ExtractionResult(
                        pages=[
                            WikiPage(
                                slug="jane-doe",
                                title="Jane Doe",
                                content="# Jane Doe\n\nJane Doe built [[Archivum]].",
                                tags=["person"],
                            )
                        ],
                        entities=[
                            {"name": "Jane Doe", "type": "person"},
                            {"name": "Archivum", "type": "tech"},
                        ],
                        relationships=[
                            {"from": "Jane Doe", "to": "Archivum", "type": "related_to"}
                        ],
                    )

            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                Path(tmp.name).write_text("fake pdf content", encoding="utf-8")

                with (
                    patch("archivum.ingest.pipeline.parse_source", side_effect=fake_parse_source),
                    patch("archivum.ingest.pipeline.get_agent", return_value=FakeAgent()),
                    patch("archivum.ingest.pipeline.sqlite.create_ingest_log", new=AsyncMock(return_value=42)) as create_log,
                    patch("archivum.ingest.pipeline.sqlite.update_ingest_log", new=AsyncMock()),
                    patch("archivum.ingest.pipeline.sqlite.get_page", new=AsyncMock(return_value=None)),
                    patch("archivum.ingest.pipeline.sqlite.upsert_page", new=AsyncMock(return_value=(1, True))),
                    patch("archivum.ingest.pipeline.qdrant.upsert_page", new=AsyncMock(return_value=1)),
                    patch("archivum.ingest.pipeline.graph.upsert_page", new=AsyncMock()),
                    patch("archivum.ingest.pipeline.graph.upsert_entity", new=AsyncMock()),
                    patch("archivum.ingest.pipeline.graph.add_entity_relation", new=AsyncMock()),
                    patch("archivum.ingest.pipeline.graph.add_mention", new=AsyncMock()),
                    patch("archivum.ingest.pipeline.graph.add_reference", new=AsyncMock()),
                ):
                    await ingest(
                        Path(tmp.name),
                        "default",
                        lambda event: events.append(event) or asyncio.sleep(0),
                        settings,
                        source_name="resume.pdf",
                    )

            create_log.assert_awaited_once_with("file", "resume.pdf", "default")
            self.assertEqual(events[0]["file"], "resume.pdf")
            self.assertEqual(events[1]["source"], "resume.pdf")
            self.assertEqual(parsed_doc_holder["doc"].source, "resume.pdf")
            self.assertEqual(parsed_doc_holder["doc"].metadata["title"], "resume")
            self.assertEqual(parsed_doc_holder["doc"].metadata["filename"], "resume.pdf")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()


@pytest.mark.asyncio
async def test_ingest_runs_to_completion_for_every_extracted_page(tmp_path, mock_kuzu_conn):
    """Ingest must finish, not abort partway with a page already on disk.

    The whole pipeline ran behind a broad `except Exception`, so a fault after
    the first page write reported as an ordinary ingest error while leaving the
    vault half-built. Asserting on the summary is what catches that; asserting
    only on the early progress events does not.
    """
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        llm_extraction_provider="test",
    )
    await sqlite_mod.init_db(settings)
    events: list[dict] = []

    async def fake_parse_source(source):
        return ParsedDoc(
            text="Jane Doe built Archivum with Kuzu.",
            source=str(source),
            metadata={"title": "notes"},
        )

    class FakeAgent:
        async def extract(self, doc):
            return ExtractionResult(
                pages=[
                    WikiPage(slug="jane-doe", title="Jane Doe", content="# Jane Doe\n\nBuilt [[Archivum]].", tags=["person"]),
                    WikiPage(slug="archivum", title="Archivum", content="# Archivum\n\nBy Jane Doe.", tags=["project"]),
                ],
                entities=[{"name": "Jane Doe", "type": "person"}, {"name": "Archivum", "type": "project"}],
                relationships=[{"from": "Jane Doe", "to": "Archivum", "type": "built"}],
            )

    source_file = tmp_path / "notes.md"
    source_file.write_text("Jane Doe built Archivum with Kuzu.", encoding="utf-8")

    with (
        patch("archivum.ingest.pipeline.parse_source", side_effect=fake_parse_source),
        patch("archivum.ingest.pipeline.get_agent", return_value=FakeAgent()),
        patch("archivum.ingest.pipeline.graph.upsert_entity", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_entity_relation", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_mention", new=AsyncMock()),
        patch("archivum.ingest.pipeline.graph.add_reference", new=AsyncMock()),
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        summary = await ingest(
            source_file,
            "default",
            lambda event: events.append(event) or asyncio.sleep(0),
            settings,
        )

    assert summary["type"] == "done", summary
    assert summary["pages_created"] == 2
    assert summary["entities_extracted"] == 2
    assert [event["slug"] for event in events if event["type"] == "page_created"] == [
        "jane-doe",
        "archivum",
    ]
    assert (settings.wiki_dir / "jane-doe.md").exists()
    assert (settings.wiki_dir / "archivum.md").exists()


@pytest.mark.asyncio
async def test_ingest_canonical_records_preserve_extracted_source_provenance():
    doc = ParsedDoc(
        text="Jane Doe built Archivum.",
        source="resume.pdf",
        metadata={"title": "Resume"},
    )
    result = ExtractionResult(
        pages=[
            WikiPage(
                slug="jane-doe",
                title="Jane Doe",
                content="# Jane Doe\n\nJane Doe built [[Archivum]].",
                tags=["person"],
            )
        ],
        entities=[
            {"name": "Jane Doe", "type": "person"},
            {"name": "Archivum", "type": "project"},
        ],
        relationships=[{"from": "Jane Doe", "to": "Archivum", "type": "built"}],
    )

    # Everything ingest derives hangs off the stored evidence, so the anchor
    # carries the real source id and the chunks its citations may point into.
    anchor = SourceAnchor(
        source_id="a1b2c3",
        text=doc.text,
        chunks=(("chunk-0", 0, len(doc.text)),),
    )

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        await _sync_extracted_result_to_knowledge(
            repo,
            result=result,
            slug_map={"jane-doe": "jane-doe"},
            doc=doc,
            wiki_id="default",
            source_type="file",
            display_source="resume.pdf",
            anchor=anchor,
        )

        page = await repo.get_object("page:default:jane-doe")
        entity = await repo.get_object("entity:default:jane-doe")
        relationships = await repo.list_relationships(scope="wiki:default")

    # The source's own record is written when its bytes are stored; this covers
    # what gets *derived* from it.
    assert page is not None
    # A citation has to name a chunk that exists, or provenance is decoration.
    assert page.citations[0].source_id == anchor.source_id
    assert page.citations[0].chunk_id == "chunk-0"
    assert page.extraction_method == "EXTRACTED"
    assert page.properties["markdown"].startswith("# Jane Doe")
    assert entity is not None
    assert entity.properties["entity_type"] == "person"
    assert entity.citations[0].source_id == anchor.source_id
    assert any(
        rel.src_id == SELF_ID
        and rel.dst_id == "page:default:jane-doe"
        and rel.rel_type == "saved_source"
        for rel in relationships
    )
    assert any(
        rel.src_id == "entity:default:jane-doe"
        and rel.dst_id == "entity:default:archivum"
        and rel.rel_type == "built"
        and rel.extraction_method == "EXTRACTED"
        for rel in relationships
    )


async def test_entities_are_linked_to_the_page_they_were_found_in():
    """Without this edge the whole entity graph floats free of the person.

    Entities were written, and entity-to-entity relationships were written, but
    nothing ever connected a page to the entities extracted from it. Pages hang
    off `person:self`; entities hung off nothing. On a real vault that left 96
    of 150 records unreachable from the owner — a personal memory graph in which
    most of the graph is not attached to the person.

    `projections.py` has always had a branch for `page --mentions--> non-page`;
    it could never fire, because no such edge was produced.
    """
    doc = ParsedDoc(text="Jane Doe built Archivum.", source="resume.pdf", metadata={})
    result = ExtractionResult(
        pages=[
            WikiPage(
                slug="jane-doe",
                title="Jane Doe",
                content="Jane Doe built [[Archivum]].",
                tags=[],
            )
        ],
        entities=[
            {"name": "Jane Doe", "type": "person"},
            {"name": "Archivum", "type": "project"},
        ],
        relationships=[],
    )
    anchor = SourceAnchor(
        source_id="a1b2c3", text=doc.text, chunks=(("chunk-0", 0, len(doc.text)),)
    )

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        await _sync_extracted_result_to_knowledge(
            repo,
            result=result,
            slug_map={"jane-doe": "jane-doe"},
            doc=doc,
            wiki_id="default",
            source_type="file",
            display_source="resume.pdf",
            anchor=anchor,
        )
        relationships = await repo.list_relationships(scope="wiki:default")

    mentioned = {
        rel.dst_id
        for rel in relationships
        if rel.src_id == "page:default:jane-doe" and rel.rel_type == "mentions"
    }
    assert mentioned == {"entity:default:jane-doe", "entity:default:archivum"}

    # Reachability is the actual property that matters, so assert it directly.
    adjacency: dict[str, set[str]] = {}
    for rel in relationships:
        adjacency.setdefault(rel.src_id, set()).add(rel.dst_id)
        adjacency.setdefault(rel.dst_id, set()).add(rel.src_id)
    seen, stack = {SELF_ID}, [SELF_ID]
    while stack:
        for neighbour in adjacency.get(stack.pop(), ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    assert "entity:default:archivum" in seen, "an entity you cannot reach is not your memory"
