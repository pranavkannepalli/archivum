"""Reading plain prose out of a markdown page.

Three places needed the same thing — the memory catalogue summarising a page,
search building an excerpt, and indexing deciding what to embed — and each had
grown its own version. They are one implementation here, because "the first
line of this page that means something" should not have three answers.
"""

from __future__ import annotations

import re

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
_LIST_MARKER_RE = re.compile(r"^\s*[-*+]\s+(\[[ xX]\]\s+)?")
_ORDERED_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# Lines made only of these are rules, underlines and spacing.
_STRUCTURE_ONLY = set("-*_=# ")

DEFAULT_LEDE_CHARS = 200


def strip_frontmatter(markdown: str | None) -> str:
    """The page without its YAML header."""
    return _FRONTMATTER_RE.sub("", markdown or "", count=1)


def lede(markdown: str | None, *, limit: int = DEFAULT_LEDE_CHARS) -> str:
    """The first line of a page a person would recognise it by.

    Skips frontmatter, headings, code fences and rules — the parts every page
    has in common, which describe markdown rather than this page. Returns "" if
    there is nothing but structure, so callers can fall back deliberately
    instead of rendering an empty line.
    """
    in_fence = False
    for raw in strip_frontmatter(markdown).splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("#") or line.startswith(">"):
            continue
        if set(line) <= _STRUCTURE_ONLY:
            continue
        # Read as prose: the markers that make it a list or a link are noise.
        line = _LIST_MARKER_RE.sub("", line)
        line = _ORDERED_MARKER_RE.sub("", line)
        line = _WIKILINK_RE.sub(lambda match: match.group(2) or match.group(1), line)
        line = _LINK_RE.sub(r"\1", line)
        line = line.replace("**", "").replace("`", "").strip()
        if not line:
            continue
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""
