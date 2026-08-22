import aiosqlite
import pytest

from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.projections import rebuild_knowledge_projections
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema


@pytest.mark.asyncio
async def test_projection_excludes_code_objects_from_qdrant(monkeypatch):
    indexed = []

    async def fake_index_page(slug, title, markdown, wiki_id="default"):
        indexed.append(slug)

    monkeypatch.setattr("archivum.knowledge.projections.index_page", fake_index_page)
    monkeypatch.setattr("archivum.knowledge.projections.clear_projection_index", async_noop)

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(KnowledgeObject(
            id="symbol:retrieve_code",
            kind="symbol",
            label="retrieve_code",
            scope="repo:test",
            confidence=1.0,
            extraction_method="EXTRACTED",
            citations=[Citation(source_id="repo:test", chunk_id="file:a.py", span_start=0, span_end=10, quote="def retrieve_code")],
            properties={},
        ))
        report = await rebuild_knowledge_projections(repo, wiki_id="default")
        assert report.qdrant_indexed == 0
        assert indexed == []


def knowledge_object(object_id, kind, label, scope="wiki:default", **properties):
    return KnowledgeObject(
        id=object_id,
        kind=kind,
        label=label,
        scope=scope,
        confidence=0.8,
        extraction_method="EXTRACTED",
        citations=[Citation(source_id="note", chunk_id="chunk:1", span_start=1, span_end=5, quote="evidence")],
        properties=properties,
    )


@pytest.mark.asyncio
async def test_rebuild_clears_derived_indexes_and_projects_provenance(monkeypatch):
    calls = {"qdrant_clears": [], "indexed": [], "nodes": [], "edges": [], "pages": [], "references": []}

    async def record(name, result=None):
        async def operation(*args):
            calls[name].append(args)
            return result
        return operation

    monkeypatch.setattr("archivum.knowledge.projections.clear_projection_index", await record("qdrant_clears"))
    monkeypatch.setattr("archivum.knowledge.projections.index_page", await record("indexed", 1))
    monkeypatch.setattr("archivum.knowledge.projections.clear_knowledge_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.clear_legacy_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.upsert_knowledge_node", await record("nodes"))
    monkeypatch.setattr("archivum.knowledge.projections.add_knowledge_relationship", await record("edges"))
    monkeypatch.setattr("archivum.knowledge.projections.upsert_page", await record("pages"))
    monkeypatch.setattr("archivum.knowledge.projections.upsert_entity", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.add_reference", await record("references"))
    monkeypatch.setattr("archivum.knowledge.projections.add_mention", async_noop)

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        page = knowledge_object("page:default:alpha", "page", "Alpha", slug="alpha", markdown="# Alpha")
        claim = knowledge_object("claim:alpha", "claim", "Claim", text="Canonical claim")
        symbol = knowledge_object("symbol:alpha", "symbol", "alpha")
        for object_ in (page, claim, symbol):
            await repo.upsert_object(object_)
        await repo.upsert_relationship(KnowledgeRelationship(
            id="rel:alpha", src_id=page.id, dst_id=claim.id, rel_type="mentions",
            scope="wiki:default", confidence=0.7, extraction_method="INFERRED",
            citations=claim.citations, properties={},
        ))
        await repo.upsert_relationship(KnowledgeRelationship(
            id="rel:reference", src_id=page.id, dst_id=page.id, rel_type="references",
            scope="wiki:default", confidence=1.0, extraction_method="USER_AUTHORED",
            citations=page.citations, properties={},
        ))

        report = await rebuild_knowledge_projections(repo, wiki_id="default")

    assert calls["qdrant_clears"] == [("default",)]
    assert {call[0] for call in calls["indexed"]} == {page.id, claim.id}
    assert calls["nodes"][0][6] == [page.citations[0].model_dump()]
    assert calls["edges"][0][7] == [claim.citations[0].model_dump()]
    assert calls["pages"] == [("alpha", "Alpha", "default")]
    assert calls["references"] == [("alpha", "alpha", "default")]
    assert report.qdrant_indexed == 2
    assert report.kuzu_nodes == 3
    assert report.kuzu_edges == 2


@pytest.mark.asyncio
async def test_rebuild_projects_only_requested_wiki_scope_plus_self(monkeypatch):
    calls = {"indexed": [], "nodes": [], "edges": [], "pages": []}

    async def record(name, result=None):
        async def operation(*args):
            calls[name].append(args)
            return result
        return operation

    monkeypatch.setattr("archivum.knowledge.projections.clear_projection_index", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.index_page", await record("indexed", 1))
    monkeypatch.setattr("archivum.knowledge.projections.clear_knowledge_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.clear_legacy_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.upsert_knowledge_node", await record("nodes"))
    monkeypatch.setattr("archivum.knowledge.projections.add_knowledge_relationship", await record("edges"))
    monkeypatch.setattr("archivum.knowledge.projections.upsert_page", await record("pages"))
    monkeypatch.setattr("archivum.knowledge.projections.upsert_entity", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.add_reference", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.add_mention", async_noop)

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        root = KnowledgeObject(
            id=SELF_ID,
            kind="person",
            label="Me",
            scope=SELF_ID,
            confidence=1.0,
            extraction_method="USER_AUTHORED",
            citations=[Citation(source_id=SELF_ID, chunk_id=SELF_ID, span_start=None, span_end=None, quote="Me")],
            properties={"is_owner": True},
        )
        default_page = knowledge_object("page:default:alpha", "page", "Alpha", slug="alpha", markdown="# Alpha")
        other_page = knowledge_object("page:other:beta", "page", "Beta", scope="wiki:other", slug="beta", markdown="# Beta")
        repo_symbol = knowledge_object("symbol:repo:service", "symbol", "Service", scope="repo:test")
        for object_ in (root, default_page, other_page, repo_symbol):
            await repo.upsert_object(object_)
        await repo.upsert_relationship(KnowledgeRelationship(
            id="rel:self-alpha", src_id=SELF_ID, dst_id=default_page.id, rel_type="authored_thought",
            scope="wiki:default", confidence=1.0, extraction_method="USER_AUTHORED",
            citations=default_page.citations, properties={},
        ))
        await repo.upsert_relationship(KnowledgeRelationship(
            id="rel:self-beta", src_id=SELF_ID, dst_id=other_page.id, rel_type="authored_thought",
            scope="wiki:other", confidence=1.0, extraction_method="USER_AUTHORED",
            citations=other_page.citations, properties={},
        ))

        report = await rebuild_knowledge_projections(repo, wiki_id="default")

    projected_ids = {call[0] for call in calls["nodes"]}
    assert projected_ids == {SELF_ID, default_page.id}
    assert {call[0] for call in calls["indexed"]} == {default_page.id}
    assert calls["pages"] == [("alpha", "Alpha", "default")]
    assert [call[2] for call in calls["edges"]] == ["rel:self-alpha"]
    assert report.objects == 2
    assert report.relationships == 1


async def async_noop(*args):
    return None


@pytest.mark.asyncio
async def test_rebuild_clears_stale_projection_records_before_each_rebuild(monkeypatch):
    qdrant_clears = []
    indexed = []

    async def clear_projection_index(wiki_id):
        qdrant_clears.append(wiki_id)

    async def index_page(slug, title, markdown, wiki_id="default"):
        indexed.append(slug)
        return 1

    monkeypatch.setattr("archivum.knowledge.projections.clear_projection_index", clear_projection_index)
    monkeypatch.setattr("archivum.knowledge.projections.index_page", index_page)
    for name in (
        "clear_knowledge_projection", "clear_legacy_projection", "upsert_knowledge_node",
        "upsert_page", "upsert_entity", "add_knowledge_relationship", "add_reference", "add_mention",
    ):
        monkeypatch.setattr(f"archivum.knowledge.projections.{name}", async_noop)

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        stale = knowledge_object("source:stale", "source", "Stale", text="old evidence")
        await repo.upsert_object(stale)
        await rebuild_knowledge_projections(repo, wiki_id="default")
        await repo.delete_object(stale.id)
        await rebuild_knowledge_projections(repo, wiki_id="default")

    assert qdrant_clears == ["default", "default"]
    assert indexed == ["source:stale"]


@pytest.mark.asyncio
async def test_rebuild_reports_only_successful_kuzu_writes(monkeypatch):
    async def fail(*args):
        raise RuntimeError("Kuzu unavailable")

    monkeypatch.setattr("archivum.knowledge.projections.clear_projection_index", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.clear_knowledge_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.clear_legacy_projection", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.upsert_knowledge_node", fail)
    monkeypatch.setattr("archivum.knowledge.projections.upsert_entity", async_noop)
    monkeypatch.setattr("archivum.knowledge.projections.add_knowledge_relationship", fail)
    monkeypatch.setattr("archivum.knowledge.projections.index_page", async_noop)

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        source = knowledge_object("source:one", "source", "One")
        target = knowledge_object("person:two", "person", "Two")
        await repo.upsert_object(source)
        await repo.upsert_object(target)
        await repo.upsert_relationship(KnowledgeRelationship(
            id="rel:one", src_id=source.id, dst_id=target.id, rel_type="saved_source",
            scope="wiki:default", confidence=1.0, extraction_method="EXTRACTED",
            citations=source.citations, properties={},
        ))
        report = await rebuild_knowledge_projections(repo, wiki_id="default")

    assert report.kuzu_nodes == 0
    assert report.kuzu_edges == 0


@pytest.mark.asyncio
async def test_rebuild_projects_registered_repositories_too(tmp_path, monkeypatch, mock_kuzu_conn):
    """Code has to reach the graph, or the graph view cannot show your work.

    The rebuild filtered strictly on `wiki:{id}`, and code lives under
    `repo:{name}`, so a repository could be fully indexed into canonical
    knowledge and still never appear as a single node in the graph.
    """
    import archivum.db.sqlite as sqlite_mod
    from archivum.config import Settings

    monkeypatch.setattr("archivum.knowledge.projections.index_page", async_noop)
    monkeypatch.setattr(
        "archivum.knowledge.projections.clear_projection_index", async_noop
    )
    settings = Settings(db_path=tmp_path / "a.db", blob_dir=tmp_path / "b")
    await sqlite_mod.init_db(settings)

    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(
            KnowledgeObject(
                id="repo_atlas_geo_haversine",
                kind="symbol",
                label="haversine",
                scope="repo:atlas",
                confidence=1.0,
                extraction_method="EXTRACTED",
                citations=[
                    Citation(
                        source_id="x", chunk_id="geo.py",
                        span_start=None, span_end=None, quote="L1-L2",
                    )
                ],
                properties={"source_scope": "repo:atlas"},
            )
        )
        await conn.execute(
            "INSERT INTO code_repos (scope, wiki_id, name, path, status, created_at, updated_at)"
            " VALUES ('repo:atlas','default','atlas','/tmp/atlas','ready','t','t')"
        )
        await conn.commit()

        report = await rebuild_knowledge_projections(repo, wiki_id="default")

    assert report.objects >= 1
    assert report.kuzu_nodes >= 1
