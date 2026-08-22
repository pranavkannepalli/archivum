"""A repository's graph has to become files you own.

Everything else in Archivum ends up as markdown in the vault. Code was the sole
exception — canonical records and a lexical index, no files — so the one part of
a developer's memory that could not be read, edited, linked or exported was the
part about their own work.

Communities come from the deterministic graph audit, so the same repository
always produces the same pages.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.code_pages import repo_folder, slugify
from archivum.code_repos import register_repo, run_pending_repo_indexing
from archivum.config import Settings

# Two clusters that barely touch: a geo module and an unrelated retry module.
GEO = (
    "def haversine(lat, lon):\n"
    "    return normalise(lat) + normalise(lon)\n"
    "\n\n"
    "def normalise(value):\n"
    "    return value % 360\n"
)
RETRY = (
    "def with_backoff(fn):\n"
    "    return schedule(fn)\n"
    "\n\n"
    "def schedule(fn):\n"
    "    return fn\n"
)


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


def make_repo(root: Path, name: str = "atlas") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / "geo.py").write_text(GEO, encoding="utf-8")
    (repo / "retry.py").write_text(RETRY, encoding="utf-8")
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


async def test_indexing_writes_an_index_page_into_the_vault(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    index = vault.wiki_dir / "code" / "atlas" / "index.md"
    assert index.exists(), "a repository should leave a readable page behind"

    body = index.read_text(encoding="utf-8")
    assert "# atlas" in body
    assert "records" in body
    # Provenance is the point: the page must say what was extracted vs inferred.
    assert "extracted" in body.lower()


async def test_each_cluster_becomes_its_own_linked_page(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    folder = vault.wiki_dir / "code" / "atlas"
    cluster_pages = [p for p in folder.glob("*.md") if p.name != "index.md"]
    assert cluster_pages, "clusters should each get a page"

    index_body = (folder / "index.md").read_text(encoding="utf-8")
    for page in cluster_pages:
        slug = f"code/atlas/{page.stem}"
        assert f"[[{slug}" in index_body, f"{slug} should be linked from the index"


async def test_cluster_pages_cite_the_file_and_line_they_came_from(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    folder = vault.wiki_dir / "code" / "atlas"
    bodies = "\n".join(
        page.read_text(encoding="utf-8") for page in folder.glob("*.md")
    )
    assert "haversine" in bodies
    assert ".py" in bodies, "records should name the file they were read from"


async def test_code_pages_are_real_indexed_pages_not_loose_files(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    rows = await sqlite_mod.list_pages("default")
    slugs = {row["slug"] for row in rows}
    assert "code/atlas/index" in slugs, "the page must go through the one indexing path"


async def test_reindexing_an_unchanged_repo_rewrites_the_same_pages(vault, tmp_path, mock_kuzu_conn):
    """Deterministic clustering means no churn when nothing changed."""
    await _index(vault, tmp_path)
    folder = vault.wiki_dir / "code" / "atlas"
    before = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.md")}

    await register_repo(path=tmp_path / "atlas", wiki_id="default")
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await run_pending_repo_indexing(settings=vault)

    after = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.md")}
    assert after == before


def test_folder_and_slug_helpers_are_vault_safe():
    assert repo_folder("My Repo") == "code/my-repo"
    assert slugify("Cluster: retry & backoff") == "cluster-retry-backoff"
    assert slugify("///") == "untitled"


async def test_locations_are_repo_relative_not_absolute_server_paths(vault, tmp_path, mock_kuzu_conn):
    """A page you read should say `geo.py:12-18`, not the server's temp path.

    Citations record where the file was on the machine that indexed it. Printing
    that verbatim leaks the deployment's filesystem layout into your vault and
    buries the only part that matters — which file, which lines.
    """
    await _index(vault, tmp_path)

    bodies = "\n".join(
        page.read_text(encoding="utf-8")
        for page in (vault.wiki_dir / "code" / "atlas").glob("*.md")
    )
    assert str(tmp_path) not in bodies, "absolute host paths must not reach the vault"
    assert "geo.py" in bodies


async def test_line_ranges_read_as_line_numbers(vault, tmp_path, mock_kuzu_conn):
    """`geo.py:1-6`, not `geo.py:1-L6`."""
    import re

    await _index(vault, tmp_path)

    bodies = "\n".join(
        page.read_text(encoding="utf-8")
        for page in (vault.wiki_dir / "code" / "atlas").glob("*.md")
    )
    assert not re.search(r":\d+-L\d+", bodies), "half-stripped line ranges"
    assert re.search(r"\.py:\d+-\d+", bodies), "a location should name its lines"


async def test_repo_metadata_does_not_get_a_cluster_page(vault, tmp_path, mock_kuzu_conn):
    """The repo node and its commit cluster together and say nothing.

    They are bookkeeping, not structure, and a page titled after the repository
    that contains only "this repo, that commit" competes with the real index.
    """
    await _index(vault, tmp_path)

    folder = vault.wiki_dir / "code" / "atlas"
    assert not (folder / "atlas.md").exists()
    assert (folder / "index.md").exists()


async def test_the_index_lists_what_broke_and_what_fixed_it(vault, tmp_path, mock_kuzu_conn):
    """The vault is where you read, so this is where fix memory belongs."""
    from archivum.capture.schema import Conversation, ToolCall, Turn
    from archivum.knowledge.repository import KnowledgeRepository
    from archivum.sessions import record_session_work

    await _index(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        await record_session_work(
            KnowledgeRepository(conn),
            conversation=Conversation(
                session_id="s",
                interface="claude_code_import",
                started_at="",
                turns=(
                    Turn(role="user", text="haversine crashes with TypeError: bad operand"),
                    Turn(
                        role="assistant",
                        text="normalise returned a string instead of a number.",
                        tool_calls=(
                            ToolCall(
                                name="Edit",
                                arguments={"file_path": str(tmp_path / "atlas" / "geo.py")},
                                result="ok",
                            ),
                        ),
                    ),
                ),
            ),
            source_id="src-fix",
            wiki_id="default",
        )
        await conn.commit()

    from archivum.code_repos import get_repo
    from archivum.code_pages import write_repo_pages

    repo = await get_repo("repo:default:atlas", wiki_id="default")
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await write_repo_pages(repo, settings=vault)

    body = (vault.wiki_dir / "code" / "atlas" / "index.md").read_text(encoding="utf-8")
    assert "TypeError" in body, "a remembered fix should be readable in the vault"
    assert "normalise returned a string" in body, "including what caused it"


async def test_a_repository_with_no_remembered_fixes_says_nothing_about_them(vault, tmp_path, mock_kuzu_conn):
    await _index(vault, tmp_path)

    body = (vault.wiki_dir / "code" / "atlas" / "index.md").read_text(encoding="utf-8")
    assert "What broke before" not in body
