"""Literal text search over the vault.

Three different questions were all going through one box, and only one of them
was being answered:

- **Ask** sends a question to a model and gets a written answer with citations.
- **Search** is semantic: it finds pages that are *about* something, and now
  deliberately returns nothing rather than a page of near-misses.
- **Find** is this. Literal, instant, index-backed. You already know the words;
  you want the file, or the line.

The third had no route. SQLite has carried an FTS5 index over title, content and
tags — kept current by triggers — since the beginning, but it was only ever used
as one channel inside the hybrid ranker, where a relevance floor can now discard
it. Text that is on the page should always be findable.
"""

from __future__ import annotations

import re

from archivum.db import sqlite
from archivum.markdown_text import strip_frontmatter

# FTS5 reads bare punctuation as syntax: `deploy?`, `local-first` and `C++` are
# all ordinary things to type and all three are errors. Quoting each token turns
# every one of them into a literal.
_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)

SNIPPET_CHARS = 240


def fts_query(text: str) -> str:
    """Translate what someone typed into something FTS5 will accept.

    Words are ANDed — narrowing as you type is what people expect — and the last
    one matches as a prefix, because the word you are in the middle of is not
    finished yet.
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return ""
    quoted = [f'"{token}"' for token in tokens[:-1]]
    # Prefix search only makes sense on a token FTS can index.
    last = tokens[-1]
    quoted.append(f'"{last}"*')
    return " AND ".join(quoted)


def _matching_line(content: str, tokens: list[str]) -> str:
    """The first line containing any of the words, for context."""
    lowered = [token.lower() for token in tokens]
    for raw in strip_frontmatter(content).splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        haystack = line.lower()
        if any(token in haystack for token in lowered):
            return line if len(line) <= SNIPPET_CHARS else line[: SNIPPET_CHARS - 1] + "…"
    return ""


async def find_pages(
    text: str, *, wiki_id: str = "default", limit: int = 30
) -> list[dict]:
    """Pages whose name or text contains what was typed. Empty means empty."""
    query = fts_query(text)
    if not query:
        return []

    try:
        rows = await sqlite.search_pages_fts(query, wiki_id=wiki_id, limit=limit)
    except Exception:
        # A query FTS still refuses is a miss, not a failure the user should see.
        return []

    tokens = _TOKEN_RE.findall(text)
    results: list[dict] = []
    for row in rows:
        page = await sqlite.get_page(row["slug"], wiki_id)
        content = (page or {}).get("content", "")
        results.append(
            {
                "slug": row["slug"],
                "title": row.get("title") or row["slug"],
                # The FTS snippet marks the term but is cut to a token window;
                # the line it came from reads better and locates it on the page.
                "excerpt": _matching_line(content, tokens) or "",
                "in_title": any(
                    token.lower() in (row.get("title") or "").lower() for token in tokens
                ),
            }
        )
    return results
