import pytest

from archivum.capture.schema import Conversation, ToolCall, Turn
from archivum.capture.store import CaptureStore
from archivum.config import Settings
from archivum.db import sqlite
from archivum.knowledge.personal_root import SELF_ID, ensure_personal_root
from archivum.knowledge.repository import KnowledgeRepository
from archivum.mcp import server
from archivum.memory.registry import MemoryAssetRegistry
from archivum.memory.service import activate_asset
from archivum.store.blobs import BlobStore
from archivum.store.repository import SourceStore


@pytest.fixture(autouse=True)
def _direct_tool_calls_bypass_transport_auth():
    """These tests call `@mcp.tool()` functions directly, not through a
    transport — there is no request to carry a bearer. That is the same kind
    of trusted local invocation stdio is exempt for, not a network call that
    needs a device key, so run tool calls here as if over stdio.
    """
    server.set_transport("stdio")
    try:
        yield
    finally:
        server.set_transport("http")


@pytest.fixture
def temp_settings(tmp_path):
    return Settings(
        db_path=tmp_path / "archivum.db",
        wiki_dir=tmp_path / "wiki",
        raw_dir=tmp_path / "raw",
        kuzu_path=tmp_path / "kuzu",
        blob_dir=tmp_path / "blobs",
        memory_page_views_enabled=False,
    )


@pytest.fixture
async def env(temp_settings, monkeypatch):
    monkeypatch.setattr(server, "settings", temp_settings)
    await sqlite.init_db(temp_settings)
    return temp_settings, CaptureStore(
        store=SourceStore(),
        blob_store=BlobStore(temp_settings.blob_dir),
        settings=temp_settings,
    )


def _conversation(session_id="s1"):
    return Conversation(
        session_id=session_id,
        interface="claude_code_native",
        started_at="2026-08-12T00:00:00Z",
        turns=(
            Turn(role="user", text="I prefer uv over pip. Deploy the stack."),
            Turn(
                role="assistant",
                text="ok",
                tool_calls=(
                    ToolCall(name="Write", arguments={"file_path": "/a"}, result="ok"),
                    ToolCall(name="Bash", arguments={"command": "up"}, result="ok"),
                    ToolCall(name="Bash", arguments={"command": "pytest"}, result="ok"),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_distill_source_reports_an_unknown_source(env):
    result = await server.distill_source("source:ghost")
    assert result["error"] == "source_not_distillable"


@pytest.mark.asyncio
async def test_distill_source_produces_cited_layered_memory(env):
    _, store = env
    captured = await store.capture(_conversation())

    result = await server.distill_source(captured.source_id, scenario_key="archivum")
    assert result["atoms_accepted"] >= 1
    assert result["scenario_id"] == "memory:scenario:wiki:default:archivum"
    assert result["skill_id"]
    assert result["pages_written"] == []


@pytest.mark.asyncio
async def test_list_memory_assets_filters_by_status(env):
    _, store = env
    captured = await store.capture(_conversation())
    await server.distill_source(captured.source_id)

    active = await server.list_memory_assets(status="active")
    drafts = await server.list_memory_assets(status="draft")
    # Distillation output is review-gated: nothing activates on its own.
    assert active["assets"] == []
    assert {asset["asset_type"] for asset in drafts["assets"]} >= {"chat", "skill"}


@pytest.mark.asyncio
async def test_load_agent_memory_returns_only_bound_active_assets(env):
    _, store = env
    captured = await store.capture(_conversation())
    report = await server.distill_source(captured.source_id)
    chat_id = f"memory:chat:{captured.source_id}"

    async with sqlite.get_db() as conn:
        registry = MemoryAssetRegistry(conn)
        # Reviewing the chat memory is what makes it agent-loadable.
        await activate_asset(conn, KnowledgeRepository(conn), chat_id, approved_by="owner")
        await registry.upsert_agent(agent_key="coder", wiki_id="default", name="Coder")
        await registry.bind_asset(
            agent_key="coder", wiki_id="default", asset_id=chat_id
        )
        # The skill is still a draft, so binding it must not leak it.
        await registry.bind_asset(
            agent_key="coder", wiki_id="default", asset_id=report["skill_id"]
        )

    package = await server.load_agent_memory("coder")
    assert [entry["asset"]["id"] for entry in package["entries"]] == [chat_id]
    assert package["citations"]


@pytest.mark.asyncio
async def test_catalog_memory_assets_registers_existing_memory(env):
    _, store = env
    await store.capture(_conversation(session_id="catalog"))
    await sqlite.upsert_page("notes", "Notes", "# Notes", ["note"], "user")

    report = await server.catalog_memory_assets()
    assert report["wiki_assets"] == 1
    assert report["source_assets"] == 1


@pytest.mark.asyncio
async def test_load_agent_memory_explains_an_unknown_agent(env):
    package = await server.load_agent_memory("ghost")
    assert package["entries"] == []
    assert "ghost" in package["reason"]


@pytest.mark.asyncio
async def test_graph_audit_report_tool_returns_a_narrative(env):
    _, store = env
    captured = await store.capture(_conversation())
    await server.distill_source(captured.source_id)

    report = await server.graph_audit_report()
    assert report["node_count"] > 0
    assert report["narrative"]
    assert "communities" in report


@pytest.mark.asyncio
async def test_graph_shortest_path_tool_walks_owner_to_memory(env):
    _, store = env
    captured = await store.capture(_conversation())
    await server.distill_source(captured.source_id)

    async with sqlite.get_db() as conn:
        await ensure_personal_root(KnowledgeRepository(conn))

    path = await server.graph_shortest_path(
        SELF_ID, f"memory:chat:{captured.source_id}"
    )
    assert path["found"] is True
    assert path["steps"][0]["relation"] == "remembers"
