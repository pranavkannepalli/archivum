"""Trust boundaries around indexing a repository.

Registering a repository points Archivum at a directory on the machine it runs
on and writes derived files under the deployment's cache. Both of those are
host-level capabilities, so who may ask and what they may name are part of the
feature, not details around it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import archivum.db.sqlite as sqlite_mod
from archivum.auth import CurrentUser, get_current_user, require_owner, require_writer
from archivum.code_repos import register_repo, scope_for
from archivum.config import Settings, get_settings
from archivum.main import create_app


def _app(settings, user: CurrentUser) -> TestClient:
    with (
        patch("archivum.main.sqlite.init_db", new=AsyncMock()),
        patch("archivum.main.qdrant.init_collection", new=AsyncMock()),
        patch("archivum.main.graph.init_graph", new=AsyncMock()),
        patch("archivum.main.sqlite.ensure_owner_exists", new=AsyncMock()),
    ):
        app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=True)


@pytest_asyncio.fixture
async def settings(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        code_cache_dir=tmp_path / "code-cache",
    )
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    await sqlite_mod.init_db(settings)
    return settings


async def test_a_collaborator_cannot_point_the_server_at_a_directory(settings, tmp_path):
    """Indexing reads a host path, so it is an owner capability.

    The route sat behind `require_writer`, which admits collaborators, meaning
    anybody with write access could have the backend read any directory it could
    reach and then browse the result as code pages.
    """
    target = tmp_path / "secrets"
    target.mkdir()

    client = _app(settings, CurrentUser(username="mate", role="collaborator", wiki_id="default"))
    response = client.post("/api/repos", json={"path": str(target)})

    assert response.status_code == 403


async def test_the_owner_can_still_register(settings, tmp_path):
    target = tmp_path / "atlas"
    target.mkdir()

    client = _app(settings, CurrentUser(username="owner", role="owner", wiki_id="default"))
    response = client.post("/api/repos", json={"path": str(target)})

    assert response.status_code == 201


@pytest.mark.parametrize(
    "name",
    ["../escape", "nested/name", "/absolute", "..", ".", "a/../../b", "\\windows"],
)
async def test_a_name_that_could_escape_the_cache_root_is_refused(settings, tmp_path, name):
    """The name becomes a directory under the cache root and a vault folder.

    Joining a request-supplied name straight onto `code_cache_dir` let a name
    containing traversal or an absolute path write generated files anywhere the
    backend could reach.
    """
    target = tmp_path / "atlas"
    target.mkdir()

    client = _app(settings, CurrentUser(username="owner", role="owner", wiki_id="default"))
    response = client.post("/api/repos", json={"path": str(target), "name": name})

    assert response.status_code == 400, f"{name!r} should be refused"
    assert response.json()["detail"]["code"] == "invalid_repo_name"


async def test_two_vaults_can_hold_a_repository_of_the_same_name(settings, tmp_path):
    """`api` is a common repository name. Two vaults must not collide.

    The register was keyed on a name-derived scope alone, so a second vault
    registering the same name took over the first vault's row — and both vaults'
    code records then shared one scope in canonical knowledge.
    """
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    first = await register_repo(path=tmp_path / "one", wiki_id="alpha", name="api")
    second = await register_repo(path=tmp_path / "two", wiki_id="beta", name="api")

    assert first.scope != second.scope
    assert first.wiki_id == "alpha"

    from archivum.code_repos import list_repos

    alpha = await list_repos(wiki_id="alpha")
    beta = await list_repos(wiki_id="beta")
    assert [repo.path for repo in alpha] == [str(tmp_path / "one")]
    assert [repo.path for repo in beta] == [str(tmp_path / "two")]


async def test_a_repository_scope_names_the_vault_that_owns_it():
    assert scope_for("api", wiki_id="alpha") != scope_for("api", wiki_id="beta")
    assert scope_for("api", wiki_id="alpha").startswith("repo:")
