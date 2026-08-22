from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from tree_sitter import Language, Parser


@dataclass(frozen=True)
class LanguageConfig:
    name: str
    suffixes: tuple[str, ...]
    ts_module: str                         # e.g. "tree_sitter_python"
    ts_language_fn: str = "language"       # attr on the module returning the grammar
    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    import_types: frozenset[str] = frozenset()
    call_types: frozenset[str] = frozenset()


def load_parser(cfg: LanguageConfig) -> Parser:
    """Import the tree-sitter module and build a Parser for the given language."""
    mod = importlib.import_module(cfg.ts_module)
    grammar_fn = getattr(mod, cfg.ts_language_fn)
    return Parser(Language(grammar_fn()))


def config_for_suffix(suffix: str) -> LanguageConfig | None:
    """Return the LanguageConfig for a file suffix, or None if unsupported."""
    return LANGUAGE_REGISTRY.get(suffix)


_PYTHON_CONFIG = LanguageConfig(
    name="python",
    suffixes=(".py",),
    ts_module="tree_sitter_python",
    ts_language_fn="language",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
)

_TS_CONFIG = LanguageConfig(
    name="typescript",
    suffixes=(".ts", ".mts", ".cts"),
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_typescript",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
)

# The TSX grammar is a superset of JavaScript and handles JSX, so `.js`/`.jsx`
# need no extra dependency — only an entry here. Leaving them out meant the most
# common language in most repositories produced no code records at all.
_TSX_CONFIG = LanguageConfig(
    name="tsx",
    suffixes=(".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_tsx",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset(
        {"function_declaration", "method_definition", "generator_function_declaration"}
    ),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
)

# Build registry: suffix -> LanguageConfig
LANGUAGE_REGISTRY: dict[str, LanguageConfig] = {}
for _cfg in (_PYTHON_CONFIG, _TS_CONFIG, _TSX_CONFIG):
    for _suffix in _cfg.suffixes:
        LANGUAGE_REGISTRY[_suffix] = _cfg

CODE_SUFFIXES: frozenset[str] = frozenset(LANGUAGE_REGISTRY)
