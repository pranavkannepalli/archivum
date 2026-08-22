"""Agents should be able to reach code memory, not just prose memory.

Archivum's whole point for a developer is that an agent can ask what the code
is and why it is that way. Every other kind of memory had an MCP tool; code had
none, so the repository graph was invisible to the agents it was built for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings

GEO = (
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


@pytest.fixture
def vault(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        code_cache_dir=tmp_path / "code-cache",
        mcp_api_key="",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("archivum.mcp.server.settings", settings, raising=False)
    return settings


def make_repo(root: Path) -> Path:
    repo = root / "atlas"
    repo.mkdir()
    (repo / "geo.py").write_text(GEO, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.com", "-c", "user.name=T",
         "commit", "-q", "-m", "x"],
        check=True,
    )
    return repo


async def test_an_agent_can_index_a_repository(vault, tmp_path, mock_kuzu_conn):
    from archivum.mcp.server import index_repository

    await sqlite_mod.init_db(vault)
    repo = make_repo(tmp_path)

    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        result = await index_repository(str(repo))

    assert result["scope"] == "repo:default:atlas"
    assert result["status"] == "ready"
    assert result["nodes"] > 0
    assert result["pages"] > 0, "indexing should leave readable pages behind"


async def test_an_agent_can_list_what_repositories_are_known(vault, tmp_path, mock_kuzu_conn):
    from archivum.mcp.server import index_repository, list_repositories

    await sqlite_mod.init_db(vault)
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await index_repository(str(make_repo(tmp_path)))

    listed = await list_repositories()
    assert [entry["name"] for entry in listed] == ["atlas"]


async def test_an_agent_asking_about_code_gets_cited_records(vault, tmp_path, mock_kuzu_conn):
    from archivum.mcp.server import index_repository, retrieve_code_context

    await sqlite_mod.init_db(vault)
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await index_repository(str(make_repo(tmp_path)))

    package = await retrieve_code_context("haversine", repo="atlas")
    labels = {node["label"] for node in package["nodes"]}
    assert "haversine" in labels
    assert package["citations"], "code context must carry citations into files"


async def test_an_agent_can_recall_a_fix_from_an_error(vault, tmp_path, mock_kuzu_conn):
    """The payoff at 2am: paste the error, get what fixed it last time."""
    from archivum.capture.schema import Conversation, ToolCall, Turn
    from archivum.knowledge.repository import KnowledgeRepository
    from archivum.mcp.server import recall_fix
    from archivum.sessions import record_session_work

    await sqlite_mod.init_db(vault)
    async with sqlite_mod.get_db() as conn:
        await record_session_work(
            KnowledgeRepository(conn),
            conversation=Conversation(
                session_id="s",
                interface="claude_code_import",
                started_at="",
                turns=(
                    Turn(role="user", text="save blows up with KeyError: 'slug'"),
                    Turn(
                        role="assistant",
                        text="The frontmatter parser dropped the key.",
                        tool_calls=(
                            ToolCall(name="Edit", arguments={"file_path": "/a.py"}, result="ok"),
                        ),
                    ),
                ),
            ),
            source_id="src-1",
            wiki_id="default",
        )
        await conn.commit()

    found = await recall_fix("KeyError: 'title' when saving")

    assert found["fixes"], found
    assert "KeyError" in found["fixes"][0]["symptom"]
    assert found["fixes"][0]["diagnosis"]


async def test_recalling_an_unfamiliar_error_says_so(vault, tmp_path, mock_kuzu_conn):
    from archivum.mcp.server import recall_fix

    await sqlite_mod.init_db(vault)
    found = await recall_fix("ZeroDivisionError: division by zero")

    assert found["fixes"] == []
    assert found["reason"], "an empty answer should explain itself"


async def test_an_agent_can_record_work_it_judged_worth_keeping(vault, tmp_path, mock_kuzu_conn):
    """Capture is automatic, but an agent still knows when something mattered."""
    from archivum.knowledge.repository import KnowledgeRepository
    from archivum.mcp.server import record_work

    await sqlite_mod.init_db(vault)
    result = await record_work(
        request="the deploy hangs on migrate",
        outcome="Postgres lock held by a stale session; killed it and added a timeout.",
        changed_paths=["/deploy/migrate.sh"],
        verified_by="make deploy",
    )

    assert result["recorded"]
    async with sqlite_mod.get_db() as conn:
        stored = await KnowledgeRepository(conn).get_object(result["id"])
    assert stored is not None
    assert stored.kind == "session"


async def test_an_agent_can_ask_what_the_vault_is_about(vault, tmp_path, mock_kuzu_conn):
    """Global questions read summaries; no single record holds the answer."""
    from unittest.mock import AsyncMock as _AsyncMock

    from archivum.knowledge.models import Citation, KnowledgeObject
    from archivum.knowledge.repository import KnowledgeRepository
    from archivum.mcp.server import summarise_vault, vault_themes

    await sqlite_mod.init_db(vault)
    async with sqlite_mod.get_db() as conn:
        repo = KnowledgeRepository(conn)
        for id_, label in [("a", "Retrieval design"), ("b", "Chunking strategy")]:
            await repo.upsert_object(
                KnowledgeObject(
                    id=id_, kind="page", label=label, scope="wiki:default",
                    confidence=1.0, extraction_method="USER_AUTHORED",
                    citations=[Citation(source_id=id_, chunk_id=id_, span_start=None, span_end=None, quote=label)],
                    properties={},
                )
            )
        from archivum.knowledge.models import KnowledgeRelationship

        await repo.upsert_relationship(
            KnowledgeRelationship(
                id="a->b", src_id="a", dst_id="b", rel_type="references",
                scope="wiki:default", confidence=1.0, extraction_method="USER_AUTHORED",
                citations=[Citation(source_id="a", chunk_id="a", span_start=None, span_end=None, quote="x")],
                properties={},
            )
        )
        await conn.commit()

    with patch(
        "archivum.summaries.cli_chat_completion",
        new=_AsyncMock(return_value="How the system finds things."),
    ):
        written = await summarise_vault()

    assert written["summaries"] >= 1
    themes = await vault_themes()
    assert themes["themes"], themes
    assert "finds things" in themes["themes"][0]["summary"]
    assert themes["themes"][0]["citations"], "a theme is prose; it must cite"
