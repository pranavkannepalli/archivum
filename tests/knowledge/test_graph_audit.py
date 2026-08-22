import aiosqlite
import pytest

from archivum.knowledge import graph_audit
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.personal_root import SELF_ID, ensure_personal_root
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema


def _citation(quote="evidence"):
    return Citation(
        source_id="source:1",
        chunk_id="chunk:1",
        span_start=0,
        span_end=len(quote),
        quote=quote,
    )


def _self_citation(node_id):
    return Citation(
        source_id=node_id, chunk_id=node_id, span_start=None, span_end=None, quote=node_id
    )


def _node(node_id, *, label=None, kind="page", confidence=1.0, method="EXTRACTED", cited=True):
    """`cited=False` means self-cited: provenance points back at the record."""
    return KnowledgeObject(
        id=node_id,
        kind=kind,
        label=label or node_id,
        scope="wiki:default",
        confidence=confidence,
        extraction_method=method,
        citations=[_citation()] if cited else [_self_citation(node_id)],
        properties={},
    )


def _edge(src, dst, rel_type="references"):
    return KnowledgeRelationship(
        id=f"rel:{src}:{rel_type}:{dst}",
        src_id=src,
        dst_id=dst,
        rel_type=rel_type,
        scope="wiki:default",
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=[_citation()],
        properties={},
    )


def _two_lobes():
    """Two dense triangles joined by a single bridge edge."""
    nodes = [_node(n) for n in ("a1", "a2", "a3", "b1", "b2", "b3")]
    edges = [
        _edge("a1", "a2"),
        _edge("a2", "a3"),
        _edge("a3", "a1"),
        _edge("b1", "b2"),
        _edge("b2", "b3"),
        _edge("b3", "b1"),
        _edge("a1", "b1", rel_type="mentions"),
    ]
    return nodes, edges


# ── Communities ───────────────────────────────────────────────────────────


def test_communities_separate_two_lobes():
    nodes, edges = _two_lobes()
    communities = graph_audit.detect_communities(nodes, edges)
    grouped = {frozenset(community.member_ids) for community in communities}
    assert frozenset({"a1", "a2", "a3"}) in grouped
    assert frozenset({"b1", "b2", "b3"}) in grouped


def test_community_detection_is_deterministic():
    nodes, edges = _two_lobes()
    first = graph_audit.detect_communities(nodes, edges)
    second = graph_audit.detect_communities(list(reversed(nodes)), list(reversed(edges)))
    assert [c.member_ids for c in first] == [c.member_ids for c in second]


def test_large_graphs_fall_back_to_connected_components():
    nodes, edges = _two_lobes()
    communities = graph_audit.detect_communities(nodes, edges, max_modularity_nodes=1)
    # Components cannot split a bridged graph, so both lobes land together.
    assert [c.size for c in communities] == [6]


def test_isolated_nodes_form_their_own_communities():
    nodes = [_node("a"), _node("b")]
    communities = graph_audit.detect_communities(nodes, [])
    assert [c.member_ids for c in communities] == [("a",), ("b",)]


def test_community_is_labelled_after_its_most_connected_member():
    nodes = [_node("hub", label="Hub page"), _node("x"), _node("y")]
    edges = [_edge("hub", "x"), _edge("hub", "y")]
    communities = graph_audit.detect_communities(nodes, edges)
    assert communities[0].label == "Hub page"


def test_dangling_edges_are_ignored():
    nodes = [_node("a")]
    adjacency = graph_audit.build_adjacency(nodes, [_edge("a", "missing")])
    assert adjacency == {"a": set()}


# ── Shortest path ─────────────────────────────────────────────────────────


def test_shortest_path_crosses_the_bridge():
    nodes, edges = _two_lobes()
    path = graph_audit.shortest_path(nodes, edges, source="a2", target="b2")
    assert path.found is True
    assert [step.to_id for step in path.steps] == ["a1", "b1", "b2"]
    assert path.length == 3


def test_path_traverses_relationships_in_either_direction():
    nodes = [_node("a"), _node("b")]
    path = graph_audit.shortest_path(nodes, [_edge("a", "b")], source="b", target="a")
    assert path.found is True
    assert path.steps[0].relation == "references"


def test_path_to_self_is_empty_but_found():
    nodes = [_node("a")]
    path = graph_audit.shortest_path(nodes, [], source="a", target="a")
    assert path.found is True
    assert path.steps == ()


def test_unknown_node_is_named_in_the_reason():
    nodes = [_node("a")]
    path = graph_audit.shortest_path(nodes, [], source="a", target="zz")
    assert path.found is False
    assert "Unknown node 'zz'" in path.reason


def test_disconnected_nodes_report_no_path():
    nodes = [_node("a"), _node("b")]
    path = graph_audit.shortest_path(nodes, [], source="a", target="b")
    assert path.found is False
    assert "No relationship path" in path.reason


# ── Surprising links ──────────────────────────────────────────────────────


def test_bridge_edge_outranks_within_cluster_edges():
    nodes, edges = _two_lobes()
    links = graph_audit.surprising_links(nodes, edges, limit=10)
    top = links[0]
    assert {top.src_id, top.dst_id} == {"a1", "b1"}
    assert top.cross_community is True
    assert top.score > links[-1].score


def test_surprise_reason_is_plain_language():
    nodes, edges = _two_lobes()
    top = graph_audit.surprising_links(nodes, edges)[0]
    assert "linked to" in top.reason
    assert "different clusters" in top.reason


def test_shared_neighbours_reduce_surprise():
    nodes = [_node(n) for n in ("a", "b", "c")]
    edges = [_edge("a", "b"), _edge("a", "c"), _edge("b", "c")]
    links = {(link.src_id, link.dst_id): link for link in graph_audit.surprising_links(nodes, edges)}
    assert links[("a", "b")].neighbor_overlap > 0
    assert links[("a", "b")].cross_community is False


def test_self_loops_are_not_surprising():
    nodes = [_node("a")]
    assert graph_audit.surprising_links(nodes, [_edge("a", "a")]) == []


# ── Report ────────────────────────────────────────────────────────────────


def test_report_counts_provenance_and_gaps():
    nodes = [
        _node("a", method="EXTRACTED"),
        _node("b", method="INFERRED", confidence=0.3),
        _node("c", method="USER_AUTHORED", cited=False),
    ]
    report = graph_audit.build_graph_report(nodes, [_edge("a", "b")], scope="wiki:default")
    assert report.node_count == 3
    assert report.edge_count == 1
    assert report.by_extraction_method == {
        "EXTRACTED": 1,
        "INFERRED": 1,
        "USER_AUTHORED": 1,
    }
    assert report.self_cited_ids == ("c",)
    assert report.low_confidence_ids == ("b",)
    assert report.orphan_ids == ("c",)


def test_self_citation_is_not_corroboration():
    external = _node("a")
    internal = _node("a", cited=False)
    assert graph_audit.is_self_cited(external) is False
    assert graph_audit.is_self_cited(internal) is True


def test_narrative_reads_as_prose_and_names_the_gaps():
    nodes = [_node("a"), _node("b", cited=False)]
    report = graph_audit.build_graph_report(nodes, [_edge("a", "b")], scope="wiki:default")
    narrative = " ".join(report.narrative)
    assert "wiki:default" in narrative
    assert "cite only themselves" in narrative
    assert "Provenance breakdown" in narrative


def test_narrative_confirms_an_externally_cited_graph():
    nodes, edges = _two_lobes()
    report = graph_audit.build_graph_report(nodes, edges)
    assert any("cites evidence outside itself" in line for line in report.narrative)


def test_edge_count_describes_the_graph_you_can_actually_see():
    """An edge to a record you cannot read is not a relationship you have.

    Edges are scope-filtered separately from nodes, so an edge can point out of
    the audited scope. `build_adjacency` already drops those, meaning clusters
    and orphans were computed on the trimmed graph while `edge_count` reported
    the untrimmed total — the narrative claimed more connectivity than the graph
    had, and a viewer drawing `edges` would show fewer links than the header
    promised.
    """
    nodes, edges = _two_lobes()
    visible = len(graph_audit.build_graph_report(nodes, edges).edge_list)

    report = graph_audit.build_graph_report(
        nodes, [*edges, _edge("a1", "somewhere-we-cannot-read")]
    )

    assert report.edge_count == visible, "the unreadable far end must not be counted"
    assert len(report.edge_list) == visible
    assert any(f"{visible} relationships" in line for line in report.narrative)


def test_report_serialises_for_transport():
    nodes, edges = _two_lobes()
    payload = graph_audit.report_to_dict(graph_audit.build_graph_report(nodes, edges))
    assert payload["node_count"] == 6
    assert payload["communities"][0]["size"] == 3
    assert payload["surprising_links"][0]["reason"]
    assert isinstance(payload["narrative"], list)


def test_path_serialises_for_transport():
    nodes, edges = _two_lobes()
    payload = graph_audit.path_to_dict(
        graph_audit.shortest_path(nodes, edges, source="a2", target="b2")
    )
    assert payload["found"] is True
    assert payload["steps"][0]["relation"]


# ── Repository integration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_includes_the_owner_root_even_though_it_is_out_of_scope():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        await ensure_personal_root(repo)
        await repo.upsert_object(_node("page:default:notes", label="Notes"))
        await repo.upsert_relationship(_edge(SELF_ID, "page:default:notes", "authored_thought"))

        report = await graph_audit.audit_knowledge_graph(repo, scope="wiki:default")

    assert report.node_count == 2
    assert report.orphan_ids == ()
    assert any(SELF_ID in community.member_ids for community in report.communities)


def _code_node(node_id: str, label: str, kind: str) -> KnowledgeObject:
    return KnowledgeObject(
        id=node_id,
        kind=kind,
        label=label,
        scope="repo:atlas",
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=[
            Citation(
                source_id=node_id, chunk_id="f.py",
                span_start=None, span_end=None, quote="L1-L2",
            )
        ],
        properties={},
    )


def _code_edge(src: str, dst: str, rel: str = "calls") -> KnowledgeRelationship:
    return KnowledgeRelationship(
        id=f"{src}__{rel}__{dst}",
        src_id=src,
        dst_id=dst,
        rel_type=rel,
        scope="repo:atlas",
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=[
            Citation(
                source_id=src, chunk_id="f.py",
                span_start=None, span_end=None, quote="L1-L2",
            )
        ],
        properties={},
    )


def test_a_code_cluster_is_named_after_its_file_not_a_helper():
    """"geo" beats "_normalise_inner" as the name of a cluster.

    Naming by raw degree meant a private helper that happened to be called from
    everywhere became the name of the cluster containing the module it lives in.
    A file or a type says what the cluster *is*; a helper says what it does.
    """
    nodes = [
        _code_node("repo_atlas_geo", "geo", "file"),
        _code_node("repo_atlas_geo_haversine", "haversine", "symbol"),
        _code_node("repo_atlas_geo_bearing", "bearing", "symbol"),
        _code_node("repo_atlas_geo_normalise", "_normalise_inner", "symbol"),
        _code_node("repo_atlas_view_render", "render", "symbol"),
    ]
    edges = [
        _code_edge("repo_atlas_geo", "repo_atlas_geo_haversine", "defines"),
        _code_edge("repo_atlas_geo", "repo_atlas_geo_bearing", "defines"),
        # The helper is called from everywhere, so raw degree makes it the hub —
        # strictly more connected than the file that declares the module.
        _code_edge("repo_atlas_geo_haversine", "repo_atlas_geo_normalise"),
        _code_edge("repo_atlas_geo_bearing", "repo_atlas_geo_normalise"),
        _code_edge("repo_atlas_geo", "repo_atlas_geo_normalise", "defines"),
        # A caller from elsewhere pushes the helper strictly past the file.
        _code_edge("repo_atlas_view_render", "repo_atlas_geo_normalise"),
    ]

    communities = graph_audit.detect_communities(nodes, edges)

    assert communities[0].label == "geo"


def test_a_cluster_with_no_file_falls_back_to_its_hub():
    nodes = [
        _code_node("a", "Alpha", "page"),
        _code_node("b", "Beta", "page"),
        _code_node("c", "Gamma", "page"),
    ]
    edges = [_code_edge("a", "b", "references"), _code_edge("a", "c", "references")]

    communities = graph_audit.detect_communities(nodes, edges)

    assert communities[0].label == "Alpha"
