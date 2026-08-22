"""Regression test: GET /{slug}/backlinks must not be shadowed by GET /{slug:path}."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from archivum.auth import create_access_token
from archivum.config import get_settings
from archivum.main import create_app
from archivum.api import pages
from archivum.indexing import ReindexResult, project_page_graph


class BacklinksRouteTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.token = create_access_token("owner", "owner", "default", self.settings)
        self.app = create_app()
        self.client = TestClient(self.app)
        self.client.cookies.set("access_token", self.token)

    def test_backlinks_route_not_shadowed_by_slug_catch_all(self):
        """GET /api/pages/my-page/backlinks must return backlinks, not 404."""
        fake_page = {
            "id": 1,
            "slug": "my-page",
            "title": "My Page",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }
        fake_backlinks = [{"slug": "other-page", "title": "Other Page"}]

        with (
            patch(
                "archivum.api.pages.sqlite.get_page",
                new=AsyncMock(return_value=fake_page),
            ),
            patch(
                "archivum.api.pages.graph.get_backlinks",
                new=AsyncMock(return_value=fake_backlinks),
            ),
        ):
            response = self.client.get("/api/pages/my-page/backlinks")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["slug"], "other-page")

    def test_backlinks_returns_404_when_page_missing(self):
        """Backlinks for a non-existent page should return 404 with page_not_found."""
        with patch(
            "archivum.api.pages.sqlite.get_page",
            new=AsyncMock(return_value=None),
        ):
            response = self.client.get("/api/pages/ghost-page/backlinks")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "page_not_found")

    def test_backlinks_route_parses_slug_without_backlinks_suffix(self):
        """GET /api/pages/compute-blade/backlinks must parse slug as 'compute-blade'."""
        fake_page = {
            "id": 2,
            "slug": "compute-blade",
            "title": "Compute Blade",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        with (
            patch(
                "archivum.api.pages.sqlite.get_page",
                new=AsyncMock(return_value=fake_page),
            ) as mock_get_page,
            patch(
                "archivum.api.pages.graph.get_backlinks",
                new=AsyncMock(return_value=[]),
            ),
        ):
            response = self.client.get("/api/pages/compute-blade/backlinks")

        self.assertEqual(response.status_code, 200)
        # Verify the slug passed to sqlite was "compute-blade", not "compute-blade/backlinks"
        mock_get_page.assert_called_once_with("compute-blade", "default")

    def test_deeply_nested_slug_backlinks(self):
        """GET /api/pages/hardware/compute-blade/backlinks must parse slug as 'hardware/compute-blade'."""
        fake_page = {
            "id": 3,
            "slug": "hardware/compute-blade",
            "title": "Compute Blade",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        with (
            patch(
                "archivum.api.pages.sqlite.get_page",
                new=AsyncMock(return_value=fake_page),
            ) as mock_get_page,
            patch(
                "archivum.api.pages.graph.get_backlinks",
                new=AsyncMock(return_value=[]),
            ),
        ):
            response = self.client.get("/api/pages/hardware/compute-blade/backlinks")

        self.assertEqual(response.status_code, 200)
        mock_get_page.assert_called_once_with("hardware/compute-blade", "default")


class BacklinkIndexingTests(unittest.IsolatedAsyncioTestCase):
    """Covers the projection the write path actually runs.

    These used to exercise a copy of this logic that lived in the pages router
    and no longer had any callers, so they stayed green while saying nothing
    about what a real page write does.
    """

    async def test_page_graph_projection_adds_references_for_existing_wikilinks(self):
        target = {
            "id": 4,
            "slug": "target-page",
            "title": "Target Page",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        with (
            patch("archivum.indexing.graph.upsert_page", new=AsyncMock()) as upsert_page,
            patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
            patch("archivum.indexing.graph.add_reference", new=AsyncMock()) as add_reference,
            patch("archivum.indexing.sqlite.get_page", new=AsyncMock(return_value=target)),
        ):
            await project_page_graph(
                "source-page",
                "Source Page",
                "See [[target-page]] and [[target-page|Target]].",
                "default",
                ReindexResult(slug="source-page"),
            )

        upsert_page.assert_awaited_once_with("source-page", "Source Page", "default")
        add_reference.assert_awaited_once_with("source-page", "target-page", "default")

    async def test_page_graph_projection_slugifies_display_text_wikilinks(self):
        target = {
            "id": 4,
            "slug": "target-page",
            "title": "Target Page",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        async def get_page(slug: str, wiki_id: str):
            if slug == "target-page" and wiki_id == "default":
                return target
            return None

        with (
            patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
            patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
            patch("archivum.indexing.graph.add_reference", new=AsyncMock()) as add_reference,
            patch("archivum.indexing.sqlite.get_page", new=AsyncMock(side_effect=get_page)),
        ):
            await project_page_graph(
                "source-page",
                "Source Page",
                "See [[Target Page|Target]].",
                "default",
                ReindexResult(slug="source-page"),
            )

        add_reference.assert_awaited_once_with("source-page", "target-page", "default")

    async def test_page_graph_projection_preserves_folder_wikilink_targets(self):
        target = {
            "id": 4,
            "slug": "smoke/target-page",
            "title": "Target Page",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        async def get_page(slug: str, wiki_id: str):
            if slug == "smoke/target-page" and wiki_id == "default":
                return target
            return None

        with (
            patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
            patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()),
            patch("archivum.indexing.graph.add_reference", new=AsyncMock()) as add_reference,
            patch("archivum.indexing.sqlite.get_page", new=AsyncMock(side_effect=get_page)),
        ):
            await project_page_graph(
                "smoke/source-page",
                "Source Page",
                "See [[smoke/Target Page|Target]].",
                "default",
                ReindexResult(slug="smoke/source-page"),
            )

        add_reference.assert_awaited_once_with(
            "smoke/source-page",
            "smoke/target-page",
            "default",
        )

    async def test_page_graph_projection_clears_stale_outgoing_references_before_reindexing(self):
        target = {
            "id": 4,
            "slug": "current-target",
            "title": "Current Target",
            "content": "",
            "tags": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "authored_by": "owner",
        }

        with (
            patch("archivum.indexing.graph.upsert_page", new=AsyncMock()),
            patch("archivum.indexing.graph.clear_references_from_page", new=AsyncMock()) as clear_references,
            patch("archivum.indexing.graph.add_reference", new=AsyncMock()) as add_reference,
            patch("archivum.indexing.sqlite.get_page", new=AsyncMock(return_value=target)),
        ):
            await project_page_graph(
                "source-page",
                "Source Page",
                "Now only [[current-target]] remains.",
                "default",
                ReindexResult(slug="source-page"),
            )

        clear_references.assert_awaited_once_with("source-page", "default")
        add_reference.assert_awaited_once_with("source-page", "current-target", "default")


if __name__ == "__main__":
    unittest.main()
