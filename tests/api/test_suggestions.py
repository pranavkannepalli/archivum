from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from archivum.auth import create_access_token
from archivum.config import get_settings
from archivum.knowledge.models import Citation
from archivum.knowledge.suggestions import SuggestionRepository, init_suggestion_schema
from archivum.main import create_app
from archivum.memory.registry import MemoryAssetRegistry, init_memory_schema


def _client_for_wiki(wiki_id: str, role: str = "owner") -> TestClient:
    settings = get_settings()
    token = create_access_token("owner", role, wiki_id, settings)
    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
    ):
        app = create_app()
    client = TestClient(app, raise_server_exceptions=True)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


async def _seed_suggestion(
    db_path,
    *,
    target_id: str,
    proposed_markdown: str = "## Suggested\n\n- [[Beta]]",
    rationale: str = "",
):
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await init_memory_schema(conn)
        await init_suggestion_schema(conn)
        return await SuggestionRepository(conn).create_suggestion(
            target_id=target_id,
            suggestion_type="append_section",
            proposed_markdown=proposed_markdown,
            proposed_objects=[],
            citations=[],
            rationale=rationale,
        )


async def _seed_memory_asset(
    db_path,
    asset_id: str,
    *,
    status: str = "active",
    wiki_id: str = "alpha",
):
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await init_memory_schema(conn)
        return await MemoryAssetRegistry(conn).register_asset(
            id=asset_id,
            wiki_id=wiki_id,
            asset_type="wiki",
            layer="L2",
            name=asset_id,
            scope=f"wiki:{wiki_id}",
            status=status,
            summary="existing",
            body="Existing memory",
            citations=[
                Citation(
                    source_id="source:seed",
                    chunk_id="chunk:seed",
                    span_start=0,
                    span_end=4,
                    quote="seed",
                )
            ],
        )


def _patch_suggestion_db(monkeypatch: pytest.MonkeyPatch, db_path) -> None:
    from archivum.api import suggestions as suggestions_api

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(suggestions_api.sqlite, "get_db", get_db)


def test_suggestion_routes_require_auth():
    client = TestClient(create_app(), raise_server_exceptions=True)
    response = client.get("/api/suggestions")
    assert response.status_code == 401


def test_list_suggestions_returns_only_authenticated_wiki(tmp_path, monkeypatch):
    owned = asyncio.run(
        _seed_suggestion(tmp_path / "suggestions.db", target_id="page:alpha:home")
    )
    asyncio.run(
        _seed_suggestion(
            tmp_path / "suggestions.db",
            target_id="page:other:home",
            proposed_markdown="## Other wiki",
        )
    )
    _patch_suggestion_db(monkeypatch, tmp_path / "suggestions.db")

    response = _client_for_wiki("alpha").get("/api/suggestions")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [owned.id]
    assert body[0]["target_id"] == "page:alpha:home"


def test_list_page_suggestions_uses_authenticated_wiki_scope(tmp_path, monkeypatch):
    match = asyncio.run(
        _seed_suggestion(
            tmp_path / "suggestions.db", target_id="page:alpha:notes/home"
        )
    )
    asyncio.run(
        _seed_suggestion(
            tmp_path / "suggestions.db", target_id="page:other:notes/home"
        )
    )
    _patch_suggestion_db(monkeypatch, tmp_path / "suggestions.db")

    response = _client_for_wiki("alpha").get("/api/suggestions?page_slug=notes/home")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [match.id]


def test_accept_reject_enforce_scope_and_conflicts(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    owned = asyncio.run(_seed_suggestion(db_path, target_id="page:alpha:home"))
    other = asyncio.run(_seed_suggestion(db_path, target_id="page:other:home"))
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")

    accepted = client.post(f"/api/suggestions/{owned.id}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    conflict = client.post(f"/api/suggestions/{owned.id}/reject")
    assert conflict.status_code == 409

    hidden = client.post(f"/api/suggestions/{other.id}/accept")
    assert hidden.status_code == 404


def test_review_action_route_supports_merge_replace_keep_retire_scope_visibility(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    asyncio.run(_seed_memory_asset(db_path, "memory:existing"))

    for action, expected in [
        ("merge", "merged"),
        ("replace", "replaced"),
        ("keep_both", "kept"),
        ("retire", "retired"),
    ]:
        suggestion = asyncio.run(
            _seed_suggestion(db_path, target_id=f"page:alpha:{action}")
        )
        response = client.post(
            f"/api/suggestions/{suggestion.id}/review",
            json={"action": action, "asset_id": "memory:existing"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected


def test_merge_replace_retire_require_a_target_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")

    for action in ["merge", "replace", "retire"]:
        suggestion = asyncio.run(
            _seed_suggestion(db_path, target_id=f"page:alpha:no-target-{action}")
        )
        response = client.post(
            f"/api/suggestions/{suggestion.id}/review", json={"action": action}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_review_action"


def test_accept_honours_scope_and_visibility_overrides(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Scoped durable memory.",
        )
    )

    response = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={"action": "accept", "scope": "person:self", "visibility": "shared"},
    )
    assert response.status_code == 200

    async def load_asset():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await MemoryAssetRegistry(conn).get_asset(
                f"memory:suggestion:{suggestion.id}"
            )

    asset = asyncio.run(load_asset())
    assert asset is not None
    assert asset.scope == "person:self"
    assert asset.visibility == "shared"


def test_accepting_a_memory_suggestion_registers_active_memory_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Remember the human-centered direction.",
        )
    )

    response = client.post(
        f"/api/suggestions/{suggestion.id}/review", json={"action": "accept"}
    )

    assert response.status_code == 200

    async def load_asset():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await MemoryAssetRegistry(conn).get_asset(
                f"memory:suggestion:{suggestion.id}"
            )

    asset = asyncio.run(load_asset())
    assert asset is not None
    assert asset.status == "active"
    assert asset.body == "Remember the human-centered direction."
    assert asset.approved_by == "owner"


def test_an_accepted_memory_summarises_itself_not_the_review_process(tmp_path, monkeypatch):
    """The rationale says why it was queued, which is not what the memory says.

    Distillation stamps every atom with the same sentence — "Above-threshold
    extraction; promotion still requires review." — and that won over the
    content. On a real vault six memories all described the review process and
    none of them described themselves. The surface renders `summary || name`, so
    the boilerplate was all you could see.
    """
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Deploys go through compose, never update.sh.",
            rationale="Above-threshold extraction; promotion still requires review.",
        )
    )

    client.post(f"/api/suggestions/{suggestion.id}/review", json={"action": "accept"})

    async def load_asset():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await MemoryAssetRegistry(conn).get_asset(
                f"memory:suggestion:{suggestion.id}"
            )

    asset = asyncio.run(load_asset())
    assert asset is not None
    assert "compose" in asset.summary, "the summary should say what is remembered"
    assert "threshold" not in asset.summary
    # The rationale is still worth keeping, just not as the thing you read first.
    assert asset.metadata.get("rationale") == (
        "Above-threshold extraction; promotion still requires review."
    )


def test_editing_a_memory_suggestion_registers_edited_memory_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Original proposed memory.",
        )
    )

    missing_edit = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={"action": "edit"},
    )
    assert missing_edit.status_code == 400

    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Original proposed memory.",
        )
    )
    edited = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={
            "action": "edit",
            "edited_markdown": "Reviewer-approved memory.",
        },
    )

    assert edited.status_code == 200
    assert edited.json()["status"] == "edited"

    async def load_asset():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await MemoryAssetRegistry(conn).get_asset(
                f"memory:suggestion:{suggestion.id}"
            )

    asset = asyncio.run(load_asset())
    assert asset is not None
    assert asset.body == "Reviewer-approved memory."
    assert asset.status == "active"


def test_replace_archives_conflicting_memory_and_records_supersession(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    asyncio.run(_seed_memory_asset(db_path, "memory:old"))
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="The human is the root.",
        )
    )
    async def add_conflict():
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                "UPDATE memory_suggestions SET conflicts=? WHERE id=?",
                ('["memory:old"]', suggestion.id),
            )
            await conn.commit()

    asyncio.run(add_conflict())

    response = client.post(
        f"/api/suggestions/{suggestion.id}/review", json={"action": "replace"}
    )

    assert response.status_code == 200

    async def load_assets():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            registry = MemoryAssetRegistry(conn)
            return (
                await registry.get_asset("memory:old"),
                await registry.get_asset(f"memory:suggestion:{suggestion.id}"),
            )

    old, new = asyncio.run(load_assets())
    assert old.status == "archived"
    assert old.superseded_by == [new.id]
    assert new.supersedes == ["memory:old"]


def test_merge_reconciles_referenced_memory_assets(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    asyncio.run(_seed_memory_asset(db_path, "memory:merge-source"))
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Merged canonical memory.",
        )
    )

    async def add_conflict():
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                "UPDATE memory_suggestions SET conflicts=? WHERE id=?",
                ('["memory:merge-source"]', suggestion.id),
            )
            await conn.commit()

    asyncio.run(add_conflict())

    response = client.post(
        f"/api/suggestions/{suggestion.id}/review", json={"action": "merge"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "merged"

    async def load_assets():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            registry = MemoryAssetRegistry(conn)
            return (
                await registry.get_asset("memory:merge-source"),
                await registry.get_asset(f"memory:suggestion:{suggestion.id}"),
            )

    old, merged = asyncio.run(load_assets())
    assert old.status == "archived"
    assert old.superseded_by == [merged.id]
    assert merged.status == "active"
    assert merged.supersedes == ["memory:merge-source"]
    assert merged.body == "Merged canonical memory."


def test_accept_only_promotes_objects_scoped_to_the_acting_wiki(tmp_path, monkeypatch):
    from archivum.knowledge.repository import (
        KnowledgeRepository,
        init_knowledge_schema,
    )

    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")

    def _proposed(object_id: str, scope: str) -> dict:
        return {
            "id": object_id,
            "kind": "memory_atom",
            "label": f"atom {object_id}",
            "scope": scope,
            "confidence": 0.8,
            "extraction_method": "EXTRACTED",
            "citations": [
                {
                    "source_id": "source:seed",
                    "chunk_id": "chunk:seed",
                    "span_start": 0,
                    "span_end": 4,
                    "quote": "seed",
                }
            ],
            "properties": {"text": object_id},
        }

    async def seed():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await init_knowledge_schema(conn)
            await init_memory_schema(conn)
            await init_suggestion_schema(conn)
            return await SuggestionRepository(conn).create_suggestion(
                target_id="wiki:alpha",
                suggestion_type="memory_atom",
                proposed_markdown="- mixed scopes",
                proposed_objects=[
                    _proposed("memory:atom:ours", "wiki:alpha"),
                    _proposed("memory:atom:owner", "person:self"),
                    _proposed("memory:atom:theirs", "wiki:other"),
                ],
                citations=[],
            )

    suggestion = asyncio.run(seed())
    response = client.post(
        f"/api/suggestions/{suggestion.id}/review", json={"action": "accept"}
    )
    assert response.status_code == 200

    async def load_objects():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            repo = KnowledgeRepository(conn)
            return (
                await repo.get_object("memory:atom:ours"),
                await repo.get_object("memory:atom:owner"),
                await repo.get_object("memory:atom:theirs"),
            )

    ours, owner, theirs = asyncio.run(load_objects())
    assert ours is not None and ours.properties["review_state"] == "accepted"
    assert owner is not None
    # Foreign-scoped proposals never cross the wiki boundary.
    assert theirs is None


def test_collaborators_cannot_promote_owner_scope_objects(tmp_path, monkeypatch):
    from archivum.knowledge.repository import (
        KnowledgeRepository,
        init_knowledge_schema,
    )

    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha", role="collaborator")

    async def seed():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await init_knowledge_schema(conn)
            await init_memory_schema(conn)
            await init_suggestion_schema(conn)
            return await SuggestionRepository(conn).create_suggestion(
                target_id="wiki:alpha",
                suggestion_type="memory_atom",
                proposed_markdown="- owner memory",
                proposed_objects=[
                    {
                        "id": "memory:atom:owner-only",
                        "kind": "memory_atom",
                        "label": "atom owner-only",
                        "scope": "person:self",
                        "confidence": 0.8,
                        "extraction_method": "EXTRACTED",
                        "citations": [
                            {
                                "source_id": "source:seed",
                                "chunk_id": "chunk:seed",
                                "span_start": 0,
                                "span_end": 4,
                                "quote": "seed",
                            }
                        ],
                        "properties": {"text": "owner memory"},
                    }
                ],
                citations=[],
            )

    suggestion = asyncio.run(seed())
    response = client.post(
        f"/api/suggestions/{suggestion.id}/review", json={"action": "accept"}
    )
    assert response.status_code == 200

    async def load_object():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await KnowledgeRepository(conn).get_object("memory:atom:owner-only")

    # The card is accepted, but the owner-scope object is never written.
    assert asyncio.run(load_object()) is None


def test_review_actions_reject_cross_wiki_asset_targets(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    asyncio.run(_seed_memory_asset(db_path, "memory:foreign", wiki_id="other"))
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Replacement text.",
        )
    )

    async def add_foreign_conflict():
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                "UPDATE memory_suggestions SET conflicts=? WHERE id=?",
                ('["memory:foreign"]', suggestion.id),
            )
            await conn.commit()

    asyncio.run(add_foreign_conflict())

    # The foreign asset is invisible to this wiki, so the card has no valid
    # target and the replace is rejected rather than acting cross-wiki.
    replace = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={"action": "replace"},
    )
    assert replace.status_code == 400

    suggestion2 = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Visibility text.",
        )
    )
    visibility = client.post(
        f"/api/suggestions/{suggestion2.id}/review",
        json={
            "action": "change_visibility",
            "asset_id": "memory:foreign",
            "visibility": "shared",
        },
    )
    assert visibility.status_code == 404

    async def load_foreign_and_replacement():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            registry = MemoryAssetRegistry(conn)
            return (
                await registry.get_asset("memory:foreign"),
                await registry.get_asset(f"memory:suggestion:{suggestion.id}"),
            )

    foreign, replacement = asyncio.run(load_foreign_and_replacement())
    assert foreign.status == "active"
    assert foreign.visibility == "private"
    assert replacement is None


def test_review_effects_roll_back_when_transition_fails(tmp_path, monkeypatch):
    from archivum.api import suggestions as suggestions_api

    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Transient memory.",
        )
    )

    async def fail_transition(self, suggestion_id, action, *, commit=True):
        raise ValueError("forced transition failure")

    monkeypatch.setattr(
        suggestions_api.SuggestionRepository,
        "transition_suggestion",
        fail_transition,
    )

    response = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={"action": "accept"},
    )
    assert response.status_code == 400

    async def load_state():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            registry = MemoryAssetRegistry(conn)
            repo = SuggestionRepository(conn)
            return (
                await registry.get_asset(f"memory:suggestion:{suggestion.id}"),
                await repo.get_suggestion(suggestion.id),
            )

    asset, reloaded = asyncio.run(load_state())
    assert asset is None
    assert reloaded.status == "pending"


def test_scope_and_visibility_actions_require_destinations_and_update_assets(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    asyncio.run(_seed_memory_asset(db_path, "memory:target"))
    client = _client_for_wiki("alpha")
    suggestion = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Update governance.",
        )
    )

    missing_scope = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={"action": "change_scope", "asset_id": "memory:target"},
    )
    assert missing_scope.status_code == 400

    scoped = client.post(
        f"/api/suggestions/{suggestion.id}/review",
        json={
            "action": "change_scope",
            "asset_id": "memory:target",
            "scope": "project:archivum",
        },
    )
    assert scoped.status_code == 200

    suggestion2 = asyncio.run(
        _seed_suggestion(
            db_path,
            target_id="wiki:alpha",
            proposed_markdown="Update visibility.",
        )
    )
    missing_visibility = client.post(
        f"/api/suggestions/{suggestion2.id}/review",
        json={"action": "change_visibility", "asset_id": "memory:target"},
    )
    assert missing_visibility.status_code == 400

    visible = client.post(
        f"/api/suggestions/{suggestion2.id}/review",
        json={
            "action": "change_visibility",
            "asset_id": "memory:target",
            "visibility": "shared",
        },
    )
    assert visible.status_code == 200

    async def load_asset():
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return await MemoryAssetRegistry(conn).get_asset("memory:target")

    asset = asyncio.run(load_asset())
    assert asset.scope == "project:archivum"
    assert asset.visibility == "shared"


def test_create_suggestion_rejects_cross_wiki_targets(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    asyncio.run(_seed_suggestion(db_path, target_id="page:alpha:seed"))
    _patch_suggestion_db(monkeypatch, db_path)

    response = _client_for_wiki("alpha").post(
        "/api/suggestions",
        json={
            "target_id": "page:other:home",
            "suggestion_type": "append_section",
            "proposed_markdown": "## Cross wiki",
            "proposed_objects": [],
            "citations": [],
        },
    )

    assert response.status_code == 403


def test_create_suggestion_accepts_review_card_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    asyncio.run(_seed_suggestion(db_path, target_id="page:alpha:seed"))
    _patch_suggestion_db(monkeypatch, db_path)

    response = _client_for_wiki("alpha").post(
        "/api/suggestions",
        json={
            "target_id": "wiki:alpha",
            "suggestion_type": "memory_atom",
            "proposed_markdown": "Remember the human-centered direction.",
            "proposed_objects": [],
            "citations": [],
            "proposed_scopes": ["person:self", "project:archivum"],
            "scores": {"future_utility": 0.9, "risk": 0.1},
            "duplicates": ["memory:old"],
            "conflicts": ["memory:conflict"],
            "retention_tier": "candidate",
            "agent_visibility": "on_review",
            "rationale": "Useful durable product direction.",
            "estimated_durability": "durable",
            "expires_at": "2026-09-12T00:00:00+00:00",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["proposed_scopes"] == ["person:self", "project:archivum"]
    assert body["scores"]["future_utility"] == 0.9
    assert body["duplicates"] == ["memory:old"]
    assert body["conflicts"] == ["memory:conflict"]
    assert body["agent_visibility"] == "on_review"
    assert body["rationale"] == "Useful durable product direction."


def test_expire_suggestions_route_expires_only_due_pending_cards(tmp_path, monkeypatch):
    db_path = tmp_path / "suggestions.db"
    _patch_suggestion_db(monkeypatch, db_path)
    stale = asyncio.run(
        _seed_suggestion(db_path, target_id="wiki:alpha", proposed_markdown="old")
    )
    fresh = asyncio.run(
        _seed_suggestion(db_path, target_id="wiki:alpha", proposed_markdown="fresh")
    )
    async def set_expiry():
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                "UPDATE memory_suggestions SET expires_at=? WHERE id=?",
                ("2026-08-01T00:00:00+00:00", stale.id),
            )
            await conn.execute(
                "UPDATE memory_suggestions SET expires_at=? WHERE id=?",
                ("2026-09-01T00:00:00+00:00", fresh.id),
            )
            await conn.commit()
    asyncio.run(set_expiry())

    response = _client_for_wiki("alpha").post(
        "/api/suggestions/expire",
        json={"now": "2026-08-13T00:00:00+00:00"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [stale.id]
