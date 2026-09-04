"""Tests for system endpoints: /api/audio-support, /api/rebuild-indexes, /api/lint, /api/lint/fix."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from archivum.auth import create_access_token
from archivum.config import get_settings
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.main import create_app


class FakeDatabase:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        rows = [
            {"id": obj.id}
            for obj in FakeKnowledgeRepository.objects
            if obj.kind == "page" and obj.scope == params[0] and obj.id.startswith("page:default:")
        ]
        return FakeCursor(rows)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def fetchall(self):
        return self._rows


def _projection_report():
    return SimpleNamespace(
        objects=3,
        relationships=2,
        qdrant_indexed=2,
        kuzu_nodes=3,
        kuzu_edges=2,
    )


class FakeKnowledgeRepository:
    objects: list[KnowledgeObject] = []
    deleted: list[str] = []

    def __init__(self, conn):
        pass

    async def list_objects(self, kind=None, scope=None, limit=100):
        return [
            obj
            for obj in self.objects
            if (kind is None or obj.kind == kind)
            and (scope is None or obj.scope == scope)
        ][:limit]

    async def delete_object(self, object_id: str):
        self.deleted.append(object_id)


def _make_client(app, token: str, *, bearer: bool = False) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    if bearer:
        # Bearer auth bypasses CSRF middleware (needed for mutating endpoints)
        client.headers.update({"Authorization": f"Bearer {token}"})
    else:
        client.cookies.set("access_token", token)
    return client


class TestAudioSupport(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        with (
            patch("archivum.main.sqlite.init_db", new=AsyncMock()),
            patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.main.graph.init_graph", new=AsyncMock()),
            patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        ):
            self.app = create_app()
        self.client = _make_client(self.app, self.token)

    def test_returns_audio_support_status(self):
        """GET /api/audio-support returns availability dict."""
        response = self.client.get("/api/audio-support")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("available", data)
        self.assertIn("audio_available", data)
        self.assertIn("video_available", data)
        self.assertIn("dependencies", data)
        self.assertIsInstance(data["available"], bool)
        self.assertNotIn("commands", data)

    def test_installs_audio_support_from_settings_action(self):
        client = _make_client(self.app, self.token, bearer=True)
        install_result = {
            "ok": True,
            "actions": [
                {"name": "openai-whisper", "status": "installed", "detail": "Installed"},
                {"name": "ffmpeg", "status": "already_available", "detail": "Already installed"},
            ],
            "status": {
                "available": True,
                "audio_available": True,
                "video_available": True,
                "dependencies": {"openai_whisper": True, "ffmpeg": True},
                "missing": [],
                "notes": [],
            },
        }

        with patch(
            "archivum.api.system.install_audio_support",
            new=AsyncMock(return_value=install_result),
        ) as installer:
            response = client.post("/api/audio-support/install")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), install_result)
        installer.assert_awaited_once()

    def test_audio_only_status_when_ffmpeg_is_missing(self):
        with (
            patch("archivum.api.system.find_spec", return_value=object()),
            patch("archivum.api.system.shutil.which", return_value=None),
        ):
            response = self.client.get("/api/audio-support")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["available"])
        self.assertTrue(data["audio_available"])
        self.assertFalse(data["video_available"])
        self.assertEqual(data["missing"], ["ffmpeg"])


class TestLlmSettings(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        with (
            patch("archivum.main.sqlite.init_db", new=AsyncMock()),
            patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.main.graph.init_graph", new=AsyncMock()),
            patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        ):
            self.app = create_app()
        self.client = _make_client(self.app, self.token, bearer=True)

    def test_get_llm_settings_masks_ollama_api_key(self):
        settings = self.settings.model_copy(
            update={
                "llm_extraction_provider": "ollama",
                "llm_synthesis_provider": "ollama",
                "ollama_base_url": "https://ollama.example.com/v1",
                "ollama_api_key": "ollama-secret",
            }
        )

        with patch("archivum.api.system.get_settings", return_value=settings):
            response = self.client.get("/api/settings/llm")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ollama_base_url"], "https://ollama.example.com/v1")
        self.assertTrue(data["ollama_api_key_configured"])
        self.assertEqual(data["ollama_api_key_masked"], "olla...cret")
        self.assertNotIn("ollama-secret", str(data))

    def test_put_llm_settings_applies_runtime_environment_without_returning_secret(self):
        body = {
            "llm_extraction_provider": "ollama",
            "llm_synthesis_provider": "ollama",
            "llm_model": "model-a",
            "llm_synthesis_model": "model-b",
            "ollama_base_url": "https://ollama.example.com/v1",
            "ollama_api_key": "new-secret",
        }

        with (
            patch("archivum.api.system._write_env_updates") as write_env_updates,
            patch("archivum.api.system.get_settings.cache_clear") as cache_clear,
        ):
            response = self.client.put("/api/settings/llm", json=body)

        self.assertEqual(response.status_code, 200)
        write_env_updates.assert_called_once()
        cache_clear.assert_called_once()
        data = response.json()
        self.assertTrue(data["ollama_api_key_configured"])
        self.assertNotIn("new-secret", str(data))

    def test_get_mcp_settings_returns_client_config_without_secret(self):
        settings = self.settings.model_copy(
            update={
                "mcp_port": 8001,
                "mcp_api_key": "mcp-secret",
                "mcp_public_url": "https://archivum-mcp.example.com/sse",
            }
        )

        self.app.dependency_overrides[get_settings] = lambda: settings
        try:
            response = self.client.get("/api/settings/mcp")
        finally:
            self.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["endpoint"], "https://archivum-mcp.example.com/sse")
        self.assertEqual(
            data["client_config"]["mcpServers"]["archivum"]["url"],
            "https://archivum-mcp.example.com/sse",
        )
        self.assertTrue(data["auth_required"])
        self.assertTrue(data["api_key_configured"])
        self.assertIn("<MCP_API_KEY>", str(data))
        self.assertNotIn("mcp-secret", str(data))

    def test_mcp_settings_config_carries_a_bearer_header_without_a_legacy_key(self):
        """The default install has no MCP_API_KEY, and HTTP still authenticates.

        A copied config with no Authorization header is guaranteed to 401, so
        the button must hand back a placeholder the user can fill from
        `archivum connect` rather than a config that cannot work.
        """
        settings = self.settings.model_copy(
            update={"mcp_port": 8001, "mcp_api_key": "", "mcp_public_url": ""}
        )

        self.app.dependency_overrides[get_settings] = lambda: settings
        try:
            response = self.client.get("/api/settings/mcp")
        finally:
            self.app.dependency_overrides.clear()

        data = response.json()
        server = data["client_config"]["mcpServers"]["archivum"]
        self.assertFalse(data["api_key_configured"])
        self.assertTrue(data["auth_required"])
        self.assertIn("Authorization", server["headers"])
        self.assertIn("archivum connect", server["headers"]["Authorization"])
        self.assertTrue(server["headers"]["Authorization"].startswith("Bearer "))


class TestRebuildIndexes(unittest.TestCase):
    def setUp(self):
        FakeKnowledgeRepository.objects = []
        FakeKnowledgeRepository.deleted = []
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        with (
            patch("archivum.main.sqlite.init_db", new=AsyncMock()),
            patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.main.graph.init_graph", new=AsyncMock()),
            patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        ):
            self.app = create_app()
        # Use bearer token to bypass CSRF for POST
        self.client = _make_client(self.app, self.token, bearer=True)

    def _fake_pages(self):
        return [
            {"slug": "page-one", "title": "Page One", "content": "Hello [[page-two]]"},
            {"slug": "page-two", "title": "Page Two", "content": ""},
        ]

    def test_smoke_rebuild_indexes(self):
        """POST /api/rebuild-indexes completes without error."""
        fake_pages = self._fake_pages()
        with (
            patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=fake_pages)),
            patch("archivum.api.system.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.api.system.graph.init_graph", new=AsyncMock()),
            patch("archivum.api.system.qdrant.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.add_reference", new=AsyncMock()),
            patch("archivum.api.system.sqlite.get_db", FakeDatabase),
            patch("archivum.api.system.KnowledgeRepository", FakeKnowledgeRepository),
            patch("archivum.api.system.init_knowledge_schema", new=AsyncMock()),
            patch("archivum.api.system.sync_page_to_knowledge", new=AsyncMock()) as sync_page,
            patch(
                "archivum.api.system.rebuild_knowledge_projections",
                new=AsyncMock(return_value=_projection_report()),
            ),
            patch(
                "archivum.api.system.sqlite.get_page",
                new=AsyncMock(return_value={"slug": "page-two", "title": "Page Two"}),
            ),
        ):
            response = self.client.post("/api/rebuild-indexes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sync_page.await_count, 2)

    def test_rebuild_returns_page_count(self):
        """POST /api/rebuild-indexes response includes page count."""
        fake_pages = self._fake_pages()
        with (
            patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=fake_pages)),
            patch("archivum.api.system.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.api.system.graph.init_graph", new=AsyncMock()),
            patch("archivum.api.system.qdrant.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.add_reference", new=AsyncMock()),
            patch("archivum.api.system.sqlite.get_db", FakeDatabase),
            patch("archivum.api.system.KnowledgeRepository", FakeKnowledgeRepository),
            patch("archivum.api.system.init_knowledge_schema", new=AsyncMock()),
            patch("archivum.api.system.sync_page_to_knowledge", new=AsyncMock()),
            patch(
                "archivum.api.system.rebuild_knowledge_projections",
                new=AsyncMock(return_value=_projection_report()),
            ),
            patch("archivum.api.system.sqlite.get_page", new=AsyncMock(return_value=None)),
        ):
            response = self.client.post("/api/rebuild-indexes")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["pages"], 2)
        self.assertEqual(data["canonical_objects"], 3)
        self.assertEqual(data["canonical_relationships"], 2)
        self.assertEqual(data["qdrant_indexed"], 2)
        self.assertIn("detail", data)

    def test_rebuild_slugifies_display_text_wikilinks(self):
        """POST /api/rebuild-indexes indexes [[Target Page|Target]] as target-page."""
        fake_pages = [
            {
                "slug": "source-page",
                "title": "Source Page",
                "content": "See [[Target Page|Target]].",
            },
            {"slug": "target-page", "title": "Target Page", "content": ""},
        ]

        async def get_page(slug: str, wiki_id: str):
            if slug == "target-page" and wiki_id == "default":
                return {"slug": "target-page", "title": "Target Page"}
            return None

        with (
            patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=fake_pages)),
            patch("archivum.api.system.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.api.system.graph.init_graph", new=AsyncMock()),
            patch("archivum.api.system.qdrant.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.add_reference", new=AsyncMock()) as add_reference,
            patch("archivum.api.system.sqlite.get_db", FakeDatabase),
            patch("archivum.api.system.KnowledgeRepository", FakeKnowledgeRepository),
            patch("archivum.api.system.init_knowledge_schema", new=AsyncMock()),
            patch("archivum.api.system.sync_page_to_knowledge", new=AsyncMock()),
            patch(
                "archivum.api.system.rebuild_knowledge_projections",
                new=AsyncMock(return_value=_projection_report()),
            ),
            patch("archivum.api.system.sqlite.get_page", new=AsyncMock(side_effect=get_page)),
        ):
            response = self.client.post("/api/rebuild-indexes")

        self.assertEqual(response.status_code, 200)
        add_reference.assert_awaited_once_with("source-page", "target-page", "default")

    def test_rebuild_removes_stale_canonical_pages_before_projection(self):
        """POST /api/rebuild-indexes deletes canonical pages absent from SQLite."""
        fake_pages = [{"slug": "current-page", "title": "Current Page", "content": ""}]
        stale = KnowledgeObject(
            id="page:default:stale-page",
            kind="page",
            label="Stale Page",
            scope="wiki:default",
            confidence=1.0,
            extraction_method="USER_AUTHORED",
            citations=[
                Citation(
                    source_id="page:default:stale-page",
                    chunk_id="page:default:stale-page",
                    span_start=None,
                    span_end=None,
                    quote="Stale Page",
                )
            ],
            properties={"slug": "stale-page", "wiki_id": "default"},
        )
        current = stale.model_copy(
            update={
                "id": "page:default:current-page",
                "label": "Current Page",
                "properties": {"slug": "current-page", "wiki_id": "default"},
            }
        )
        stale_pages = [
            stale.model_copy(
                update={
                    "id": f"page:default:stale-page-{index}",
                    "label": f"Stale Page {index}",
                    "properties": {"slug": f"stale-page-{index}", "wiki_id": "default"},
                }
            )
            for index in range(1005)
        ]
        FakeKnowledgeRepository.objects = [*stale_pages, current]
        FakeKnowledgeRepository.deleted = []

        with (
            patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=fake_pages)),
            patch("archivum.api.system.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.api.system.graph.init_graph", new=AsyncMock()),
            patch("archivum.api.system.qdrant.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.upsert_page", new=AsyncMock()),
            patch("archivum.api.system.graph.add_reference", new=AsyncMock()),
            patch("archivum.api.system.sqlite.get_db", FakeDatabase),
            patch("archivum.api.system.KnowledgeRepository", FakeKnowledgeRepository),
            patch("archivum.api.system.init_knowledge_schema", new=AsyncMock()),
            patch("archivum.api.system.sync_page_to_knowledge", new=AsyncMock()),
            patch(
                "archivum.api.system.rebuild_knowledge_projections",
                new=AsyncMock(return_value=_projection_report()),
            ),
            patch("archivum.api.system.sqlite.get_page", new=AsyncMock(return_value=None)),
        ):
            response = self.client.post("/api/rebuild-indexes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(FakeKnowledgeRepository.deleted), 1005)
        self.assertIn("page:default:stale-page-1004", FakeKnowledgeRepository.deleted)
        FakeKnowledgeRepository.objects = []


class TestLintWiki(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        with (
            patch("archivum.main.sqlite.init_db", new=AsyncMock()),
            patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.main.graph.init_graph", new=AsyncMock()),
            patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        ):
            self.app = create_app()
        self.client = _make_client(self.app, self.token)

    def test_lint_returns_empty_when_no_issues(self):
        """GET /api/lint returns no issues for clean pages."""
        clean_pages = [
            {"slug": "a", "content": "[[b]]"},
            {"slug": "b", "content": "[[a]]"},
        ]
        with patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=clean_pages)):
            response = self.client.get("/api/lint")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["counts"]["issues"], 0)
        self.assertEqual(len(data["issues"]), 0)

    def test_lint_detects_broken_wikilinks(self):
        """GET /api/lint reports broken_wikilink when target page does not exist."""
        pages = [
            {"slug": "has-broken", "content": "See [[missing-page]] for details."},
        ]
        with patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=pages)):
            response = self.client.get("/api/lint")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        broken = [i for i in data["issues"] if i["type"] == "broken_wikilink"]
        self.assertGreater(len(broken), 0)
        self.assertEqual(broken[0]["target"], "missing-page")
        self.assertEqual(broken[0]["page"], "has-broken")

    def test_lint_detects_orphan_pages(self):
        """GET /api/lint reports orphan_page for pages with no links in or out."""
        pages = [
            {"slug": "island", "content": "No links here."},
        ]
        with patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=pages)):
            response = self.client.get("/api/lint")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        orphans = [i for i in data["issues"] if i["type"] == "orphan_page"]
        self.assertGreater(len(orphans), 0)
        self.assertEqual(orphans[0]["page"], "island")

    def test_lint_detects_contradictory_boolean_claims(self):
        """GET /api/lint reports contradictory_claim for enabled/disabled statements."""
        pages = [
            {"slug": "ops-a", "content": "Public wiki is enabled."},
            {"slug": "ops-b", "content": "Public wiki is disabled."},
        ]
        with patch("archivum.api.system.sqlite.list_pages", new=AsyncMock(return_value=pages)):
            response = self.client.get("/api/lint")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        contradictions = [i for i in data["issues"] if i["type"] == "contradictory_claim"]
        self.assertEqual(len(contradictions), 1)
        self.assertEqual(contradictions[0]["subject"], "public wiki")
        self.assertEqual(contradictions[0]["pages"], ["ops-a", "ops-b"])


class TestLintFix(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        with (
            patch("archivum.main.sqlite.init_db", new=AsyncMock()),
            patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
            patch("archivum.main.graph.init_graph", new=AsyncMock()),
            patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
        ):
            self.app = create_app()
        # Bearer token for POST (bypasses CSRF)
        self.client = _make_client(self.app, self.token, bearer=True)

    def test_fix_broken_wikilink(self):
        """POST /api/lint/fix with type=broken_wikilink patches the page content."""
        fake_page = {
            "slug": "source-page",
            "title": "Source",
            "content": "See [[dead-link]] for info.",
            "tags": "[]",
            "authored_by": "owner",
        }
        with (
            patch("archivum.api.system.sqlite.get_page", new=AsyncMock(return_value=fake_page)),
            patch("archivum.api.system.sqlite.upsert_page", new=AsyncMock()) as mock_upsert,
        ):
            response = self.client.post(
                "/api/lint/fix",
                json={"type": "broken_wikilink", "source_slug": "source-page", "link_target": "dead-link"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["detail"], "fixed")
        mock_upsert.assert_called_once()
        # Content passed to upsert_page should no longer contain the wikilink
        call_kwargs = mock_upsert.call_args.kwargs
        self.assertNotIn("[[dead-link]]", call_kwargs.get("content", ""))

    def test_fix_orphan_returns_no_auto_fix(self):
        """POST /api/lint/fix with type=orphan returns no_auto_fix detail."""
        response = self.client.post(
            "/api/lint/fix",
            json={"type": "orphan", "slug": "lonely-page"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["detail"], "no_auto_fix")

    def test_fix_broken_wikilink_returns_404_when_page_not_found(self):
        """POST /api/lint/fix returns 404 if source_slug page does not exist."""
        with patch("archivum.api.system.sqlite.get_page", new=AsyncMock(return_value=None)):
            response = self.client.post(
                "/api/lint/fix",
                json={"type": "broken_wikilink", "source_slug": "ghost-page", "link_target": "dead-link"},
            )

        self.assertEqual(response.status_code, 404)

    def test_fix_unknown_type_returns_400(self):
        """POST /api/lint/fix with an unrecognised type returns 400."""
        response = self.client.post(
            "/api/lint/fix",
            json={"type": "unknown_type", "source_slug": "some-page"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
