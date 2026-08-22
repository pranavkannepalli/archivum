"""Remembering how a bug was fixed, not just that the code changed.

The closest thing that existed was skill memory: "a procedure I once followed."
That is not what a developer needs at 2am. What they need is: I have seen this
error before — here is what caused it and what fixed it.

A fix is only recorded from work that actually happened: a session asking about
a failure, edits that succeeded, and ideally a verification that went from
failing to passing. Prose describing a bug never becomes a fix.
"""

from __future__ import annotations

from archivum.capture.schema import Conversation, ToolCall, Turn
from archivum.fixes import extract_fix, symptom_key


def _session(
    request: str,
    *,
    tools: tuple[ToolCall, ...] = (),
    assistant: str = "Found it.",
) -> Conversation:
    return Conversation(
        session_id="s1",
        interface="claude_code_import",
        started_at="2026-08-20T10:00:00Z",
        turns=(
            Turn(role="user", text=request),
            Turn(role="assistant", text=assistant, tool_calls=tools),
        ),
    )


_EDIT = ToolCall(name="Edit", arguments={"file_path": "/src/geo.py"}, result="ok")


def test_a_failing_then_passing_check_is_a_verified_fix():
    session = _session(
        "haversine raises TypeError: unsupported operand type(s) for +",
        tools=(
            ToolCall(name="Bash", arguments={"command": "pytest tests/"}, result="1 failed", ok=False),
            _EDIT,
            ToolCall(name="Bash", arguments={"command": "pytest tests/"}, result="3 passed", ok=True),
        ),
    )

    fix = extract_fix(session)

    assert fix is not None
    assert "TypeError" in fix.symptom
    assert fix.changed_paths == ["/src/geo.py"]
    assert fix.verified_by == "pytest tests/"


def test_a_fix_without_a_check_is_recorded_but_unverified():
    """Most fixes are not accompanied by a test run. They are still worth keeping."""
    session = _session(
        "the login redirect is broken and throws",
        tools=(_EDIT,),
    )

    fix = extract_fix(session)

    assert fix is not None
    assert fix.verified_by == ""


def test_a_session_that_changed_nothing_is_not_a_fix():
    """Investigating a bug is not fixing it."""
    session = _session(
        "why does haversine raise TypeError?",
        tools=(ToolCall(name="Read", arguments={"file_path": "/src/geo.py"}, result="..."),),
    )

    assert extract_fix(session) is None


def test_a_feature_session_is_not_a_fix():
    assert extract_fix(_session("Add a settings page", tools=(_EDIT,))) is None


def test_a_session_that_ended_failing_is_not_a_fix():
    """A fix that did not work is not a fix, however much was tried."""
    session = _session(
        "it crashes with ValueError",
        tools=(
            _EDIT,
            ToolCall(name="Bash", arguments={"command": "pytest"}, result="2 failed", ok=False),
        ),
    )

    assert extract_fix(session) is None


def test_the_symptom_keeps_the_error_rather_than_the_whole_request():
    session = _session(
        "Hey, when I save a page it blows up with KeyError: 'slug' — can you look?",
        tools=(_EDIT,),
    )

    fix = extract_fix(session)

    assert fix is not None
    assert "KeyError" in fix.symptom


# ── Recognising the same trouble twice ────────────────────────────────────


def test_the_same_error_produces_the_same_key():
    assert symptom_key("TypeError: unsupported operand type(s) for +: 'int' and 'str'") == symptom_key(
        "TypeError: unsupported operand type(s) for +: 'int' and 'str'"
    )


def test_the_key_ignores_the_specific_values_in_an_error():
    """`KeyError: 'slug'` and `KeyError: 'title'` are the same shape of trouble."""
    assert symptom_key("KeyError: 'slug'") == symptom_key("KeyError: 'title'")


def test_different_errors_do_not_collide():
    assert symptom_key("KeyError: 'slug'") != symptom_key("TypeError: bad operand")
