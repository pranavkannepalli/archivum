"""Graph-native audit over canonical knowledge: communities, paths, surprise.

Everything here is a deterministic graph algorithm over records already in the
store. No LLM, no extra dependency, no sampling — the same graph always yields
the same report, which is what makes the audit trustworthy.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field, replace
from typing import Any

from archivum.knowledge.models import KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.personal_root import SELF_ID
from archivum.knowledge.repository import KnowledgeRepository

_AUDIT_OBJECT_LIMIT = 20_000
LOW_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_SURPRISE_LIMIT = 10

# Greedy modularity is agglomerative and quadratic in the worst case. Above
# this many nodes the audit falls back to connected components, which is linear
# and still meaningful — the report says which method was used.
MAX_MODULARITY_NODES = 3_000
_MODULARITY_EPSILON = 1e-12

# Weighting for the surprise score. Structural novelty (few shared neighbours)
# dominates; crossing a community boundary is the secondary signal.
# Scopes that exist to join two other scopes rather than to hold a graph of
# their own: cross-repository identity, and code-to-evidence bridging.
LINK_SCOPES: tuple[str, ...] = ("bridge", "cross_repo")

_NOVELTY_WEIGHT = 0.6
_CROSS_COMMUNITY_WEIGHT = 0.4


@dataclass(frozen=True)
class Community:
    id: str
    label: str
    member_ids: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.member_ids)


@dataclass(frozen=True)
class PathStep:
    from_id: str
    to_id: str
    relation: str


@dataclass(frozen=True)
class GraphPath:
    source: str
    target: str
    found: bool
    steps: tuple[PathStep, ...] = ()
    reason: str | None = None

    @property
    def length(self) -> int:
        return len(self.steps)


@dataclass(frozen=True)
class SurprisingLink:
    src_id: str
    dst_id: str
    src_label: str
    dst_label: str
    rel_type: str
    score: float
    neighbor_overlap: float
    cross_community: bool
    reason: str


@dataclass(frozen=True)
class GraphReport:
    scope: str | None
    node_count: int
    edge_count: int
    by_kind: dict[str, int]
    by_extraction_method: dict[str, int]
    self_cited_ids: tuple[str, ...]
    low_confidence_ids: tuple[str, ...]
    orphan_ids: tuple[str, ...]
    communities: tuple[Community, ...]
    surprising_links: tuple[SurprisingLink, ...]
    narrative: tuple[str, ...] = field(default=())
    # Labels and kinds for every record in the report. Anything drawing the
    # graph needs a name and a shape per node, and looking those up separately
    # meant a second, differently-scoped request that missed everything when
    # the report was pointed at a repository.
    node_labels: dict[str, str] = field(default_factory=dict)
    node_kinds: dict[str, str] = field(default_factory=dict)
    # The relationships the analysis ran over. Without these a caller can only
    # place clusters around a centre — a layout, not the graph. Restricted to
    # edges whose endpoints are both in the report, so nothing dangles.
    edge_list: tuple[tuple[str, str, str, str], ...] = field(default=())


# ── Adjacency ─────────────────────────────────────────────────────────────


def build_adjacency(
    nodes: list[KnowledgeObject], edges: list[KnowledgeRelationship]
) -> dict[str, set[str]]:
    """Undirected adjacency restricted to edges whose endpoints both exist."""
    known = {node.id for node in nodes}
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in known}
    for edge in edges:
        if edge.src_id not in known or edge.dst_id not in known:
            continue
        if edge.src_id == edge.dst_id:
            continue
        adjacency[edge.src_id].add(edge.dst_id)
        adjacency[edge.dst_id].add(edge.src_id)
    return adjacency


# ── Communities ───────────────────────────────────────────────────────────


def detect_communities(
    nodes: list[KnowledgeObject],
    edges: list[KnowledgeRelationship],
    *,
    max_modularity_nodes: int = MAX_MODULARITY_NODES,
) -> list[Community]:
    """Partition the graph into clusters by greedy modularity maximisation.

    Communities are merged while the merge raises modularity, taking the best
    gain each round and breaking ties on sorted community ids, so the partition
    is reproducible rather than seed-dependent. Above `max_modularity_nodes`
    the agglomeration is too costly, so connected components are used instead.
    """
    adjacency = build_adjacency(nodes, edges)
    groups = (
        _connected_components(adjacency)
        if len(adjacency) > max_modularity_nodes
        else _greedy_modularity(adjacency)
    )

    labels_by_node = {node.id: node.label for node in nodes}
    kinds_by_node = {node.id: node.kind for node in nodes}
    communities = [
        Community(
            id=min(members),
            label=_community_label(members, adjacency, labels_by_node, kinds_by_node),
            member_ids=tuple(sorted(members)),
        )
        for members in groups
    ]
    return sorted(communities, key=lambda community: (-community.size, community.id))


def _connected_components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for node_id in sorted(adjacency):
        if node_id in seen:
            continue
        component: list[str] = []
        queue: deque[str] = deque([node_id])
        seen.add(node_id)
        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbour in sorted(adjacency[current]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        components.append(component)
    return components


def _greedy_modularity(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Clauset-Newman-Moore style agglomeration with deterministic merges.

    For communities i and j joined by `b` edges in a graph of `m` edges,
    merging changes modularity by `b/m - d_i*d_j/(2*m^2)`, where `d` is the
    community's total degree. Merging stops once no positive gain remains.
    """
    degrees = {node_id: len(peers) for node_id, peers in adjacency.items()}
    edge_count = sum(degrees.values()) // 2
    if edge_count == 0:
        return [[node_id] for node_id in sorted(adjacency)]

    members = {node_id: {node_id} for node_id in adjacency}
    community_of = {node_id: node_id for node_id in adjacency}
    degree_sum = dict(degrees)
    between: Counter[tuple[str, str]] = Counter()
    for node_id, peers in adjacency.items():
        for peer in peers:
            if node_id < peer:
                between[(node_id, peer)] += 1

    while between:
        best_pair: tuple[str, str] | None = None
        best_gain = _MODULARITY_EPSILON
        for (left, right), count in sorted(between.items()):
            gain = count / edge_count - (
                degree_sum[left] * degree_sum[right] / (2 * edge_count**2)
            )
            if gain > best_gain:
                best_gain = gain
                best_pair = (left, right)
        if best_pair is None:
            break

        keep, absorb = best_pair
        members[keep] |= members.pop(absorb)
        for node_id in members[keep]:
            community_of[node_id] = keep
        degree_sum[keep] += degree_sum.pop(absorb)

        del between[best_pair]
        for (left, right), count in list(between.items()):
            if absorb not in (left, right):
                continue
            other = right if left == absorb else left
            del between[(left, right)]
            if other == keep:
                continue
            merged_key = (keep, other) if keep < other else (other, keep)
            between[merged_key] += count

    return [sorted(group) for group in members.values()]


# Kinds that name a cluster better than raw connectivity does. A file or a type
# says what a cluster *is*; the most-called helper inside it only says what it
# does, and a shared utility is exactly the node most likely to top the degree
# count while being the least descriptive name available.
_NAMING_KINDS: tuple[str, ...] = ("file", "type", "repo")


def _community_label(
    members: list[str],
    adjacency: dict[str, set[str]],
    labels_by_node: dict[str, str],
    kinds_by_node: dict[str, str] | None = None,
) -> str:
    """Name a community after the member that best describes it."""
    kinds_by_node = kinds_by_node or {}

    def most_connected(candidates: list[str]) -> str:
        return min(candidates, key=lambda node_id: (-len(adjacency[node_id]), node_id))

    for kind in _NAMING_KINDS:
        of_kind = [node_id for node_id in members if kinds_by_node.get(node_id) == kind]
        if of_kind:
            return labels_by_node.get(most_connected(of_kind), most_connected(of_kind))

    anchor = most_connected(members)
    return labels_by_node.get(anchor, anchor)


# ── Shortest path ─────────────────────────────────────────────────────────


def shortest_path(
    nodes: list[KnowledgeObject],
    edges: list[KnowledgeRelationship],
    *,
    source: str,
    target: str,
) -> GraphPath:
    """Breadth-first shortest path, treating relationships as undirected."""
    known = {node.id for node in nodes}
    if source not in known:
        return GraphPath(source, target, False, reason=f"Unknown node '{source}'.")
    if target not in known:
        return GraphPath(source, target, False, reason=f"Unknown node '{target}'.")
    if source == target:
        return GraphPath(source, target, True, steps=())

    # Deterministic neighbour order so ties resolve the same way every run.
    neighbours: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in known}
    for edge in sorted(edges, key=lambda e: (e.src_id, e.dst_id, e.rel_type, e.id)):
        if edge.src_id not in known or edge.dst_id not in known:
            continue
        neighbours[edge.src_id].append((edge.dst_id, edge.rel_type))
        neighbours[edge.dst_id].append((edge.src_id, edge.rel_type))

    previous: dict[str, tuple[str, str]] = {}
    visited = {source}
    queue: deque[str] = deque([source])
    while queue:
        current = queue.popleft()
        for neighbour, relation in neighbours[current]:
            if neighbour in visited:
                continue
            visited.add(neighbour)
            previous[neighbour] = (current, relation)
            if neighbour == target:
                return GraphPath(source, target, True, steps=_rebuild(previous, source, target))
            queue.append(neighbour)

    return GraphPath(
        source,
        target,
        False,
        reason="No relationship path connects these records in this scope.",
    )


def _rebuild(
    previous: dict[str, tuple[str, str]], source: str, target: str
) -> tuple[PathStep, ...]:
    steps: list[PathStep] = []
    cursor = target
    while cursor != source:
        parent, relation = previous[cursor]
        steps.append(PathStep(from_id=parent, to_id=cursor, relation=relation))
        cursor = parent
    return tuple(reversed(steps))


# ── Surprising links ──────────────────────────────────────────────────────


def surprising_links(
    nodes: list[KnowledgeObject],
    edges: list[KnowledgeRelationship],
    *,
    communities: list[Community] | None = None,
    limit: int = DEFAULT_SURPRISE_LIMIT,
) -> list[SurprisingLink]:
    """Rank edges by how little their endpoints otherwise have in common.

    A link is surprising when the two records share almost no neighbours and
    sit in different communities: that is the edge a reader would not have
    predicted from the rest of the graph.
    """
    adjacency = build_adjacency(nodes, edges)
    communities = communities if communities is not None else detect_communities(nodes, edges)
    community_of = {
        member: community.id for community in communities for member in community.member_ids
    }
    labels = {node.id: node.label for node in nodes}

    ranked: list[SurprisingLink] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        if edge.src_id not in adjacency or edge.dst_id not in adjacency:
            continue
        if edge.src_id == edge.dst_id:
            continue
        low, high = sorted((edge.src_id, edge.dst_id))
        key = (low, high, edge.rel_type)
        if key in seen:
            continue
        seen.add(key)

        overlap = _neighbour_overlap(adjacency, edge.src_id, edge.dst_id)
        cross = community_of.get(edge.src_id) != community_of.get(edge.dst_id)
        score = round(
            _NOVELTY_WEIGHT * (1.0 - overlap) + _CROSS_COMMUNITY_WEIGHT * float(cross),
            4,
        )
        ranked.append(
            SurprisingLink(
                src_id=edge.src_id,
                dst_id=edge.dst_id,
                src_label=labels.get(edge.src_id, edge.src_id),
                dst_label=labels.get(edge.dst_id, edge.dst_id),
                rel_type=edge.rel_type,
                score=score,
                neighbor_overlap=overlap,
                cross_community=cross,
                reason=_surprise_reason(labels, edge, overlap, cross),
            )
        )

    ranked.sort(key=lambda link: (-link.score, link.src_id, link.dst_id, link.rel_type))
    return ranked[: max(limit, 0)]


def _neighbour_overlap(adjacency: dict[str, set[str]], src: str, dst: str) -> float:
    left = adjacency[src] - {dst}
    right = adjacency[dst] - {src}
    union = left | right
    if not union:
        return 0.0
    return round(len(left & right) / len(union), 4)


def _surprise_reason(
    labels: dict[str, str],
    edge: KnowledgeRelationship,
    overlap: float,
    cross: bool,
) -> str:
    src = labels.get(edge.src_id, edge.src_id)
    dst = labels.get(edge.dst_id, edge.dst_id)
    shared = (
        "they share no other connections"
        if overlap == 0.0
        else f"they share only {overlap:.0%} of their connections"
    )
    where = "different clusters" if cross else "the same cluster"
    return f"'{src}' is linked to '{dst}' via {edge.rel_type}, but {shared} and they sit in {where}."


# ── Report ────────────────────────────────────────────────────────────────


def is_self_cited(node: KnowledgeObject) -> bool:
    """True when a record's only provenance is a pointer back at itself.

    Canonical objects always carry at least one citation, so the honest audit
    question is not "is it cited" but "does any citation point at evidence
    outside the record". Owner-root and page-title citations are self-referential
    by construction and should not read as corroboration.
    """
    return all(
        citation.source_id == node.id or citation.chunk_id == node.id
        for citation in node.citations
    )


def build_graph_report(
    nodes: list[KnowledgeObject],
    edges: list[KnowledgeRelationship],
    *,
    scope: str | None = None,
    surprise_limit: int = DEFAULT_SURPRISE_LIMIT,
) -> GraphReport:
    """Assemble the full audit, including a plain-language narrative."""
    adjacency = build_adjacency(nodes, edges)
    communities = detect_communities(nodes, edges)
    links = surprising_links(nodes, edges, communities=communities, limit=surprise_limit)

    # Nodes and edges are scope-filtered by separate queries, so an edge can
    # point at a record outside the audited scope — one the caller may not read
    # and that `_add_incident_links` could not pull in. `build_adjacency` already
    # ignores those, so clusters and orphans describe the trimmed graph; count
    # the same trimmed set here or the narrative claims connectivity that is not
    # there and a viewer draws fewer links than the header promises.
    known_ids = {node.id for node in nodes}
    visible_edges = [
        edge for edge in edges if edge.src_id in known_ids and edge.dst_id in known_ids
    ]
    by_kind = Counter(node.kind for node in nodes)
    by_method = Counter(node.extraction_method for node in nodes)
    self_cited = tuple(sorted(node.id for node in nodes if is_self_cited(node)))
    low_confidence = tuple(
        sorted(node.id for node in nodes if node.confidence < LOW_CONFIDENCE_THRESHOLD)
    )
    orphans = tuple(sorted(node_id for node_id, peers in adjacency.items() if not peers))

    report = GraphReport(
        scope=scope,
        node_count=len(nodes),
        edge_count=len(visible_edges),
        by_kind=dict(sorted(by_kind.items())),
        by_extraction_method=dict(sorted(by_method.items())),
        self_cited_ids=self_cited,
        low_confidence_ids=low_confidence,
        orphan_ids=orphans,
        communities=tuple(communities),
        surprising_links=tuple(links),
        node_labels={node.id: node.label for node in nodes},
        node_kinds={node.id: node.kind for node in nodes},
        edge_list=tuple(
            (edge.src_id, edge.dst_id, edge.rel_type, edge.extraction_method)
            for edge in visible_edges
        ),
    )
    return replace(report, narrative=_narrative(report))


def _narrative(report: GraphReport) -> tuple[str, ...]:
    lines: list[str] = []
    scope_text = f" in scope {report.scope}" if report.scope else ""
    lines.append(
        f"The graph{scope_text} holds {report.node_count} records and "
        f"{report.edge_count} relationships across {len(report.communities)} clusters."
    )

    if report.node_count:
        method_parts = [
            f"{count} {method.lower().replace('_', '-')}"
            for method, count in report.by_extraction_method.items()
        ]
        lines.append("Provenance breakdown: " + ", ".join(method_parts) + ".")

    if report.self_cited_ids:
        lines.append(
            f"{len(report.self_cited_ids)} records cite only themselves, so nothing "
            "outside the record corroborates them."
        )
    elif report.node_count:
        lines.append("Every record cites evidence outside itself.")

    if report.low_confidence_ids:
        lines.append(
            f"{len(report.low_confidence_ids)} records sit below "
            f"{LOW_CONFIDENCE_THRESHOLD:.2f} confidence and are worth reviewing."
        )

    if report.orphan_ids:
        lines.append(
            f"{len(report.orphan_ids)} records have no relationships at all, so they "
            "cannot be reached by graph traversal."
        )

    if report.communities:
        largest = report.communities[0]
        lines.append(
            f"The largest cluster centres on '{largest.label}' with {largest.size} records."
        )

    if report.surprising_links:
        top = report.surprising_links[0]
        lines.append(f"Most surprising connection ({top.score:.2f}): {top.reason}")

    return tuple(lines)


# ── Repository wrapper ────────────────────────────────────────────────────


async def audit_knowledge_graph(
    repo: KnowledgeRepository,
    *,
    scope: str | None = None,
    surprise_limit: int = DEFAULT_SURPRISE_LIMIT,
    allowed_scopes: set[str] | None = None,
) -> GraphReport:
    nodes, edges = await load_graph(repo, scope=scope, allowed_scopes=allowed_scopes)
    return build_graph_report(
        nodes, edges, scope=scope, surprise_limit=surprise_limit
    )


async def load_graph(
    repo: KnowledgeRepository,
    *,
    scope: str | None = None,
    allowed_scopes: set[str] | None = None,
) -> tuple[list[KnowledgeObject], list[KnowledgeRelationship]]:
    """Load a scoped graph, always including the owner root and any links in.

    `person:self` lives in its own scope but owns edges into every wiki scope,
    so omitting it would hide the hub that most of the graph hangs off.

    Link scopes are the same problem one level out. A `decided_in` edge joins a
    code symbol to the conversation it came from, so by construction it belongs
    to neither the repository's scope nor the wiki's. Loading strictly by scope
    would drop exactly the edges that explain why a thing exists — so links that
    touch a loaded record are pulled in, along with what they point at.

    Following a link crosses a scope boundary, so it needs its own authorisation.
    `allowed_scopes` is what the caller may read; anything the caller has not
    been granted is left out rather than fetched by id. Callers that pass nothing
    get the requested scope only, because a link is not worth disclosing another
    vault's records by default.
    """
    nodes = await repo.list_objects(scope=scope, limit=_AUDIT_OBJECT_LIMIT)
    if len(nodes) == _AUDIT_OBJECT_LIMIT:
        raise RuntimeError(
            "Graph audit reached its object limit; narrow the scope before retrying."
        )
    if scope is not None and all(node.id != SELF_ID for node in nodes):
        root = await repo.get_object(SELF_ID)
        if root is not None:
            nodes.append(root)
    edges = await repo.list_relationships(scope=scope)

    if scope is not None:
        readable = set(allowed_scopes) if allowed_scopes is not None else {scope}
        readable.add(scope)
        nodes, edges = await _add_incident_links(repo, nodes, edges, readable)
    return nodes, edges


async def _add_incident_links(
    repo: KnowledgeRepository,
    nodes: list[KnowledgeObject],
    edges: list[KnowledgeRelationship],
    readable_scopes: set[str],
) -> tuple[list[KnowledgeObject], list[KnowledgeRelationship]]:
    """Pull in link-scope edges whose far end the caller is allowed to read."""
    known = {node.id for node in nodes}
    incident: list[KnowledgeRelationship] = []
    wanted: set[str] = set()

    for link_scope in LINK_SCOPES:
        for edge in await repo.list_relationships(scope=link_scope):
            touches_src = edge.src_id in known
            touches_dst = edge.dst_id in known
            if not (touches_src or touches_dst):
                continue
            incident.append(edge)
            if not touches_src:
                wanted.add(edge.src_id)
            if not touches_dst:
                wanted.add(edge.dst_id)

    if not incident:
        return nodes, edges

    # An edge is only kept once its far end has been fetched *and* cleared, so
    # an unreadable neighbour discloses neither its label nor the fact of the
    # connection.
    disclosed: set[str] = set()
    for node_id in sorted(wanted):
        far_end = await repo.get_object(node_id)
        if far_end is None or far_end.scope not in readable_scopes:
            continue
        nodes.append(far_end)
        disclosed.add(node_id)

    visible = known | disclosed
    kept = [
        edge
        for edge in incident
        if edge.src_id in visible and edge.dst_id in visible
    ]
    return nodes, [*edges, *kept]


def report_to_dict(report: GraphReport) -> dict[str, Any]:
    return {
        "scope": report.scope,
        "node_count": report.node_count,
        "edge_count": report.edge_count,
        "by_kind": report.by_kind,
        "by_extraction_method": report.by_extraction_method,
        "self_cited_ids": list(report.self_cited_ids),
        "low_confidence_ids": list(report.low_confidence_ids),
        "orphan_ids": list(report.orphan_ids),
        "communities": [
            {
                "id": community.id,
                "label": community.label,
                "size": community.size,
                "member_ids": list(community.member_ids),
            }
            for community in report.communities
        ],
        "surprising_links": [
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
            for link in report.surprising_links
        ],
        "narrative": list(report.narrative),
        "node_labels": report.node_labels,
        "node_kinds": report.node_kinds,
        "edges": [
            {"source": src, "target": dst, "relation": rel, "extraction_method": method}
            for src, dst, rel, method in report.edge_list
        ],
    }


def path_to_dict(path: GraphPath) -> dict[str, Any]:
    return {
        "source": path.source,
        "target": path.target,
        "found": path.found,
        "length": path.length,
        "steps": [
            {"from_id": step.from_id, "to_id": step.to_id, "relation": step.relation}
            for step in path.steps
        ],
        "reason": path.reason,
    }
