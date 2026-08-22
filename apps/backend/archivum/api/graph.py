from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from archivum.auth import CurrentUser, get_current_user
from archivum.code_repos import REPO_SCOPE_PREFIX, owned_repo_scopes
from archivum.db import graph, sqlite
from archivum.knowledge import graph_audit
from archivum.knowledge.repository import KnowledgeRepository

# Repo-owned mock fixtures (no Kuzu DB required)
from archivum.scripts.graph_export import DEMO_GRAPH

router = APIRouter(prefix="/api", tags=["graph"])

logger = logging.getLogger(__name__)


async def _readable_scopes(current_user: CurrentUser) -> set[str]:
    """Every scope this caller may read.

    Used both to authorise the requested scope and to bound link traversal: a
    `bridge` edge crosses scopes by design, so following one has to be checked
    against the same set rather than trusted because its near end was allowed.
    """
    allowed = {f"wiki:{current_user.wiki_id}"}
    if current_user.role == "owner":
        allowed.add("person:self")
    allowed |= await owned_repo_scopes(wiki_id=current_user.wiki_id)
    return allowed


async def _authorized_scope(requested: str | None, current_user: CurrentUser) -> str:
    """Resolve the scope to read, defaulting to this wiki.

    Code lives under `repo:<name>` rather than `wiki:<id>`, so a repository
    scope cannot be authorised by inspecting the string. It is authorised by
    the register instead: you may read a repository you registered. Without
    this, every graph route was pinned to the wiki scope and could not see a
    single line of code.
    """
    default_scope = f"wiki:{current_user.wiki_id}"
    if requested is None or not requested.strip():
        return default_scope

    scope = requested.strip()
    allowed = {default_scope}
    if current_user.role == "owner":
        allowed.add("person:self")
    # Only the register can authorise a repository, and only a repository needs
    # it, so the wiki path stays free of the extra query.
    if scope.startswith(REPO_SCOPE_PREFIX):
        allowed |= await owned_repo_scopes(wiki_id=current_user.wiki_id)
    if scope not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Graph scope is not authorized for this wiki",
                "code": "unauthorized_graph_scope",
            },
        )
    return scope


def _format_edges(data: dict) -> list[dict]:
    # Frontend expects edges with `label`
    return [
        {"from": e["from"], "to": e["to"], "label": e.get("type", "")}
        for e in data.get("edges", [])
        if e.get("from") and e.get("to")
    ]


@router.get("/graph")
async def get_graph(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    source = "live"
    try:
        data = await graph.get_all_nodes_edges(current_user.wiki_id)
    except Exception as e:
        # Local/mock-safe fallback: if Kuzu isn't configured, still return a usable demo graph.
        logger.warning("graph.get_all_nodes_edges failed; falling back to demo graph", extra={"error": str(e)})
        data = DEMO_GRAPH
        source = "demo"

    # `source` is not cosmetic: without it a fabricated graph is indistinguishable
    # from the user's real one. Clients must surface "demo" to the viewer.
    return {
        "nodes": data.get("nodes", []),
        "edges": _format_edges(data),
        "source": source,
    }


@router.get("/graph/demo")
async def get_graph_demo(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    """Explicit demo graph endpoint (no DB required)."""
    data = DEMO_GRAPH
    return {"nodes": data.get("nodes", []), "edges": _format_edges(data)}


@router.get("/graph/neighbors")
async def graph_neighbors(
    node_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        data = await graph.get_neighbors(node_id, current_user.wiki_id)
    except Exception as e:
        logger.warning("graph.get_neighbors failed; returning empty neighbors", extra={"error": str(e), "node_id": node_id})
        data = {"center": node_id, "nodes": [], "edges": []}

    return {
        "center": data.get("center"),
        "nodes": data.get("nodes", []),
        "edges": _format_edges(data),
    }


# ── Canonical graph audit (Graphify-style discovery) ──────────────────────


@router.get("/graph/audit")
async def graph_report(
    surprise_limit: int = Query(default=10, ge=1, le=50),
    scope: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Plain-language audit of a canonical knowledge graph, wiki or repository."""
    scope = await _authorized_scope(scope, current_user)
    readable = await _readable_scopes(current_user)
    async with sqlite.get_db() as conn:
        report = await graph_audit.audit_knowledge_graph(
            KnowledgeRepository(conn),
            scope=scope,
            surprise_limit=surprise_limit,
            allowed_scopes=readable,
        )
    return graph_audit.report_to_dict(report)


@router.get("/graph/communities")
async def graph_communities(
    scope: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Cluster canonical records by label propagation over relationships."""
    scope = await _authorized_scope(scope, current_user)
    readable = await _readable_scopes(current_user)
    async with sqlite.get_db() as conn:
        nodes, edges = await graph_audit.load_graph(
            KnowledgeRepository(conn), scope=scope, allowed_scopes=readable
        )
    communities = graph_audit.detect_communities(nodes, edges)
    return {
        "scope": scope,
        "communities": [
            {
                "id": community.id,
                "label": community.label,
                "size": community.size,
                "member_ids": list(community.member_ids),
            }
            for community in communities
        ],
    }


@router.get("/graph/surprising")
async def graph_surprising(
    limit: int = Query(default=10, ge=1, le=50),
    scope: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Rank the connections least predictable from the rest of the graph."""
    scope = await _authorized_scope(scope, current_user)
    readable = await _readable_scopes(current_user)
    async with sqlite.get_db() as conn:
        nodes, edges = await graph_audit.load_graph(
            KnowledgeRepository(conn), scope=scope, allowed_scopes=readable
        )
    links = graph_audit.surprising_links(nodes, edges, limit=limit)
    return {
        "scope": scope,
        "links": [
            {
                "src_id": link.src_id,
                "dst_id": link.dst_id,
                "src_label": link.src_label,
                "dst_label": link.dst_label,
                "rel_type": link.rel_type,
                "score": link.score,
                "neighbor_overlap": link.neighbor_overlap,
                "cross_community": link.cross_community,
                "reason": link.reason,
            }
            for link in links
        ],
    }


@router.get("/graph/path")
async def graph_path(
    source: str = Query(min_length=1),
    target: str = Query(min_length=1),
    scope: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Shortest relationship path between two canonical records."""
    scope = await _authorized_scope(scope, current_user)
    readable = await _readable_scopes(current_user)
    async with sqlite.get_db() as conn:
        nodes, edges = await graph_audit.load_graph(
            KnowledgeRepository(conn), scope=scope, allowed_scopes=readable
        )
    path = graph_audit.shortest_path(nodes, edges, source=source, target=target)
    if not path.found and path.reason and path.reason.startswith("Unknown node"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": path.reason, "code": "unknown_graph_node"},
        )
    return graph_audit.path_to_dict(path)

