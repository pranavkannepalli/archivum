from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import archivum.db.sqlite as sqlite_mod
from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.capture.schema import Conversation, ToolCall, Turn
from archivum.capture.store import CaptureStore
from archivum.config import Settings, get_settings
from archivum.main import create_app
from archivum.store.blobs import BlobStore
from archivum.store.repository import SourceStore


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(db_path=tmp_path / "archivum.db", blob_dir=tmp_path / "blobs")
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

    store = CaptureStore(
        store=SourceStore(), blob_store=BlobStore(settings.blob_dir), settings=settings
    )
    # No lifespan: the fixture already initialised the schema against tmp paths.
    yield TestClient(app, raise_server_exceptions=True), store


def _asset_payload(**overrides):
    payload = {
        "id": "memory:wiki:notes",
        "asset_type": "wiki",
        "layer": "L1",
        "name": "Notes",
        "summary": "Editable notes",
        "body": "# Notes",
        "tags": ["wiki"],
        "citations": [
            {
                "source_id": "source:1",
                "chunk_id": "chunk:1",
                "span_start": 0,
                "span_end": 5,
                "quote": "Notes",
            }
        ],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_register_list_and_fetch_an_asset(env):
    client, _ = env
    created = client.post("/api/memory/assets", json=_asset_payload())
    assert created.status_code == 201
    assert created.json()["version"] == 1
    assert created.json()["status"] == "draft"

    listed = client.get("/api/memory/assets")
    assert [asset["id"] for asset in listed.json()] == ["memory:wiki:notes"]

    fetched = client.get("/api/memory/assets/memory:wiki:notes")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Notes"


@pytest.mark.asyncio
async def test_memory_scope_routes_round_trip_budget_and_retention(env):
    client, _ = env

    created = client.post(
        "/api/memory/scopes",
        json={
            "id": "topic:clean-memory",
            "scope_type": "topic",
            "name": "Clean memory",
            "parent_scope_id": "person:self",
            "budget_tokens": 3_000,
            "budget_items": 12,
            "retention_policy": {"candidate_ttl_days": 14},
        },
    )

    assert created.status_code == 201
    assert created.json()["id"] == "topic:clean-memory"
    assert created.json()["retention_policy"] == {"candidate_ttl_days": 14}

    listed = client.get("/api/memory/scopes").json()
    assert "person:self" in {scope["id"] for scope in listed}
    assert "topic:clean-memory" in {scope["id"] for scope in listed}

    topics = client.get("/api/memory/scopes", params={"scope_type": "topic"}).json()
    assert [scope["id"] for scope in topics] == ["topic:clean-memory"]


@pytest.mark.asyncio
async def test_unknown_asset_is_a_404(env):
    client, _ = env
    response = client.get("/api/memory/assets/memory:wiki:ghost")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "asset_not_found"


@pytest.mark.asyncio
async def test_invalid_asset_type_is_rejected(env):
    client, _ = env
    response = client.post("/api/memory/assets", json=_asset_payload(asset_type="junk"))
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_memory_asset"


@pytest.mark.asyncio
async def test_edits_create_versions_and_status_transitions_do_not(env):
    client, _ = env
    client.post("/api/memory/assets", json=_asset_payload())
    client.post("/api/memory/assets", json=_asset_payload(body="# Notes v2"))

    versions = client.get("/api/memory/assets/memory:wiki:notes/versions").json()
    assert [v["version"] for v in versions] == [2, 1]

    activated = client.post(
        "/api/memory/assets/memory:wiki:notes/status", json={"status": "active"}
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    assert activated.json()["version"] == 2

    shared = client.post(
        "/api/memory/assets/memory:wiki:notes/visibility", json={"visibility": "shared"}
    )
    assert shared.json()["visibility"] == "shared"


@pytest.mark.asyncio
async def test_asset_api_exposes_review_lineage(env):
    client, _ = env
    old = client.post(
        "/api/memory/assets",
        json=_asset_payload(
            id="memory:decision:old",
            asset_type="wiki",
            layer="L2",
            name="Old direction",
        ),
    ).json()

    new = client.post(
        "/api/memory/assets",
        json=_asset_payload(
            id="memory:decision:new",
            asset_type="wiki",
            layer="L2",
            name="New direction",
            supersedes=[old["id"]],
            conflict_lineage=["suggestion:conflict"],
            approved_by="owner",
        ),
    ).json()

    assert new["supersedes"] == [old["id"]]
    assert new["conflict_lineage"] == ["suggestion:conflict"]
    assert new["approved_by"] == "owner"
    assert new["reviewed_at"]
    assert client.get("/api/memory/assets/memory:decision:old").json()["superseded_by"] == [
        new["id"]
    ]


@pytest.mark.asyncio
async def test_invalid_status_is_rejected(env):
    client, _ = env
    client.post("/api/memory/assets", json=_asset_payload())
    response = client.post(
        "/api/memory/assets/memory:wiki:notes/status", json={"status": "maybe"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_asset_status"


@pytest.mark.asyncio
async def test_agent_bindings_drive_the_loadout(env):
    client, _ = env
    client.post("/api/memory/assets", json=_asset_payload())
    client.post("/api/memory/assets/memory:wiki:notes/status", json={"status": "active"})
    client.post("/api/memory/agents", json={"agent_key": "coder", "name": "Coder"})

    bound = client.post(
        "/api/memory/agents/coder/bindings",
        json={"asset_id": "memory:wiki:notes", "priority": 10},
    )
    assert bound.status_code == 201

    loadout = client.get("/api/memory/agents/coder/loadout").json()
    assert [entry["asset"]["id"] for entry in loadout["entries"]] == ["memory:wiki:notes"]
    assert loadout["insufficient_evidence"] is False

    removed = client.delete("/api/memory/agents/coder/bindings/memory:wiki:notes")
    assert removed.json() == {"removed": True}
    assert client.get("/api/memory/agents/coder/loadout").json()["entries"] == []


@pytest.mark.asyncio
async def test_binding_an_unknown_asset_is_a_404(env):
    client, _ = env
    client.post("/api/memory/agents", json={"agent_key": "coder", "name": "Coder"})
    response = client.post(
        "/api/memory/agents/coder/bindings", json={"asset_id": "memory:wiki:ghost"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "binding_target_not_found"


@pytest.mark.asyncio
async def test_loadout_for_an_unknown_agent_explains_itself(env):
    client, _ = env
    loadout = client.get("/api/memory/agents/ghost/loadout").json()
    assert loadout["entries"] == []
    assert "ghost" in loadout["reason"]


@pytest.mark.asyncio
async def test_catalog_brings_existing_pages_and_sources_under_governance(env):
    client, store = env
    await sqlite_mod.upsert_page("notes", "Notes", "# Notes", ["note"], "user")
    await store.capture(
        Conversation(
            session_id="s0",
            interface="claude_code_native",
            started_at="2026-08-12T00:00:00Z",
            turns=(Turn(role="user", text="hello there"),),
        )
    )

    body = client.post("/api/memory/catalog").json()
    assert body["wiki_assets"] == 1
    assert body["source_assets"] == 1
    assert body["codegraph_assets"] == 0

    types = {
        asset["asset_type"] for asset in client.get("/api/memory/assets").json()
    }
    assert types == {"wiki", "source"}


@pytest.mark.asyncio
async def test_distilling_an_unknown_source_is_a_404(env):
    client, _ = env
    response = client.post("/api/memory/distill", json={"source_id": "source:ghost"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "source_not_distillable"


@pytest.mark.asyncio
async def test_distill_endpoint_produces_cited_memory_assets(env):
    client, store = env
    conv = Conversation(
        session_id="s1",
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
    captured = await store.capture(conv)

    response = client.post(
        "/api/memory/distill",
        json={
            "source_id": captured.source_id,
            "scenario_key": "archivum",
            "write_pages": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["atoms_accepted"] >= 1
    assert body["scenario_id"] == "memory:scenario:wiki:default:archivum"
    assert body["skill_id"]
    assert body["pages_written"] == []

    chat = client.get("/api/memory/assets", params={"asset_type": "chat"}).json()
    assert chat[0]["citations"]
    assert chat[0]["layer"] == "L1"

    skills = client.get("/api/memory/assets", params={"asset_type": "skill"}).json()
    assert skills[0]["status"] == "draft"


@pytest.mark.asyncio
async def test_owner_filter_returns_the_memory_the_profile_page_asks_for(env):
    """`/me` asks "what are my agents told about me?" — that is an owner question.

    Assets are scoped to the wiki they belong to and owned by `person:self`.
    The profile page filtered on `scope=person:self`, which no asset ever
    carries, so the section was permanently empty while the count beside it
    said otherwise.
    """
    client, store = env
    captured = await store.capture(
        Conversation(
            session_id="s-owner",
            interface="claude_code_native",
            started_at="2026-08-12T00:00:00Z",
            turns=(Turn(role="user", text="I prefer uv over pip."),),
        )
    )
    client.post(
        "/api/memory/distill",
        json={"source_id": captured.source_id, "write_pages": False},
    )

    everything = client.get("/api/memory/assets").json()
    assert everything, "distillation should have registered at least one asset"

    owned = client.get("/api/memory/assets", params={"owner": "person:self"}).json()
    assert [asset["id"] for asset in owned] == [asset["id"] for asset in everything]

    others = client.get("/api/memory/assets", params={"owner": "person:nobody"}).json()
    assert others == []


@pytest.mark.asyncio
async def test_distill_threshold_override_routes_everything_to_review(env):
    client, store = env
    conv = Conversation(
        session_id="s2",
        interface="claude_code_native",
        started_at="2026-08-12T00:00:00Z",
        turns=(Turn(role="user", text="I prefer uv over pip."),),
    )
    captured = await store.capture(conv)

    body = client.post(
        "/api/memory/distill",
        json={"source_id": captured.source_id, "threshold": 1.0, "write_pages": False},
    ).json()
    assert body["atoms_accepted"] == 0
    assert body["atoms_pending_review"] == 1

    pending = client.get("/api/suggestions").json()
    assert [item["suggestion_type"] for item in pending] == ["memory_atom"]


@pytest.mark.asyncio
async def test_an_agent_with_no_profile_still_gets_the_owner_s_active_memory(env):
    """A fresh vault should not hand an agent an empty package and a shrug.

    Loadouts needed a profile and bindings someone created by hand, and no
    screen created them — so `load_agent_memory` returned nothing on every new
    install. Active assets are already owner-approved, so they are the honest
    default until a profile says otherwise.
    """
    client, store = env
    created = client.post("/api/memory/assets", json=_asset_payload())
    assert created.status_code == 201
    client.post("/api/memory/assets/memory%3Awiki%3Anotes/status", json={"status": "active"})

    loadout = client.get("/api/memory/agents/never-configured/loadout").json()

    assert loadout["entries"], loadout
    assert loadout["insufficient_evidence"] is False
    assert "default" in loadout["reason"].lower()


@pytest.mark.asyncio
async def test_the_default_loadout_leaves_out_what_was_never_activated(env):
    """Draft assets are proposals. An agent is handed decisions, not proposals."""
    client, _ = env
    client.post("/api/memory/assets", json=_asset_payload())

    loadout = client.get("/api/memory/agents/never-configured/loadout").json()

    assert loadout["entries"] == []
