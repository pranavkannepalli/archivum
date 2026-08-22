"""Repositories are memory too, and have to be reachable from the app.

Archgraph could already read a repo into canonical knowledge, but the only way
to run it was a console script that no route, tool, or screen ever called — so
on a running Archivum there was no way to index a repo at all. These cover the
path from "register this repo" to "its code is retrievable".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import archivum.db.sqlite as sqlite_mod
from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.code_repos import run_pending_repo_indexing
from archivum.config import Settings, get_settings
from archivum.knowledge.repository import KnowledgeRepository
from archivum.main import create_app
from archivum.memory.registry import MemoryAssetRegistry
from archivum.retrieval.context import ContextRequest, build_context_package

CALC = (
    "def haversine(lat, lon):\n"
    "    return normalise(lat) + normalise(lon)\n"
    "\n\n"
    "def normalise(value):\n"
    "    return value % 360\n"
)


@pytest.fixture(autouse=True)
def _needs_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")


def make_repo(root: Path, name: str = "atlas") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / "geo.py").write_text(CALC, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@example.com", "-c", "user.name=Test",
            "commit", "-q", "-m", "initial",
        ],
        check=True,
    )
    return repo


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

    yield TestClient(app, raise_server_exceptions=True), settings


async def _index_all(settings):
    """Drain the queue the way the background worker does."""
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        return await run_pending_repo_indexing(settings=settings)


async def test_registering_a_repo_queues_it_and_indexing_fills_in_the_counts(env, tmp_path, mock_kuzu_conn):
    client, settings = env
    repo = make_repo(tmp_path)

    created = client.post("/api/repos", json={"path": str(repo)})
    assert created.status_code == 201, created.text
    assert created.json()["scope"] == "repo:default:atlas"
    # Indexing is queued, never inline: a large repo must not block the server.
    assert created.json()["status"] == "pending"

    assert await _index_all(settings) == 1

    listed = client.get("/api/repos").json()
    assert [repo_["scope"] for repo_ in listed] == ["repo:default:atlas"]
    assert listed[0]["status"] == "ready"
    assert listed[0]["files"] == 1
    assert listed[0]["nodes"] > 0
    assert listed[0]["error"] is None


async def test_an_indexed_repo_puts_its_symbols_in_canonical_knowledge(env, tmp_path, mock_kuzu_conn):
    client, settings = env
    client.post("/api/repos", json={"path": str(make_repo(tmp_path))})
    await _index_all(settings)

    async with sqlite_mod.get_db() as conn:
        objects = await KnowledgeRepository(conn).list_objects(scope="repo:default:atlas", limit=100)

    labels = {object_.label for object_ in objects}
    assert "haversine" in labels
    assert "normalise" in labels


async def test_code_retrieval_finds_the_lexical_index_the_server_wrote(env, tmp_path, mock_kuzu_conn):
    """The index has to live where the server looks for it.

    Ingest wrote the lexical index into a database inside the repo's own cache
    directory, while retrieval looked for it in the application database. The
    tables were never in the same file, so lexical seeding never once ran in a
    deployed Archivum and every code query silently fell back.
    """
    client, settings = env
    client.post("/api/repos", json={"path": str(make_repo(tmp_path))})
    await _index_all(settings)

    async with sqlite_mod.get_db() as conn:
        package = await build_context_package(
            KnowledgeRepository(conn),
            ContextRequest(query="haversine", scope="repo:default:atlas", wiki_id="default"),
        )

    assert package.nodes, "a code query should return code"
    assert "haversine" in {node.label for node in package.nodes}
    assert package.seeds, "lexical seeding should have chosen a seed"


async def test_indexing_registers_the_code_graph_as_governed_memory(env, tmp_path, mock_kuzu_conn):
    client, settings = env
    client.post("/api/repos", json={"path": str(make_repo(tmp_path))})
    await _index_all(settings)

    async with sqlite_mod.get_db() as conn:
        assets = await MemoryAssetRegistry(conn).list_assets(
            wiki_id="default", asset_type="codegraph"
        )

    assert [asset.id for asset in assets] == ["codegraph:repo:default:atlas"]
    assert assets[0].layer == "L2"
    assert assets[0].owner == "person:self"


async def test_registering_a_path_that_is_not_a_directory_is_rejected(env):
    client, _ = env
    response = client.post("/api/repos", json={"path": "/definitely/not/here"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "repo_not_found"
