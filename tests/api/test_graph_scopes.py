"""Code has to be reachable from the same screens as everything else.

Every graph route pinned itself to `wiki:{wiki_id}`, and code records live under
`repo:{name}`, so the audit, the clusters, the surprising links and the path
finder could not see a single line of code — even though the algorithms are
scope-agnostic and the records were sitting right there.

Authorisation cannot come from the scope string the way it does for pages, so it
comes from the register: you may read a repository you registered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import archivum.db.sqlite as sqlite_mod
from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.repository import KnowledgeRepository
from archivum.main import create_app


def _citation(id_: str) -> Citation:
    return Citation(source_id=id_, chunk_id="f.py", span_start=None, span_end=None, quote="L1-L2")


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        code_cache_dir=tmp_path / "code-cache",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    await sqlite_mod.init_db(settings)

    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
    ):
        app = create_app()

    owner = CurrentUser(username="owner", role="owner", wiki_id="default")
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[require_writer] = lambda: owner
    app.dependency_overrides[get_settings] = lambda: settings

    # A tiny code graph, plus the register entry that authorises reading it.
    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        for symbol in ("haversine", "normalise"):
            await repo.upsert_object(
                KnowledgeObject(
                    id=f"repo_atlas_geo_{symbol}",
                    kind="symbol",
                    label=symbol,
                    scope="repo:default:atlas",
                    confidence=1.0,
                    extraction_method="EXTRACTED",
                    citations=[_citation(f"repo_atlas_geo_{symbol}")],
                    properties={"source_scope": "repo:default:atlas"},
                )
            )
        await repo.upsert_relationship(
            KnowledgeRelationship(
                id="rel:calls",
                src_id="repo_atlas_geo_haversine",
                dst_id="repo_atlas_geo_normalise",
                rel_type="calls",
                scope="repo:default:atlas",
                confidence=1.0,
                extraction_method="EXTRACTED",
                citations=[_citation("rel:calls")],
                properties={},
            )
        )
        await conn.execute(
            "INSERT INTO code_repos (scope, wiki_id, name, path, status, created_at, updated_at)"
            " VALUES ('repo:default:atlas','default','atlas','/tmp/atlas','ready','t','t')"
        )
        await conn.commit()

    yield TestClient(app, raise_server_exceptions=True)


async def test_the_audit_can_be_pointed_at_a_repository(env):
    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()
    assert body["scope"] == "repo:default:atlas"
    assert body["node_count"] == 2
    assert body["edge_count"] == 1


async def test_clusters_can_be_pointed_at_a_repository(env):
    body = env.get("/api/graph/communities", params={"scope": "repo:default:atlas"}).json()
    assert body["scope"] == "repo:default:atlas"
    members = {member for c in body["communities"] for member in c["member_ids"]}
    assert "repo_atlas_geo_haversine" in members


async def test_an_unregistered_repository_is_refused(env):
    response = env.get("/api/graph/audit", params={"scope": "repo:somebody-elses"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "unauthorized_graph_scope"


async def test_omitting_the_scope_still_means_this_wiki(env):
    body = env.get("/api/graph/audit").json()
    assert body["scope"] == "wiki:default"


async def test_code_context_is_allowed_for_a_registered_repository(env):
    response = env.post(
        "/api/context-package", json={"query": "haversine", "scope": "repo:default:atlas"}
    )
    assert response.status_code == 200, response.text
    labels = {node["label"] for node in response.json()["nodes"]}
    assert "haversine" in labels, "a code query should come back with code"


async def test_code_context_is_refused_for_an_unregistered_repository(env):
    response = env.post(
        "/api/context-package", json={"query": "x", "scope": "repo:somebody-elses"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "unauthorized_context_scope"


async def test_a_decision_link_is_visible_from_the_repository_graph(env):
    """`decided_in` joins a symbol to a conversation, so it lives in neither scope.

    Bridge edges are stored under their own scope precisely because they cross
    between code and memory. If a scoped load ignored them, the one edge that
    explains *why* a function exists would be the one edge nobody could see.
    """
    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(
            KnowledgeObject(
                id="memory:atom:1",
                kind="memory_atom",
                label="Keep haversine pure",
                scope="wiki:default",
                confidence=0.9,
                extraction_method="EXTRACTED",
                citations=[_citation("memory:atom:1")],
                properties={"text": "We decided haversine should stay pure."},
            )
        )
        await repo.upsert_relationship(
            KnowledgeRelationship(
                id="repo_atlas_geo_haversine__decided_in__memory:atom:1",
                src_id="repo_atlas_geo_haversine",
                dst_id="memory:atom:1",
                rel_type="decided_in",
                scope="bridge",
                confidence=0.8,
                extraction_method="INFERRED",
                citations=[_citation("memory:atom:1")],
                properties={},
            )
        )
        await conn.commit()

    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()
    assert body["edge_count"] == 2, "the decision link should be part of the code graph"
    assert body["node_count"] == 3, "and so should the decision it points at"


async def test_the_audit_carries_the_labels_needed_to_draw_it(env):
    """The picture is drawn from the audit, so the audit has to name its nodes.

    Cluster members came back as bare ids and the surface looked their labels up
    in a second, unscoped call. Pointed at a repository that lookup missed
    everything, so the graph rendered rows of `repo_atlas_geo_haversine` instead
    of function names.
    """
    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()

    labels = body["node_labels"]
    assert labels["repo_atlas_geo_haversine"] == "haversine"

    members = [m for c in body["communities"] for m in c["member_ids"]]
    assert members, "there should be something to draw"
    assert all(member in labels for member in members), (
        "every drawable member must have a label"
    )


async def test_the_audit_names_the_kind_of_each_record(env):
    """Colour and shape come from kind, so it travels with the report."""
    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()
    assert body["node_kinds"]["repo_atlas_geo_haversine"] == "symbol"


async def test_a_link_into_another_vault_is_not_disclosed(env):
    """Link edges cross scopes by design, so they need their own check.

    A scoped load pulls in `bridge` and `cross_repo` edges touching its nodes
    and then fetches whatever sits at the far end. Fetching that by id alone
    meant an edge into another vault's memory disclosed that vault's labels and
    graph shape through an otherwise-authorised repository audit.
    """
    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(
            KnowledgeObject(
                id="memory:atom:someone-else",
                kind="memory_atom",
                label="Another vault's private decision",
                scope="wiki:other",
                confidence=0.9,
                extraction_method="EXTRACTED",
                citations=[_citation("memory:atom:someone-else")],
                properties={"text": "Not for this vault."},
            )
        )
        await repo.upsert_relationship(
            KnowledgeRelationship(
                id="repo_atlas_geo_haversine__decided_in__memory:atom:someone-else",
                src_id="repo_atlas_geo_haversine",
                dst_id="memory:atom:someone-else",
                rel_type="decided_in",
                scope="bridge",
                confidence=0.8,
                extraction_method="INFERRED",
                citations=[_citation("memory:atom:someone-else")],
                properties={},
            )
        )
        await conn.commit()

    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()

    assert "memory:atom:someone-else" not in body["node_labels"]
    assert "Another vault's private decision" not in str(body)
    assert body["node_count"] == 2, "only this repository's own records"


async def test_the_audit_returns_the_edges_it_analysed(env):
    """A picture of clusters without edges cannot show how they connect.

    The report carried nodes and cluster membership but not the relationships
    between them, so anything drawing it could only place clusters in a ring
    around the owner — which is a layout, not the graph.
    """
    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()

    edges = body["edges"]
    assert edges, "the audit analysed edges; it should return them"
    first = edges[0]
    assert {"source", "target", "relation"} <= set(first)
    assert first["source"] == "repo_atlas_geo_haversine"
    assert first["relation"] == "calls"


async def test_edges_only_reference_nodes_the_report_includes(env):
    """An edge to a node the caller cannot see would be a dangling line."""
    body = env.get("/api/graph/audit", params={"scope": "repo:default:atlas"}).json()

    known = set(body["node_labels"])
    for edge in body["edges"]:
        assert edge["source"] in known and edge["target"] in known
