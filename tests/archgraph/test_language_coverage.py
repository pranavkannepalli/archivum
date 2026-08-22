"""What a developer's repositories are actually written in.

Extraction covered `.py` and `.ts` only, so a JavaScript file — still the most
common thing in most people's repos — became a prose page and never a single
code record. The TypeScript grammar is a superset of JavaScript and the TSX
grammar handles JSX, so this is coverage the parser already had and the registry
simply never offered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archivum.archgraph.extract import extract_file
from archivum.archgraph.resolve import resolve_cross_file
from archivum.archgraph.registry import CODE_SUFFIXES, config_for_suffix

JS = (
    "export function haversine(lat, lon) {\n"
    "  return normalise(lat) + normalise(lon);\n"
    "}\n"
    "\n"
    "function normalise(value) {\n"
    "  return value % 360;\n"
    "}\n"
)

JSX = (
    "export function Panel(props) {\n"
    "  return <div>{label(props)}</div>;\n"
    "}\n"
    "\n"
    "function label(props) {\n"
    "  return props.name;\n"
    "}\n"
)


@pytest.mark.parametrize("suffix", [".js", ".jsx", ".mjs", ".cjs"])
def test_javascript_files_are_recognised(suffix):
    assert suffix in CODE_SUFFIXES
    assert config_for_suffix(suffix) is not None


def test_symbols_are_extracted_from_plain_javascript(tmp_path):
    path = tmp_path / "geo.js"
    path.write_text(JS, encoding="utf-8")

    extraction = extract_file(path, root=tmp_path, scope="repo:atlas")

    assert extraction.error is None
    labels = {node.label for node in extraction.nodes if node.kind == "symbol"}
    assert {"haversine", "normalise"} <= labels


def test_symbols_are_extracted_from_jsx(tmp_path):
    path = tmp_path / "panel.jsx"
    path.write_text(JSX, encoding="utf-8")

    extraction = extract_file(path, root=tmp_path, scope="repo:atlas")

    assert extraction.error is None
    labels = {node.label for node in extraction.nodes if node.kind == "symbol"}
    assert {"Panel", "label"} <= labels


PY_INHERIT = (
    "class Base:\n"
    "    pass\n"
    "\n\n"
    "class Retry(Base):\n"
    "    pass\n"
)

TS_INHERIT = (
    "export class Base {}\n"
    "export class Retry extends Base {}\n"
)


def test_python_subclassing_becomes_an_edge(tmp_path):
    """`inherits` is in the edge model and was never once emitted.

    Type hierarchies are some of the strongest structure in a codebase, so
    leaving them out cost the clustering its clearest signal.
    """
    path = tmp_path / "shapes.py"
    path.write_text(PY_INHERIT, encoding="utf-8")

    extraction = extract_file(path, root=tmp_path, scope="repo:atlas")
    resolved = extraction.edges + resolve_cross_file([extraction])

    node_ids = {node.id for node in extraction.nodes}
    inherits = [
        edge
        for edge in resolved
        if edge.relation == "inherits" and edge.target in node_ids
    ]
    assert len(inherits) == 1
    ids = {node.label: node.id for node in extraction.nodes}
    assert inherits[0].source == ids["Retry"]
    assert inherits[0].target == ids["Base"]


def test_typescript_extends_becomes_an_edge(tmp_path):
    path = tmp_path / "shapes.ts"
    path.write_text(TS_INHERIT, encoding="utf-8")

    extraction = extract_file(path, root=tmp_path, scope="repo:atlas")
    resolved = extraction.edges + resolve_cross_file([extraction])

    node_ids = {node.id for node in extraction.nodes}
    inherits = [
        edge
        for edge in resolved
        if edge.relation == "inherits" and edge.target in node_ids
    ]
    assert len(inherits) == 1
    ids = {node.label: node.id for node in extraction.nodes}
    assert inherits[0].source == ids["Retry"]
    assert inherits[0].target == ids["Base"]
