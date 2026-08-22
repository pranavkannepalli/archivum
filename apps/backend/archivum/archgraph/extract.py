from __future__ import annotations

from pathlib import Path

from archivum.archgraph.extractors.base import (
    _file_namespace,
    _file_stem,
    _make_id,
    _read_text,
    _source_location,
)
from archivum.archgraph.models import CodeEdge, CodeNode, Extraction, ExtractionMethod
from archivum.archgraph.registry import LanguageConfig, config_for_suffix, load_parser

# Enough to identify a symbol, not enough to paste a file into the graph. The
# canonical answer is always the cited source; these are for recognition.
_MAX_SIGNATURE = 300
_MAX_SUMMARY = 200


def extract_file(path: Path, *, root: Path | None = None, scope: str | None = None) -> Extraction:
    """Dispatch extraction by file suffix. Returns Extraction with error on unknown suffix."""
    cfg = config_for_suffix(path.suffix)
    if cfg is None:
        return Extraction(nodes=[], edges=[], error=f"unsupported suffix {path.suffix!r}")
    return _extract_generic(path, cfg, root=root, scope=scope)


def _extract_generic(
    path: Path,
    cfg: LanguageConfig,
    *,
    root: Path | None = None,
    scope: str | None = None,
) -> Extraction:
    """Walk a tree-sitter parse tree and emit CodeNodes + CodeEdges."""
    try:
        source = path.read_bytes()
        parser = load_parser(cfg)
        tree = parser.parse(source)
        tree_root = tree.root_node
    except Exception as exc:
        return Extraction(nodes=[], edges=[], error=str(exc))

    nodes: list[CodeNode] = []
    edges: list[CodeEdge] = []
    # Maps locally-imported names to their canonical target ids.
    # Built during _emit_import; consumed by _emit_call for bare-name resolution.
    named_imports: dict[str, str] = {}

    stem = _file_stem(path)
    file_namespace = _file_namespace(path, root=root, scope=scope)
    file_id = _make_id(file_namespace)

    # Emit the file node
    nodes.append(
        CodeNode(
            id=file_id,
            label=stem,
            kind="file",
            source_file=str(path),
            source_location=_source_location(tree_root),
        )
    )

    # First pass: collect class names and their node spans for enclosing-class lookup
    # Build a mapping: tree-sitter node id -> class name (for class_definition nodes)
    class_node_names: dict[int, str] = {}

    def _find_classes(node) -> None:  # type: ignore[no-untyped-def]
        if node.type in cfg.class_types:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                class_node_names[node.id] = _read_text(name_node, source)
        for child in node.children:
            _find_classes(child)

    _find_classes(tree_root)

    def _enclosing_class(node) -> str | None:
        """Walk ancestors looking for a class_definition node."""
        current = node.parent
        while current is not None:
            if current.type in cfg.class_types:
                return class_node_names.get(current.id)
            current = current.parent
        return None

    def _emit_inheritance(node, type_id: str) -> None:  # type: ignore[no-untyped-def]
        """Record what this type extends.

        `inherits` was in the edge model from the start and never once emitted.
        A type hierarchy is some of the strongest structure a codebase has, so
        leaving it out cost the clustering its clearest signal — and cost anyone
        reading the graph the answer to "what is this a kind of?".

        Base names are emitted bare, exactly like call targets, and resolved by
        the same pass; an unresolvable base (an imported third-party class) is
        dropped rather than stored as a pointer to nothing.
        """
        # Python: `class Retry(Base)` -> argument_list. TS/JS: `class Retry
        # extends Base` -> class_heritage. Both hold plain identifiers.
        for child in node.children:
            if child.type not in ("argument_list", "class_heritage"):
                continue
            for base in _identifier_names(child):
                edges.append(
                    CodeEdge(
                        source=type_id,
                        target=_make_id(base),
                        relation="inherits",
                        method=ExtractionMethod.EXTRACTED,
                        source_file=str(path),
                        source_location=_source_location(node),
                    )
                )

    def _signature_of(node) -> str:  # type: ignore[no-untyped-def]
        """The declaration line, without the body.

        A name alone tells an agent almost nothing; the parameters and return
        type are what let it decide whether a symbol is the one it wants without
        opening the file.
        """
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        text = source[node.start_byte:end].decode("utf-8", "replace")
        # Collapse a wrapped signature onto one readable line.
        signature = " ".join(text.split()).rstrip("{:").strip()
        return signature[:_MAX_SIGNATURE] if signature else ""

    def _summary_of(node) -> str:  # type: ignore[no-untyped-def]
        """The first line of the docstring, if the author wrote one.

        Only what is actually written: an absent docstring stays absent rather
        than being replaced by a guess at what the code does.
        """
        body = node.child_by_field_name("body")
        if body is None:
            return ""
        for child in body.children:
            # Python: a bare string expression opening the body. TS/JS has no
            # equivalent construct, so this correctly finds nothing there.
            if child.type != "expression_statement":
                continue
            for grandchild in child.children:
                if grandchild.type != "string":
                    continue
                raw = _read_text(grandchild, source)
                text = raw.strip().strip('"\'').strip()
                first = next((line.strip() for line in text.splitlines() if line.strip()), "")
                return first[:_MAX_SUMMARY]
            return ""
        return ""

    def _identifier_names(node) -> list[str]:  # type: ignore[no-untyped-def]
        """Identifier text under a node, outermost first, deduplicated."""
        found: list[str] = []
        stack = list(node.children)
        while stack:
            current = stack.pop(0)
            if current.type in ("identifier", "type_identifier"):
                text = _read_text(current, source)
                if text and text not in found:
                    found.append(text)
                continue
            stack.extend(current.children)
        return found

    def _walk(node) -> None:  # type: ignore[no-untyped-def]
        if node.type in cfg.class_types:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _read_text(name_node, source)
                nodes.append(
                    CodeNode(
                        id=_make_id(file_namespace, name),
                        label=name,
                        kind="type",
                        source_file=str(path),
                        source_location=_source_location(node),
                        properties={
                            "signature": _signature_of(node),
                            "summary": _summary_of(node),
                        },
                    )
                )
                _emit_inheritance(node, _make_id(file_namespace, name))

        elif node.type in cfg.function_types:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                fname = _read_text(name_node, source)
                enc_class = _enclosing_class(node)
                if enc_class is not None:
                    sym_id = _make_id(file_namespace, enc_class, fname)
                else:
                    sym_id = _make_id(file_namespace, fname)
                nodes.append(
                    CodeNode(
                        id=sym_id,
                        label=fname,
                        kind="symbol",
                        source_file=str(path),
                        source_location=_source_location(node),
                        properties={
                            "signature": _signature_of(node),
                            "summary": _summary_of(node),
                        },
                    )
                )

        elif node.type in cfg.import_types:
            _emit_import(node)

        elif node.type in cfg.call_types:
            _emit_call(node)

        for child in node.children:
            _walk(child)

    def _emit_import(node) -> None:
        """Emit an imports edge from the file node to the imported module."""
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                # TypeScript ES-module import: import { X } from "./module"
                # source field is the string literal with the module path.
                raw_path = _read_text(source_node, source).strip("\"'")
                module_namespace = _module_namespace(raw_path)
                # Walk import_clause > named_imports > import_specifier
                for clause in node.children:
                    if clause.type == "import_clause":
                        for named in clause.children:
                            if named.type == "named_imports":
                                for specifier in named.children:
                                    if specifier.type == "import_specifier":
                                        name_field = specifier.child_by_field_name("name")
                                        if name_field is not None:
                                            local_name = _read_text(name_field, source)
                                            target_id = _make_id(module_namespace, local_name)
                                            named_imports[local_name] = target_id
                                            edges.append(
                                                CodeEdge(
                                                    source=file_id,
                                                    target=target_id,
                                                    relation="imports",
                                                    method=ExtractionMethod.EXTRACTED,
                                                    source_file=str(path),
                                                    source_location=_source_location(node),
                                                )
                                            )
            else:
                # Python-style bare import: import math  OR  import os, sys
                for child in node.children:
                    if child.type in ("dotted_name", "aliased_import"):
                        # dotted_name for simple imports; get first dotted_name inside aliased_import
                        actual = child
                        if child.type == "aliased_import":
                            actual = child.children[0]  # the module name part
                        module_name = _read_text(actual, source).split(".")[0]
                        target_id = _make_id(_python_module_namespace(module_name))
                        edges.append(
                            CodeEdge(
                                source=file_id,
                                target=target_id,
                                relation="imports",
                                method=ExtractionMethod.EXTRACTED,
                                source_file=str(path),
                                source_location=_source_location(node),
                            )
                        )
        elif node.type == "import_from_statement":
            # from x import y  -> module is x
            mod_node = node.child_by_field_name("module_name")
            if mod_node is not None:
                module_name = _read_text(mod_node, source).split(".")[0]
                target_id = _make_id(_python_module_namespace(module_name))
                edges.append(
                    CodeEdge(
                        source=file_id,
                        target=target_id,
                        relation="imports",
                        method=ExtractionMethod.EXTRACTED,
                        source_file=str(path),
                        source_location=_source_location(node),
                    )
                )

    def _enclosing_function_id(node) -> str | None:
        """Walk ancestors to find the nearest enclosing function and return its symbol id."""
        current = node.parent
        enc_class: str | None = None
        while current is not None:
            if current.type in cfg.function_types:
                fn_name_node = current.child_by_field_name("name")
                if fn_name_node is None:
                    return None
                fn_name = _read_text(fn_name_node, source)
                # find the class enclosing this function
                if enc_class is None:
                    # look further up for a class
                    cls = _enclosing_class(current)
                else:
                    cls = enc_class
                if cls is not None:
                    return _make_id(file_namespace, cls, fn_name)
                return _make_id(file_namespace, fn_name)
            if current.type in cfg.class_types:
                enc_class = class_node_names.get(current.id)
            current = current.parent
        return None

    def _emit_call(node) -> None:
        """Emit a calls edge from the enclosing function to the callee."""
        caller_id = _enclosing_function_id(node)
        if caller_id is None:
            return  # call not inside a function (e.g. module-level)

        fn_child = node.child_by_field_name("function")
        if fn_child is None:
            return

        if fn_child.type == "identifier":
            # bare name call: foo(...)
            callee_name = _read_text(fn_child, source)
            # Check named imports first (e.g. TypeScript: import { formatName } from "./format")
            if callee_name in named_imports:
                target_id = named_imports[callee_name]
            else:
                # best-effort: check if there's a class node with this method in same file
                enc_fn_class = _get_enclosing_class_for_function_id(caller_id)
                if enc_fn_class is not None and (
                    same_class_target := _make_id(file_namespace, enc_fn_class, callee_name)
                ) in {n.id for n in nodes}:
                    target_id = same_class_target
                else:
                    target_id = _make_id(callee_name)

        elif fn_child.type in ("attribute", "member_expression"):
            # Python attribute call: obj.method(...)
            # TypeScript member_expression call: obj.method(...)
            obj_node = fn_child.child_by_field_name("object")
            # Python uses "attribute" field; TypeScript uses "property" field
            attr_node = fn_child.child_by_field_name("attribute") or fn_child.child_by_field_name("property")
            if obj_node is None or attr_node is None:
                return
            obj_text = _read_text(obj_node, source)
            attr_text = _read_text(attr_node, source)

            if obj_text in ("self", "this"):
                # self.method / this.method -> resolve to same-class method
                enc_fn_class = _get_enclosing_class_for_function_id(caller_id)
                if enc_fn_class is not None:
                    target_id = _make_id(file_namespace, enc_fn_class, attr_text)
                else:
                    target_id = _make_id(file_namespace, attr_text)
            else:
                # external: obj.method -> _make_id(obj, method)
                target_id = _make_id(obj_text, attr_text)
        else:
            return

        edges.append(
            CodeEdge(
                source=caller_id,
                target=target_id,
                relation="calls",
                method=ExtractionMethod.EXTRACTED,
                source_file=str(path),
                source_location=_source_location(node),
            )
        )

    def _get_enclosing_class_for_function_id(fn_id: str) -> str | None:
        """Given a function symbol id (e.g. 'calc_calculator_hypot'), find its class.
        We do this by looking up the function node in our collected node list."""
        for n in nodes:
            if n.id == fn_id and n.kind == "symbol":
                # The class name sits between stem and fname in the id.
                # Simpler: search class_node_names values
                pass
        # Re-derive: parse fn_id pieces
        # fn_id = _make_id(file_namespace, class_name, fn_name) or _make_id(file_namespace, fn_name)
        # We'll search via nodes
        for n in nodes:
            if n.id == fn_id and n.kind == "symbol":
                fn_label = n.label
                # try to find a class with id = _make_id(file_namespace, class_name)
                for cls_name in class_node_names.values():
                    candidate = _make_id(file_namespace, cls_name, fn_label)
                    if candidate == fn_id:
                        return cls_name
        return None

    def _module_namespace(raw_path: str) -> str:
        """Resolve relative module paths into the same file namespace used by nodes."""
        if raw_path.startswith("."):
            return _file_namespace(path.parent / raw_path, root=root, scope=scope)
        return Path(raw_path).stem

    def _python_module_namespace(module_name: str) -> str:
        if root is None or not module_name:
            return module_name

        candidate = root / f"{module_name.replace('.', '/')}.py"
        if candidate.exists():
            return _file_namespace(candidate, root=root, scope=scope)
        package_init = root / module_name.replace(".", "/") / "__init__.py"
        if package_init.exists():
            return _file_namespace(package_init, root=root, scope=scope)
        return module_name

    try:
        _walk(tree_root)
    except Exception as exc:
        return Extraction(nodes=[], edges=[], error=str(exc))

    # Containment. Without it a file node has no edges at all, and the symbols
    # it declares are joined only by whatever calls happen to run between them —
    # so a module with no internal calls shatters into isolated records and
    # nothing can cluster by the structure a person actually navigates.
    for node in list(nodes):
        if node.id == file_id or node.kind not in ("symbol", "type"):
            continue
        edges.append(
            CodeEdge(
                source=file_id,
                target=node.id,
                relation="defines",
                method=ExtractionMethod.EXTRACTED,
                source_file=str(path),
                source_location=node.source_location,
            )
        )

    return Extraction(nodes=nodes, edges=edges)
