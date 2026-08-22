"""Tasks as lines in pages.

There used to be a `life_tasks` table, and it was deleted for a good reason: the
tool wrote to a store no screen ever read, so there were two models of the same
noun and only one of them was visible.

A task here is what it looks like — a checkbox line in a markdown page. The file
stays the source of truth, so you can write one in vim, on your phone through a
sync folder, or in Archivum, and checking it off edits that line rather than
updating a shadow record that can disagree with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from archivum.config import Settings
from archivum.db import sqlite
from archivum.indexing import page_path, reindex_page

# `- [ ] text`, `* [x] text`, indented or not. The brackets must open the item,
# so a sentence that merely contains "[ ]" is prose, not a task.
_TASK_RE = re.compile(r"^(?P<indent>\s*)(?P<bullet>[-*+])\s+\[(?P<mark>[ xX])\]\s+(?P<text>.*\S)\s*$")

# Pages Archivum generated. A todo list is something you keep, not something the
# code graph writes for you.
_GENERATED_PREFIXES = ("code/", "memory/", "skills/")


@dataclass(frozen=True)
class Task:
    text: str
    done: bool
    line: int
    slug: str = ""
    page_title: str = ""


def parse_tasks(markdown: str, *, slug: str = "", page_title: str = "") -> list[Task]:
    """Every checkbox line in a page, with the line it sits on.

    The line number is what makes a task addressable: it is how checking one off
    finds the line to rewrite, without needing the text to be unique.
    """
    tasks: list[Task] = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        match = _TASK_RE.match(line)
        if match is None:
            continue
        tasks.append(
            Task(
                text=match.group("text").strip(),
                done=match.group("mark").lower() == "x",
                line=number,
                slug=slug,
                page_title=page_title,
            )
        )
    return tasks


def _is_generated(slug: str) -> bool:
    return slug.startswith(_GENERATED_PREFIXES)


async def list_open_tasks(*, wiki_id: str, limit: int = 200) -> list[Task]:
    """Everything still unchecked across the vault, newest page first."""
    open_tasks: list[Task] = []
    for row in await sqlite.list_pages(wiki_id):
        slug = row["slug"]
        if _is_generated(slug):
            continue
        page = await sqlite.get_page(slug, wiki_id)
        if page is None:
            continue
        for task in parse_tasks(
            page["content"] or "", slug=slug, page_title=page["title"] or slug
        ):
            if not task.done:
                open_tasks.append(task)
            if len(open_tasks) >= limit:
                return open_tasks
    return open_tasks


async def set_task_done(
    *, slug: str, line: int, done: bool, wiki_id: str, settings: Settings
) -> Task:
    """Check or uncheck one task, by rewriting its line in the file.

    Writing the file and reindexing — rather than updating a row — is what keeps
    a task the same object whether you ticked it here or in your editor.
    """
    path = page_path(settings, slug)
    if not path.is_file():
        raise ValueError(f"Page '{slug}' has no file")

    lines = path.read_text(encoding="utf-8").splitlines()
    if not 1 <= line <= len(lines):
        raise ValueError(f"Line {line} is outside '{slug}'")

    match = _TASK_RE.match(lines[line - 1])
    if match is None:
        raise ValueError(f"Line {line} of '{slug}' is not a task")

    mark = "x" if done else " "
    lines[line - 1] = (
        f"{match.group('indent')}{match.group('bullet')} [{mark}] {match.group('text')}"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Same indexing path as any other edit. No distillation: ticking a box is
    # not new thinking to remember.
    await reindex_page(
        slug,
        wiki_id=wiki_id,
        settings=settings,
        force=True,
        authored_by="user",
        reason="task-toggle",
        distill=False,
    )
    page = await sqlite.get_page(slug, wiki_id)
    return Task(
        text=match.group("text").strip(),
        done=done,
        line=line,
        slug=slug,
        page_title=(page or {}).get("title", slug),
    )
