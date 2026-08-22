"""Remembering how a bug was fixed.

The nearest thing that existed was skill memory — "a procedure I once followed."
That is not what someone needs when a familiar error comes back. What they need
is: I have seen this before, here is what it was, here is what fixed it.

A fix is extracted only from work that actually happened:

* the session asked about a failure,
* an edit succeeded,
* and the session did not end on a failing check.

Prose describing a bug never becomes a fix, for the same reason prose describing
a procedure never becomes a skill: a record of work has to be backed by work.

Verification is recorded when a check went from failing to passing across the
edits. Most real fixes have no such check, and those are still worth keeping —
they are simply marked unverified rather than dropped or overclaimed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from archivum.capture.classify import classify_session, touched_paths
from archivum.capture.schema import Conversation, ToolCall
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.repository import KnowledgeRepository

# Commands whose success or failure says something about whether the code works.
_CHECK_HINTS = re.compile(
    r"\b(pytest|jest|vitest|mocha|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test"
    r"|yarn\s+test|make\s+test|tox|rspec|phpunit|dotnet\s+test|gradle\s+test)\b",
    re.I,
)

# `SomeError: detail`, a traceback line, or an explicit failure phrase. The
# error is the part worth keeping; the rest of the sentence is scaffolding.
_ERROR_RE = re.compile(
    r"(?:^|[\s(])((?:[A-Z][A-Za-z0-9_]*)?(?:Error|Exception|Warning)\b[^.\n]{0,160})"
)
_FAILURE_PHRASE_RE = re.compile(
    r"\b((?:[\w\s]{0,40})?(?:crash(?:es|ed)?|fails?|failing|broken|breaks?"
    r"|not working|doesn'?t work|hangs?|times? out|returns? nothing)[^.\n]{0,120})",
    re.I,
)

_MAX_SYMPTOM = 240

# Values inside an error message vary between occurrences of the same trouble;
# the shape is what should match.
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUMBER_RE = re.compile(r"\b\d+\b")
_PATHISH_RE = re.compile(r"(?:/[\w.\-]+)+")


@dataclass(frozen=True)
class Fix:
    """One remembered repair."""

    symptom: str
    diagnosis: str
    changed_paths: list[str] = field(default_factory=list)
    verified_by: str = ""

    @property
    def key(self) -> str:
        return symptom_key(self.symptom)


def symptom_key(symptom: str) -> str:
    """A stable key for "the same shape of trouble".

    `KeyError: 'slug'` and `KeyError: 'title'` are one problem seen twice, so
    quoted values, numbers and paths are dropped before comparing. Without that,
    every occurrence would look novel and memory would never recognise anything.
    """
    text = _QUOTED_RE.sub("", symptom)
    text = _PATHISH_RE.sub("", text)
    text = _NUMBER_RE.sub("", text)
    return " ".join(text.lower().split())


def _first_request(conversation: Conversation) -> str:
    for turn in conversation.turns:
        if turn.role == "user" and turn.text.strip():
            return turn.text.strip()
    return ""


def _symptom_of(request: str) -> str:
    """The failure being reported, rather than the whole message around it."""
    error = _ERROR_RE.search(request)
    if error:
        return " ".join(error.group(1).split())[:_MAX_SYMPTOM]
    phrase = _FAILURE_PHRASE_RE.search(request)
    if phrase:
        return " ".join(phrase.group(1).split())[:_MAX_SYMPTOM]
    return " ".join(request.split())[:_MAX_SYMPTOM]


def _diagnosis_of(conversation: Conversation) -> str:
    """What the session said while it was working.

    The last assistant turn that carried an edit is the closest thing to a
    stated cause. It is quoted, never summarised, so the record cannot claim
    more than was actually written.
    """
    latest = ""
    for turn in conversation.turns:
        if turn.role != "assistant" or not turn.text.strip():
            continue
        if any(call.name in ("Edit", "Write", "MultiEdit") for call in turn.tool_calls):
            latest = " ".join(turn.text.split())
    return latest[:_MAX_SYMPTOM]


def _checks(conversation: Conversation) -> list[ToolCall]:
    return [
        call
        for turn in conversation.turns
        for call in turn.tool_calls
        if _CHECK_HINTS.search(str(call.arguments.get("command", "")))
    ]


def _verification(conversation: Conversation) -> tuple[str, bool]:
    """(command, ended_failing) for the checks this session ran.

    A check that went from failing to passing is what verifies a fix. A session
    whose last check still fails did not fix anything, whatever else it did.
    """
    checks = _checks(conversation)
    if not checks:
        return "", False
    last = checks[-1]
    command = str(last.arguments.get("command", "")).strip()
    if not last.ok:
        return command, True
    # Only call it verified when something actually failed first; a check that
    # passed all along verifies nothing about the change.
    if any(not call.ok for call in checks[:-1]):
        return command, False
    return "", False


def extract_fix(conversation: Conversation) -> Fix | None:
    """The fix this session performed, or None if it did not perform one."""
    if classify_session(conversation) != "bugfix":
        return None

    changed = touched_paths(conversation)
    if not changed:
        # Investigating a bug is not fixing it.
        return None

    verified_by, ended_failing = _verification(conversation)
    if ended_failing:
        return None

    request = _first_request(conversation)
    if not request:
        return None

    return Fix(
        symptom=_symptom_of(request),
        diagnosis=_diagnosis_of(conversation),
        changed_paths=changed,
        verified_by=verified_by,
    )


# ── Storing and recalling ─────────────────────────────────────────────────

# Two symptoms match when they share enough of their shape. Set low enough that
# a rephrased error still lands, high enough that "Error" alone matches nothing.
_MATCH_THRESHOLD = 0.5
_MAX_RECALLED = 5


def fix_id_for(source_id: str) -> str:
    return f"fix:{source_id}"


def _tokens(text: str) -> set[str]:
    return {token for token in symptom_key(text).split() if len(token) > 2}


def match_score(left: str, right: str) -> float:
    """How much two symptoms share, as a fraction of the smaller one.

    Comparing against the smaller side means a terse error still matches the
    verbose report it was first seen in, which is the direction that matters:
    you paste the error, memory has the story.
    """
    first, second = _tokens(left), _tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def fix_to_object(fix: Fix, *, source_id: str, wiki_id: str) -> KnowledgeObject:
    """The canonical record for one remembered repair."""
    citation = Citation(
        source_id=source_id,
        chunk_id=source_id,
        span_start=None,
        span_end=None,
        quote=fix.symptom,
    )
    return KnowledgeObject(
        id=fix_id_for(source_id),
        kind="fix",
        label=fix.symptom,
        scope=f"wiki:{wiki_id}",
        # A verified fix is worth more than an unverified one, and the record
        # should say so rather than presenting both as equally certain.
        confidence=0.9 if fix.verified_by else 0.6,
        extraction_method="EXTRACTED",
        citations=[citation],
        properties={
            "symptom": fix.symptom,
            "symptom_key": fix.key,
            "diagnosis": fix.diagnosis,
            "changed_paths": fix.changed_paths,
            "verified_by": fix.verified_by,
        },
    )


async def recall_fixes(
    repo: KnowledgeRepository,
    *,
    symptom: str,
    wiki_id: str,
    limit: int = _MAX_RECALLED,
) -> list[KnowledgeObject]:
    """Fixes for trouble that looks like this one, best match first."""
    if not symptom.strip():
        return []
    candidates = await repo.list_objects(scope=f"wiki:{wiki_id}", limit=10_000)
    scored = [
        (match_score(symptom, str(obj.properties.get("symptom", ""))), obj)
        for obj in candidates
        if obj.kind == "fix"
    ]
    matches = [
        (score, obj) for score, obj in scored if score >= _MATCH_THRESHOLD
    ]
    # Best match first; ties broken on id so the same store answers the same way.
    matches.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [obj for _, obj in matches[:limit]]
