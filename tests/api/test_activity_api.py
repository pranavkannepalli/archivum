from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import archivum.db.sqlite as sqlite_mod
from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.main import create_app


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
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

    yield TestClient(app, raise_server_exceptions=True)


async def test_activity_merges_pages_and_suggestions(env):
    client = env
    await sqlite_mod.upsert_page(
        "notes/retrieval", "Retrieval design", "# Retrieval", [], "user", "default"
    )
    await sqlite_mod.upsert_page(
        "notes/agent-note", "Agent note", "# Written by an agent", [], "agent", "default"
    )

    created = client.post(
        "/api/suggestions",
        json={
            "page_slug": "notes/retrieval",
            "suggestion_type": "edit",
            "proposed_markdown": "Rerank the top 25 candidates.",
            "rationale": "The eval shows no gain past 25.",
        },
    )
    assert created.status_code == 201, created.text

    res = client.get("/api/activity")
    assert res.status_code == 200, res.text
    feed = res.json()

    kinds = {item["kind"] for item in feed["items"]}
    assert "page_created" in kinds
    assert "suggestion" in kinds
    assert feed["pending_review"] == 1

    # Newest first, and every item carries a usable timestamp.
    times = [item["at"] for item in feed["items"]]
    assert all(times)
    assert times == sorted(times, reverse=True)

    by_slug = {item["slug"]: item for item in feed["items"] if item["kind"].startswith("page")}
    assert by_slug["notes/agent-note"]["actor"] == "agent"
    assert by_slug["notes/retrieval"]["actor"] == "you"

    suggestion = next(item for item in feed["items"] if item["kind"] == "suggestion")
    assert suggestion["needs_review"] is True
    assert suggestion["actor"] == "agent"
    assert suggestion["slug"] == "notes/retrieval"


async def test_activity_respects_limit_and_cursor(env):
    client = env
    for i in range(5):
        await sqlite_mod.upsert_page(
            f"notes/page-{i}", f"Page {i}", "body", [], "user", "default"
        )

    first = client.get("/api/activity?limit=2").json()
    assert len(first["items"]) == 2
    assert first["next_before"]

    second = client.get(f"/api/activity?limit=2&before={first['next_before']}").json()
    assert len(second["items"]) <= 2
    first_ids = {item["id"] for item in first["items"]}
    assert not (first_ids & {item["id"] for item in second["items"]})


async def test_suggestions_expose_timestamps(env):
    client = env
    await sqlite_mod.upsert_page("notes/a", "A", "body", [], "user", "default")
    created = client.post(
        "/api/suggestions",
        json={
            "page_slug": "notes/a",
            "suggestion_type": "edit",
            "proposed_markdown": "Something.",
        },
    ).json()

    # Written by SQLite defaults, so they must survive the round trip rather
    # than coming back as the empty-string model default.
    assert created["created_at"]
    assert created["updated_at"]

    listed = client.get("/api/suggestions").json()
    assert listed[0]["created_at"] == created["created_at"]


async def test_activity_does_not_leak_other_wikis(env):
    """Suggestions are keyed by a target id embedding the wiki.

    `list_suggestions` only applies tenancy when target filters are supplied, so
    an unfiltered aggregate would serialise another wiki's proposed markdown and
    rationale into this feed.
    """
    client = env
    await sqlite_mod.upsert_page("notes/mine", "Mine", "body", [], "user", "default")

    async with sqlite_mod.get_db() as conn:
        from archivum.knowledge.suggestions import SuggestionRepository

        await SuggestionRepository(conn).create_suggestion(
            target_id="page:other-wiki:notes/theirs",
            suggestion_type="edit",
            proposed_markdown="A secret from another tenant.",
            proposed_objects=[],
            citations=[],
        )

    feed = client.get("/api/activity").json()
    bodies = [item["title"] for item in feed["items"]]
    assert not any("secret" in body.lower() for body in bodies)
    assert feed["pending_review"] == 0

    entries = client.get("/api/entries").json()
    assert all(not entry["needs_review"] for entry in entries["entries"])

    stats = client.get("/api/memory/stats").json()
    assert stats["suggestions_total"] == 0

    assert client.get("/api/me").json()["pending_review"] == 0


async def test_activity_cursor_keeps_records_tied_on_timestamp(env):
    """A timestamp-only cursor skips every record sharing the boundary second.

    Batch writes make ties the norm, not the exception.
    """
    client = env
    tied_at = "2026-08-19T09:00:00+00:00"
    async with sqlite_mod.get_db() as conn:
        for i in range(6):
            await conn.execute(
                "INSERT INTO pages (wiki_id, slug, title, content, tags, authored_by, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("default", f"notes/tied-{i}", f"Tied {i}", "body", "[]", "user", tied_at, tied_at),
            )
        await conn.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(6):
        suffix = f"&before={cursor}" if cursor else ""
        page = client.get(f"/api/activity?limit=2{suffix}").json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_before"]
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "pagination repeated a record"
    tied = [item_id for item_id in seen if "notes/tied-" in item_id]
    assert len(tied) == 6, f"cursor dropped tied records: got {len(tied)} of 6"


async def test_activity_keeps_memory_records_tied_on_timestamp(env):
    """A source whose SQL tie-breaks the opposite way to the merge strands rows.

    `list_assets` caps its slice in SQL; if it tie-breaks ascending while the
    feed orders descending, the assets it drops are already behind the cursor by
    the time the next page is requested.
    """
    client = env
    tied_at = "2026-08-19T09:00:00+00:00"
    async with sqlite_mod.get_db() as conn:
        for i in range(6):
            await conn.execute(
                "INSERT INTO memory_assets (id, wiki_id, asset_type, layer, name, owner, "
                "scope, status, visibility, version, summary, body, tags, metadata, "
                "citations, supersedes, superseded_by, conflict_lineage, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"memory:atom:{i}", "default", "wiki", "L1", f"Atom {i}", "person:self",
                    "person:self", "active", "private", 1, f"Atom {i}", "", "[]", "{}",
                    "[]", "[]", "[]", "[]", tied_at, tied_at,
                ),
            )
        await conn.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(6):
        suffix = f"&before={cursor}" if cursor else ""
        page = client.get(f"/api/activity?limit=2{suffix}").json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_before"]
        if not cursor:
            break

    # Activity ids for memory are "memory:{asset_id}:{version}".
    memories = [item_id for item_id in seen if "memory:atom:" in item_id]
    assert len(memories) == 6, f"cursor dropped tied memory records: {len(memories)} of 6"
    assert len(seen) == len(set(seen))


async def test_activity_handles_prefix_related_memory_ids(env):
    """Asset ids where one is a prefix of another sort differently once the
    version suffix is appended: "role:1" vs "role2:1" reverses the raw order.
    The SQL cursor has to compare the same key the feed sorts by.
    """
    client = env
    tied_at = "2026-08-19T09:00:00+00:00"
    ids = ["memory:persona:role", "memory:persona:role2", "memory:persona:role10"]
    async with sqlite_mod.get_db() as conn:
        for asset_id in ids:
            await conn.execute(
                "INSERT INTO memory_assets (id, wiki_id, asset_type, layer, name, owner, "
                "scope, status, visibility, version, summary, body, tags, metadata, "
                "citations, supersedes, superseded_by, conflict_lineage, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id, "default", "persona", "L3", asset_id, "person:self",
                    "person:self", "active", "private", 1, asset_id, "", "[]", "{}",
                    "[]", "[]", "[]", "[]", tied_at, tied_at,
                ),
            )
        await conn.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(6):
        suffix = f"&before={cursor}" if cursor else ""
        page = client.get(f"/api/activity?limit=1{suffix}").json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_before"]
        if not cursor:
            break

    for asset_id in ids:
        assert any(item_id.startswith(f"memory:{asset_id}:") for item_id in seen), (
            f"{asset_id} was dropped by the cursor"
        )
    assert len(seen) == len(set(seen))


# ── The work you actually did should show up where you look ───────────────


@pytest.mark.asyncio
async def test_a_captured_session_appears_in_the_stream(env):
    """Automatic capture you cannot see is hard to tell apart from no capture."""
    from archivum.knowledge.models import Citation, KnowledgeObject
    from archivum.knowledge.repository import KnowledgeRepository

    client = env
    async with sqlite_mod.get_db() as conn:
        await KnowledgeRepository(conn).upsert_object(
            KnowledgeObject(
                id="session:src-1",
                kind="session",
                label="Fix the crash in haversine",
                scope="wiki:default",
                confidence=1.0,
                extraction_method="EXTRACTED",
                citations=[
                    Citation(source_id="src-1", chunk_id="src-1", span_start=None, span_end=None, quote="x")
                ],
                properties={
                    "kind": "bugfix",
                    "started_at": "2026-08-20T10:00:00Z",
                    "touched_paths": ["/src/geo.py"],
                },
            )
        )
        await conn.commit()

    body = client.get("/api/activity").json()
    sessions = [item for item in body["items"] if item["kind"] == "session"]

    assert sessions, body
    assert sessions[0]["payload"]["session_kind"] == "bugfix"
    assert sessions[0]["actor"] == "agent"


@pytest.mark.asyncio
async def test_a_remembered_fix_appears_in_the_stream(env):
    from archivum.knowledge.models import Citation, KnowledgeObject
    from archivum.knowledge.repository import KnowledgeRepository

    client = env
    async with sqlite_mod.get_db() as conn:
        await KnowledgeRepository(conn).upsert_object(
            KnowledgeObject(
                id="fix:src-1",
                kind="fix",
                label="TypeError: bad operand",
                scope="wiki:default",
                confidence=0.9,
                extraction_method="EXTRACTED",
                citations=[
                    Citation(source_id="src-1", chunk_id="src-1", span_start=None, span_end=None, quote="x")
                ],
                properties={
                    "symptom": "TypeError: bad operand",
                    "diagnosis": "normalise returned a string.",
                    "verified_by": "pytest",
                    "started_at": "2026-08-20T11:00:00Z",
                },
            )
        )
        await conn.commit()

    body = client.get("/api/activity").json()
    fixes = [item for item in body["items"] if item["kind"] == "fix"]

    assert fixes, body
    assert fixes[0]["summary"] == "normalise returned a string."
    assert fixes[0]["payload"]["verified_by"] == "pytest"


@pytest.mark.asyncio
async def test_open_tasks_ride_along_with_the_feed(env):
    """The stream is where you would look for what still needs doing."""
    client = env
    await sqlite_mod.upsert_page(
        "daily/today", "Today", "- [ ] Ship it\n- [x] Done\n", [], "user", "default"
    )

    body = client.get("/api/activity").json()

    assert [task["text"] for task in body["open_tasks"]] == ["Ship it"]
    assert body["open_tasks"][0]["slug"] == "daily/today"


@pytest.mark.asyncio
async def test_ticking_a_task_edits_the_page_it_lives_in(env, tmp_path):
    """The file is the source of truth, so ticking a box changes the file."""
    client = env
    settings = client.app.dependency_overrides[get_settings]()
    path = settings.wiki_dir / "daily" / "today.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- [ ] Ship it\n", encoding="utf-8")
    await sqlite_mod.upsert_page(
        "daily/today", "Today", path.read_text(encoding="utf-8"), [], "user", "default"
    )

    with (
        patch("archivum.indexing.qdrant.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
        patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
        patch("archivum.indexing.graph.add_reference", new=AsyncMock()),
    ):
        response = client.post(
            "/api/tasks/toggle", json={"slug": "daily/today", "line": 1, "done": True}
        )

    assert response.status_code == 200
    assert "- [x] Ship it" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_ticking_something_that_is_not_a_task_is_refused(env):
    client = env
    settings = client.app.dependency_overrides[get_settings]()
    (settings.wiki_dir / "a.md").write_text("Just prose.\n", encoding="utf-8")
    await sqlite_mod.upsert_page("a", "A", "Just prose.\n", [], "user", "default")

    response = client.post("/api/tasks/toggle", json={"slug": "a", "line": 1, "done": True})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "not_a_task"
