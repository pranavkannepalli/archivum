"""A code graph whose edges point at nothing is not a graph.

Symbol ids are namespaced by repository and file (`repo_atlas_geo_haversine`),
but a plain call site only knows the bare name it called (`normalise`). Cross-
file resolution existed to close that gap and deliberately skipped candidates in
the *same* file — which is where the large majority of calls actually resolve.

The result was an almost edgeless graph: every symbol its own island, community
detection with nothing to cluster, retrieval BFS that expanded to nothing, and
an audit that reported the whole repository as orphans.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archivum.archgraph.extract import extract_file
from archivum.archgraph.ingest import ingest_repo
from archivum.archgraph.resolve import resolve_cross_file
from archivum.knowledge.graph_audit import build_adjacency

GEO = (
    "def haversine(lat, lon):\n"
    "    return normalise(lat) + normalise(lon)\n"
    "\n\n"
    "def normalise(value):\n"
    "    return value % 360\n"
)


def _extract(tmp_path: Path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path, extract_file(path, root=tmp_path, scope="repo:atlas")


def test_a_call_within_one_file_resolves_to_the_symbol_it_names(tmp_path):
    _, extraction = _extract(tmp_path, "geo.py", GEO)
    node_ids = {node.id for node in extraction.nodes}
    edges = extraction.edges + resolve_cross_file([extraction])

    calls = [edge for edge in edges if edge.relation == "calls"]
    assert calls, "the call to normalise should produce an edge"
    resolved = [edge for edge in calls if edge.target in node_ids]
    assert resolved, (
        "a call to a function defined in the same file must point at that "
        f"function's node; targets were {[e.target for e in calls]}"
    )


def test_a_file_is_connected_to_the_symbols_it_defines(tmp_path):
    _, extraction = _extract(tmp_path, "geo.py", GEO)

    files = [node for node in extraction.nodes if node.kind == "file"]
    symbols = [node for node in extraction.nodes if node.kind == "symbol"]
    assert files and symbols

    defines = [
        edge
        for edge in extraction.edges
        if edge.source == files[0].id and edge.target in {s.id for s in symbols}
    ]
    assert len(defines) == len(symbols), (
        "every symbol should be reachable from the file that declares it"
    )


@pytest.mark.asyncio
async def test_an_indexed_repo_produces_a_connected_graph(tmp_path, mock_kuzu_conn):
    """End to end: the stored graph must actually join up."""
    import shutil
    import subprocess

    import aiosqlite

    from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo = tmp_path / "atlas"
    repo.mkdir()
    (repo / "geo.py").write_text(GEO, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.com", "-c", "user.name=T",
         "commit", "-q", "-m", "x"],
        check=True,
    )

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        cache = tmp_path / "cache"
        cache.mkdir()
        await ingest_repo(
            repo, scope="repo:atlas", cache_dir=cache,
            knowledge=knowledge, lexical_conn=conn,
        )
        nodes = await knowledge.list_objects(scope="repo:atlas", limit=500)
        edges = await knowledge.list_relationships(scope="repo:atlas")

    # Every stored relationship must join two records that exist. A dangling
    # edge is not knowledge, it is a pointer to nothing.
    node_ids = {node.id for node in nodes}
    dangling = [
        (edge.src_id, edge.dst_id)
        for edge in edges
        if edge.src_id not in node_ids or edge.dst_id not in node_ids
    ]
    assert not dangling, f"edges point at records that do not exist: {dangling}"

    adjacency = build_adjacency(nodes, edges)
    isolated = [node_id for node_id, neighbours in adjacency.items() if not neighbours]
    assert not isolated, f"these records ended up with no connections at all: {isolated}"


@pytest.mark.asyncio
async def test_the_report_counts_what_was_actually_stored(tmp_path, mock_kuzu_conn):
    """The CLI prints these numbers, so they have to be true.

    Counting candidates rather than records meant two call sites for one callee
    were reported as two edges while the store held one.
    """
    import shutil
    import subprocess

    import aiosqlite

    from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo = tmp_path / "atlas"
    repo.mkdir()
    # haversine calls normalise twice: two call sites, one relationship.
    (repo / "geo.py").write_text(GEO, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.com", "-c", "user.name=T",
         "commit", "-q", "-m", "x"],
        check=True,
    )

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        cache = tmp_path / "cache"
        cache.mkdir()
        report = await ingest_repo(
            repo, scope="repo:atlas", cache_dir=cache,
            knowledge=knowledge, lexical_conn=conn,
        )
        nodes = await knowledge.list_objects(scope="repo:atlas", limit=500)
        edges = await knowledge.list_relationships(scope="repo:atlas")

    assert report.nodes == len(nodes)
    assert report.edges == len(edges)
