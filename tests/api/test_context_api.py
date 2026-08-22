from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from archivum.api import context as context_api
from archivum.auth import CurrentUser
from archivum.config import Settings
from archivum.knowledge.models import Citation, ContextPackage, ContextNode
from archivum.main import app
from archivum.retrieval.hybrid import HybridHit


def test_context_package_route_requires_auth_or_test_auth(monkeypatch):
    client = TestClient(app)
    response = client.post("/api/context-package", json={"query": "Alpha", "max_nodes": 5})
    assert response.status_code in {200, 401}


@pytest.mark.asyncio
async def test_context_package_defaults_to_the_authenticated_wiki(monkeypatch):
    package = ContextPackage(
        query="Alpha",
        seeds=["entity:alpha"],
        nodes=[
            ContextNode(
                id="entity:alpha",
                label="Alpha",
                node_type="entity",
                scope="wiki:owner",
                extraction_method="EXTRACTED",
                confidence=0.8,
                citations=[
                    Citation(
                        source_id="page:owner:alpha",
                        chunk_id="page:owner:alpha:chunk:0",
                        span_start=0,
                        span_end=5,
                        quote="Alpha evidence",
                    )
                ],
            )
        ],
        edges=[],
        citations=[],
        insufficient_evidence=False,
        reason=None,
    )

    @asynccontextmanager
    async def fake_db():
        yield object()

    build = AsyncMock(return_value=package)
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)

    result = await context_api.context_package(
        context_api.ContextPackageRequest(query="Alpha"),
        CurrentUser(username="owner", role="owner", wiki_id="owner"),
    )

    assert result.nodes[0].id == "entity:alpha"
    assert build.await_args.args[1].scope == "wiki:owner"


@pytest.mark.asyncio
async def test_context_package_allows_explicit_authenticated_wiki_scope(monkeypatch):
    package = ContextPackage(
        query="Alpha",
        seeds=[],
        nodes=[],
        edges=[],
        citations=[],
        insufficient_evidence=True,
        reason="No cited knowledge objects matched the requested context.",
    )

    @asynccontextmanager
    async def fake_db():
        yield object()

    build = AsyncMock(return_value=package)
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)

    await context_api.context_package(
        context_api.ContextPackageRequest(query="Alpha", scope="wiki:owner"),
        CurrentUser(username="owner", role="owner", wiki_id="owner"),
    )

    assert build.await_args.args[1].scope == "wiki:owner"


@pytest.mark.asyncio
async def test_context_package_allows_person_self_scope(monkeypatch):
    package = ContextPackage(
        query="Alpha",
        seeds=["person:self"],
        nodes=[],
        edges=[],
        citations=[],
        insufficient_evidence=True,
        reason="No cited knowledge objects matched the requested context.",
    )

    @asynccontextmanager
    async def fake_db():
        yield object()

    build = AsyncMock(return_value=package)
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)

    await context_api.context_package(
        context_api.ContextPackageRequest(query="Alpha", scope="person:self"),
        CurrentUser(username="owner", role="owner", wiki_id="owner"),
    )

    assert build.await_args.args[1].scope == "person:self"


@pytest.mark.asyncio
async def test_context_package_denies_person_self_scope_to_collaborators(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield object()

    build = AsyncMock()
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)

    with pytest.raises(HTTPException) as error:
        await context_api.context_package(
            context_api.ContextPackageRequest(query="Alpha", scope="person:self"),
            CurrentUser(username="helper", role="collaborator", wiki_id="owner"),
        )

    assert error.value.status_code == 403
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_package_rejects_other_wiki_scope(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield object()

    package = ContextPackage(
        query="Alpha",
        seeds=[],
        nodes=[],
        edges=[],
        citations=[],
        insufficient_evidence=True,
        reason="No cited knowledge objects matched the requested context.",
    )
    build = AsyncMock(return_value=package)
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)

    with pytest.raises(HTTPException) as error:
        await context_api.context_package(
            context_api.ContextPackageRequest(query="Alpha", scope="wiki:other"),
            CurrentUser(username="owner", role="owner", wiki_id="owner"),
        )

    assert error.value.status_code == 403
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_package_rejects_repo_scope_without_authorization(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield object()

    package = ContextPackage(
        query="Alpha",
        seeds=[],
        nodes=[],
        edges=[],
        citations=[],
        insufficient_evidence=True,
        reason="No cited knowledge objects matched the requested context.",
    )
    build = AsyncMock(return_value=package)
    monkeypatch.setattr(context_api.sqlite, "get_db", fake_db)
    monkeypatch.setattr(context_api, "build_context_package", build)
    # A repository is authorised by being in the register, so "unauthorised"
    # means the register is empty rather than the scope string looking wrong.
    monkeypatch.setattr(
        context_api, "owned_repo_scopes", AsyncMock(return_value=set())
    )

    with pytest.raises(HTTPException) as error:
        await context_api.context_package(
            context_api.ContextPackageRequest(query="Alpha", scope="repo:test"),
            CurrentUser(username="owner", role="owner", wiki_id="owner"),
        )

    assert error.value.status_code == 403
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrieve_returns_compact_cited_hybrid_hits(monkeypatch):
    hit = HybridHit(
        id="entity:alpha",
        label="Alpha",
        score=0.8,
        source="keyword+graph",
        citation=Citation(
            source_id="page:default:alpha",
            chunk_id="page:default:alpha:chunk:0",
            span_start=0,
            span_end=5,
            quote="Alpha evidence",
        ),
        extraction_method="USER_AUTHORED",
        confidence=0.65,
        provenance="canonical",
        citations=(
            Citation(
                source_id="page:default:alpha",
                chunk_id="canonical:alpha",
                span_start=0,
                span_end=5,
                quote="Canonical Alpha evidence",
            ),
        ),
    )
    monkeypatch.setattr(context_api, "hybrid_retrieve", AsyncMock(return_value=[hit]))

    result = await context_api.retrieve(
        context_api.RetrieveRequest(query="Alpha"),
        CurrentUser(username="owner", role="owner", wiki_id="default"),
        Settings(),
    )

    assert result.hits[0].id == "entity:alpha"
    assert result.hits[0].label == "Alpha"
    assert result.hits[0].citation == hit.citation
    assert result.hits[0].extraction_method == "USER_AUTHORED"
    assert result.hits[0].confidence == 0.65
    assert result.hits[0].provenance == "canonical"
    assert result.hits[0].citations[0].chunk_id == "canonical:alpha"


@pytest.mark.asyncio
async def test_retrieve_preserves_partial_canonical_provenance(monkeypatch):
    hit = HybridHit(
        id="entity:alpha",
        label="Alpha",
        score=0.8,
        source="graph",
        citation=Citation(
            source_id="entity:alpha",
            chunk_id="derived:alpha",
            span_start=None,
            span_end=None,
            quote=None,
        ),
        extraction_method="INFERRED",
        provenance="canonical",
    )
    monkeypatch.setattr(context_api, "hybrid_retrieve", AsyncMock(return_value=[hit]))

    result = await context_api.retrieve(
        context_api.RetrieveRequest(query="Alpha"),
        CurrentUser(username="owner", role="owner", wiki_id="default"),
        Settings(),
    )

    assert result.hits[0].provenance == "canonical"
    assert result.hits[0].extraction_method == "INFERRED"
    assert result.hits[0].confidence is None
    assert result.hits[0].citations == []
    assert result.citations == []
    assert result.insufficient_evidence is True


@pytest.mark.asyncio
async def test_retrieve_rejects_whitespace_only_query(monkeypatch):
    retrieve = AsyncMock()
    monkeypatch.setattr(context_api, "hybrid_retrieve", retrieve)

    with pytest.raises(HTTPException) as error:
        await context_api.retrieve(
            context_api.RetrieveRequest(query=" "),
            CurrentUser(username="owner", role="owner", wiki_id="default"),
            Settings(),
        )

    assert error.value.status_code == 400
    retrieve.assert_not_awaited()
