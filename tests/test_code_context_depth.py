"""What an agent actually receives when it asks about code.

Retrieval used to hand back a label, a kind and a `file:line` citation — roughly
ten names and their line numbers. An agent still had to open everything before
it could act, and the scope budget system did not apply because no `repo:` scope
had a budget row.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.code_repos import register_repo, run_pending_repo_indexing
from archivum.config import Settings
from archivum.knowledge.repository import KnowledgeRepository
from archivum.memory.registry import MemoryAssetRegistry
from archivum.retrieval.context import ContextRequest, build_context_package

GEO = '''"""Geo helpers."""


def haversine(lat: float, lon: float) -> float:
    """Distance on a sphere, in degrees."""
    return normalise(lat) + normalise(lon)


def normalise(value):
    return value % 360
'''


@pytest.fixture(autouse=True)
def _needs_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")


@pytest.fixture
def vault(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        code_cache_dir=tmp_path / "code-cache",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    return settings


def make_repo(root: Path) -> Path:
    repo = root / "atlas"
    repo.mkdir()
    (repo / "geo.py").write_text(GEO, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=d@e.com", "-c", "user.name=Dev",
         "commit", "-q", "-m", "Add geo helpers"],
        check=True,
    )
    return repo


async def _index(settings, tmp_path):
    await sqlite_mod.init_db(settings)
    await register_repo(path=make_repo(tmp_path), wiki_id="default")
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await run_pending_repo_indexing(settings=settings)


async def test_a_commit_is_linked_to_the_symbols_it_changed(vault, tmp_path, mock_kuzu_conn):
    """File-level attribution says a module moved; line-level says which function."""
    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        edges = await repo.list_relationships(scope="repo:default:atlas")
        objects = {o.id: o for o in await repo.list_objects(scope="repo:default:atlas", limit=200)}

    changed = [e for e in edges if e.rel_type == "changed_in"]
    assert changed, "the commit should be attached to what it changed"
    touched = {objects[e.src_id].label for e in changed if e.src_id in objects}
    assert "haversine" in touched


async def test_retrieved_code_carries_its_signature_and_summary(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        package = await build_context_package(
            KnowledgeRepository(conn),
            ContextRequest(query="haversine", scope="repo:default:atlas", wiki_id="default"),
        )

    node = next(n for n in package.nodes if n.label == "haversine")
    assert "lat" in node.signature
    assert node.summary == "Distance on a sphere, in degrees."


async def test_retrieved_code_can_carry_the_lines_it_cites(vault, tmp_path, mock_kuzu_conn):
    """A citation an agent has to go and resolve is a pointer, not context."""
    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        package = await build_context_package(
            KnowledgeRepository(conn),
            ContextRequest(
                query="haversine",
                scope="repo:default:atlas",
                wiki_id="default",
                include_source=True,
            ),
        )

    node = next(n for n in package.nodes if n.label == "haversine")
    assert "def haversine" in node.excerpt


async def test_source_is_left_out_unless_it_is_asked_for(vault, tmp_path, mock_kuzu_conn):
    """Mapping a repository should not cost the source of everything in it."""
    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        package = await build_context_package(
            KnowledgeRepository(conn),
            ContextRequest(query="haversine", scope="repo:default:atlas", wiki_id="default"),
        )

    assert all(node.excerpt == "" for node in package.nodes)


async def test_a_repository_gets_a_budget_when_it_is_registered(vault, tmp_path, mock_kuzu_conn):
    """Budgets live in the scope registry, so a scope with no row is unbounded."""
    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        scope = await MemoryAssetRegistry(conn).get_scope("repo:default:atlas", "default")

    assert scope is not None, "an indexed repository should be a budgeted scope"
    assert scope.scope_type == "repo"
    assert scope.budget_tokens > 0
