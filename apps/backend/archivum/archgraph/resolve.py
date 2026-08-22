from __future__ import annotations

from collections import defaultdict

from archivum.archgraph.models import CodeEdge, CodeNode, Extraction, ExtractionMethod


# Relations whose target a call/heritage site knows only by bare name, and which
# therefore have to be matched back to a real symbol. `inherits` belongs here for
# the same reason `calls` does: `class Retry(Base)` names Base, not its id.
_RESOLVABLE_RELATIONS: frozenset[str] = frozenset({"calls", "references", "inherits"})


def _bare_name(node_id: str) -> str:
    """Return the last underscore-separated segment of a node id."""
    return node_id.rsplit("_", 1)[-1]


def resolve_cross_file(extractions: list[Extraction]) -> list[CodeEdge]:
    """Return new cross-file edges derived from unresolved targets.

    Algorithm:
    1. Build combined node set and a symbol table: bare_name -> [node_id, ...]
    2. For every calls/references edge whose target is NOT in the combined node
       set, look up the target's bare name in the symbol table.
       - 1 match in a different file  -> emit INFERRED edge
       - 2+ matches                   -> emit AMBIGUOUS edge to each candidate
       - 0 matches                    -> drop (truly external)
    3. Never re-emit an edge already present as EXTRACTED with same
       (source, target, relation).
    """
    # Combined node id set and file mapping
    all_node_ids: set[str] = set()
    node_file: dict[str, str] = {}
    for ext in extractions:
        for node in ext.nodes:
            all_node_ids.add(node.id)
            node_file[node.id] = node.source_file

    # Symbol table: bare_name -> list of node ids. Iterate sorted so candidate
    # order (and thus AMBIGUOUS edge emission order) is stable across processes,
    # not dependent on set iteration / PYTHONHASHSEED.
    symbol_table: dict[str, list[str]] = defaultdict(list)
    for node_id in sorted(all_node_ids):
        symbol_table[_bare_name(node_id)].append(node_id)

    # Existing EXTRACTED edges keyed by (source, target, relation)
    extracted_keys: set[tuple[str, str, str]] = set()
    for ext in extractions:
        for edge in ext.edges:
            if edge.method == ExtractionMethod.EXTRACTED:
                extracted_keys.add((edge.source, edge.target, edge.relation))

    new_edges: list[CodeEdge] = []

    for ext in extractions:
        for edge in ext.edges:
            if edge.relation not in _RESOLVABLE_RELATIONS:
                continue
            if edge.target in all_node_ids:
                # Target already known -> not an unresolved external
                continue

            # Unresolved: look up bare name of target
            key = _bare_name(edge.target)
            # Skip ultra-short bare names (e.g. "id", "os", "x"): they match too
            # promiscuously and would fabricate low-value AMBIGUOUS edges.
            if len(key) < 3:
                continue
            candidates = symbol_table.get(key, [])

            # A name defined in the same file is what the call site meant. This
            # is the common case in any codebase, and skipping it — as this used
            # to — left the majority of calls pointing at a bare name that
            # matched no node, so the graph came out almost entirely edgeless.
            # Resolution within one file is certain, so it is EXTRACTED.
            same_file = [c for c in candidates if node_file.get(c) == edge.source_file]
            if len(same_file) == 1:
                target_id = same_file[0]
                if (edge.source, target_id, edge.relation) not in extracted_keys:
                    new_edges.append(
                        CodeEdge(
                            source=edge.source,
                            target=target_id,
                            relation=edge.relation,
                            method=ExtractionMethod.EXTRACTED,
                            source_file=edge.source_file,
                            source_location=edge.source_location,
                            confidence=edge.confidence,
                        )
                    )
                continue

            # Filter out candidates in same file as edge source
            cross_candidates = [
                c for c in candidates
                if node_file.get(c) != edge.source_file
            ]

            if len(cross_candidates) == 0:
                continue
            elif len(cross_candidates) == 1:
                target_id = cross_candidates[0]
                triple = (edge.source, target_id, edge.relation)
                if triple in extracted_keys:
                    continue
                new_edges.append(
                    CodeEdge(
                        source=edge.source,
                        target=target_id,
                        relation=edge.relation,
                        method=ExtractionMethod.INFERRED,
                        source_file=edge.source_file,
                        source_location=edge.source_location,
                        confidence=edge.confidence,
                    )
                )
            else:
                # Ambiguous: emit one edge per candidate
                for target_id in cross_candidates:
                    triple = (edge.source, target_id, edge.relation)
                    if triple in extracted_keys:
                        continue
                    new_edges.append(
                        CodeEdge(
                            source=edge.source,
                            target=target_id,
                            relation=edge.relation,
                            method=ExtractionMethod.AMBIGUOUS,
                            source_file=edge.source_file,
                            source_location=edge.source_location,
                            confidence=edge.confidence,
                        )
                    )

    return new_edges
