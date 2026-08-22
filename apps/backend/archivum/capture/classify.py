"""What kind of work a session was, and which files it changed.

A captured transcript is undifferentiated text. Two cheap, deterministic reads
turn it into something the rest of memory can act on:

* **Kind** — a bug fix is worth mining for a fix record; a refactor is not. The
  kind comes from the request that opened the session, because that is where a
  person says what they wanted.
* **Touched paths** — which files the work actually changed, from the tool calls
  it made. This is what connects a session to the code it altered.

Both are rules over what the session did, not a model's opinion about it, so the
same transcript always classifies the same way and nothing here needs an API key.
A request that matches nothing stays `unknown`: an invented category is worse
than an absent one, because everything downstream would treat it as a fact.
"""

from __future__ import annotations

import re
from typing import Literal

from archivum.capture.schema import Conversation, ToolCall

SessionKind = Literal["bugfix", "feature", "refactor", "investigation", "unknown"]

# Tools that change a file. A file an agent *read* is not a file the work
# changed, and counting reads would attribute every session to everything it
# looked at on the way.
_MUTATING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit", "str_replace_editor"})

_PATH_ARGUMENTS = ("file_path", "path", "notebook_path", "filename")

# Ordered: the first match wins, so "fix the crash in the new feature" reads as
# a bug fix rather than a feature. A reported failure is the stronger signal.
_RULES: tuple[tuple[SessionKind, re.Pattern[str]], ...] = (
    (
        "bugfix",
        re.compile(
            r"\b(bug|broken|breaks?|breaking|crash(?:e[sd])?|fail(?:s|ed|ing|ure)?"
            r"|error|exception|traceback|regress(?:ion|ed)?|throw(?:s|ing|n)?"
            r"|not working|doesn'?t work|wrong)\b"
            r"|\bfix(?:es|ed|ing)?\b"
            r"|\b\w+Error\b",
            re.I,
        ),
    ),
    (
        "refactor",
        re.compile(
            r"\b(refactor(?:s|ed|ing)?|clean\s*up|tidy|simplif(?:y|ies|ied)"
            r"|rename|extract|deduplicate|restructure|reorganise|reorganize)\b",
            re.I,
        ),
    ),
    (
        "feature",
        re.compile(
            r"\b(add|build|implement|create|introduce|support|write)\b",
            re.I,
        ),
    ),
    (
        "investigation",
        re.compile(
            r"^\s*(how|what|why|where|when|which|who|can|does|is|are|should)\b"
            r"|\b(investigate|explain|understand|look into|review|audit|explore)\b"
            r"|\?\s*$",
            re.I,
        ),
    ),
)


def _first_request(conversation: Conversation) -> str:
    for turn in conversation.turns:
        if turn.role == "user" and turn.text.strip():
            return turn.text.strip()
    return ""


def classify_session(conversation: Conversation) -> SessionKind:
    """What kind of work this session was, from the request that opened it."""
    request = _first_request(conversation)
    if not request:
        return "unknown"
    for kind, pattern in _RULES:
        if pattern.search(request):
            return kind
    return "unknown"


def _path_of(call: ToolCall) -> str:
    for key in _PATH_ARGUMENTS:
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def touched_paths(conversation: Conversation) -> list[str]:
    """The files this session actually changed, sorted and deduplicated.

    A failed call changed nothing, so it is not counted — attributing work to a
    file an edit failed against would be a claim the session never made.
    """
    paths: set[str] = set()
    for turn in conversation.turns:
        for call in turn.tool_calls:
            if call.name not in _MUTATING_TOOLS or not call.ok:
                continue
            path = _path_of(call)
            if path:
                paths.add(path)
    return sorted(paths)
