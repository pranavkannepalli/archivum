"""A session has to reach the code it changed, or it is just a transcript.

Capture stores the conversation. On its own that is prose in a blob: you can
search it, but you cannot ask "what happened to this function?" and get an
answer. These links are what make the work findable *from the code*.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.capture.schema import Conversation, ToolCall, Turn
from archivum.code_repos import register_repo, run_pending_repo_indexing
from archivum.config import Settings
from archivum.knowledge.repository import KnowledgeRepository
from archivum.sessions import record_session_work

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
         "commit", "-q", "-m", "initial"],
        check=True,
    )
    return repo


async def _indexed_repo(settings, tmp_path) -> Path:
    await sqlite_mod.init_db(settings)
    repo = make_repo(tmp_path)
    await register_repo(path=repo, wiki_id="default")
    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        await run_pending_repo_indexing(settings=settings)
    return repo


def _session(repo: Path, request: str) -> Conversation:
    return Conversation(
        session_id="s-1",
        interface="claude_code_import",
        started_at="2026-08-20T10:00:00Z",
        turns=(
            Turn(role="user", text=request),
            Turn(
                role="assistant",
                text="done",
                tool_calls=(
                    ToolCall(
                        name="Edit",
                        arguments={"file_path": str(repo / "geo.py")},
                        result="ok",
                    ),
                ),
            ),
        ),
    )


async def test_a_session_becomes_a_record_of_the_work(vault, tmp_path, mock_kuzu_conn):
    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_session(repo, "Fix the crash in haversine"),
            source_id="src-1",
            wiki_id="default",
        )
        session = await knowledge.get_object("session:src-1")

    assert session is not None
    assert session.properties["kind"] == "bugfix"
    assert session.properties["touched_paths"] == [str(repo / "geo.py")]
    assert session.citations, "a session record must cite the transcript it came from"


async def test_a_session_links_to_the_symbols_it_changed(vault, tmp_path, mock_kuzu_conn):
    """This is what makes "what happened to this function?" answerable."""
    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_session(repo, "Fix the crash in haversine"),
            source_id="src-1",
            wiki_id="default",
        )
        links = await knowledge.list_relationships(scope="bridge")

    touched = [rel for rel in links if rel.rel_type == "touched"]
    assert touched, "the session should reach the code it edited"
    assert all(rel.src_id == "session:src-1" for rel in touched)
    assert any(rel.dst_id.endswith("haversine") for rel in touched)


async def test_a_session_that_touched_no_registered_repo_links_to_nothing(vault, tmp_path, mock_kuzu_conn):
    await _indexed_repo(vault, tmp_path)
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_session(elsewhere, "Fix something else"),
            source_id="src-2",
            wiki_id="default",
        )
        links = await knowledge.list_relationships(scope="bridge")

    assert [rel for rel in links if rel.rel_type == "touched"] == []


async def test_recording_the_same_session_twice_does_not_duplicate_it(vault, tmp_path, mock_kuzu_conn):
    repo = await _indexed_repo(vault, tmp_path)
    conversation = _session(repo, "Fix the crash in haversine")

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        for _ in range(2):
            await record_session_work(
                knowledge, conversation=conversation, source_id="src-1", wiki_id="default"
            )
        links = await knowledge.list_relationships(scope="bridge")
        sessions = [
            obj
            for obj in await knowledge.list_objects(scope="wiki:default", limit=100)
            if obj.kind == "session"
        ]

    assert len(sessions) == 1
    assert len({rel.id for rel in links}) == len(links)


# ── Fix memory, stored and recalled ───────────────────────────────────────


def _bugfix_session(repo: Path) -> Conversation:
    return Conversation(
        session_id="s-fix",
        interface="claude_code_import",
        started_at="2026-08-20T10:00:00Z",
        turns=(
            Turn(role="user", text="haversine raises TypeError: bad operand on save"),
            Turn(
                role="assistant",
                text="normalise was returning a string.",
                tool_calls=(
                    ToolCall(name="Bash", arguments={"command": "pytest"}, result="1 failed", ok=False),
                    ToolCall(name="Edit", arguments={"file_path": str(repo / "geo.py")}, result="ok"),
                    ToolCall(name="Bash", arguments={"command": "pytest"}, result="ok", ok=True),
                ),
            ),
        ),
    )


async def test_a_bug_fix_session_leaves_a_fix_record(vault, tmp_path, mock_kuzu_conn):
    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_bugfix_session(repo),
            source_id="src-fix",
            wiki_id="default",
        )
        fix = await knowledge.get_object("fix:src-fix")

    assert fix is not None
    assert "TypeError" in fix.properties["symptom"]
    assert fix.properties["verified_by"] == "pytest"
    assert fix.properties["diagnosis"]


async def test_a_fix_links_to_the_symbols_it_repaired(vault, tmp_path, mock_kuzu_conn):
    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_bugfix_session(repo),
            source_id="src-fix",
            wiki_id="default",
        )
        links = await knowledge.list_relationships(scope="bridge")

    fixes = [rel for rel in links if rel.rel_type == "fixes"]
    assert fixes, "a fix should reach the code it repaired"
    assert any(rel.dst_id.endswith("haversine") for rel in fixes)


async def test_an_ordinary_session_leaves_no_fix_record(vault, tmp_path, mock_kuzu_conn):
    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_session(repo, "Add a settings page"),
            source_id="src-feat",
            wiki_id="default",
        )
        assert await knowledge.get_object("fix:src-feat") is None


async def test_the_same_error_recalls_the_earlier_fix(vault, tmp_path, mock_kuzu_conn):
    """The payoff: the error you are staring at, answered with what fixed it."""
    from archivum.fixes import recall_fixes

    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_bugfix_session(repo),
            source_id="src-fix",
            wiki_id="default",
        )
        found = await recall_fixes(
            knowledge, symptom="TypeError: bad operand", wiki_id="default"
        )

    assert [f.id for f in found] == ["fix:src-fix"]


async def test_an_unfamiliar_error_recalls_nothing(vault, tmp_path, mock_kuzu_conn):
    from archivum.fixes import recall_fixes

    repo = await _indexed_repo(vault, tmp_path)

    async with sqlite_mod.get_db() as conn:
        knowledge = KnowledgeRepository(conn)
        await record_session_work(
            knowledge,
            conversation=_bugfix_session(repo),
            source_id="src-fix",
            wiki_id="default",
        )
        assert await recall_fixes(
            knowledge, symptom="ZeroDivisionError: division by zero", wiki_id="default"
        ) == []
