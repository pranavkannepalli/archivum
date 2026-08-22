"""What kind of work a session was, and what code it touched.

A captured transcript is undifferentiated text. Knowing a session was a bug fix
rather than a refactor is what lets fix memory find the sessions worth mining;
knowing which files it edited is what connects the work to the code it changed.

Both are read from what the session actually did — the request that opened it
and the tool calls it made — not from a model's opinion about it.
"""

from __future__ import annotations

from archivum.capture.classify import classify_session, touched_paths
from archivum.capture.schema import Conversation, ToolCall, Turn


def _session(user: str, *, tools: tuple[ToolCall, ...] = ()) -> Conversation:
    return Conversation(
        session_id="s1",
        interface="claude_code_import",
        started_at="2026-08-20T10:00:00Z",
        turns=(
            Turn(role="user", text=user),
            Turn(role="assistant", text="ok", tool_calls=tools),
        ),
    )


# ── What kind of work was this? ───────────────────────────────────────────


def test_a_reported_failure_reads_as_a_bug_fix():
    assert classify_session(_session("This is throwing a TypeError on save")) == "bugfix"
    assert classify_session(_session("fix the broken login redirect")) == "bugfix"


def test_a_request_to_build_reads_as_a_feature():
    assert classify_session(_session("Add a settings page for repositories")) == "feature"


def test_a_request_to_tidy_reads_as_a_refactor():
    assert classify_session(_session("Refactor the ingest pipeline, no behaviour change")) == "refactor"


def test_a_question_reads_as_an_investigation():
    assert classify_session(_session("How does the distillation queue work?")) == "investigation"


def test_an_unrecognisable_request_is_not_forced_into_a_category():
    """Guessing a kind is worse than admitting there isn't one."""
    assert classify_session(_session("hey")) == "unknown"


def test_a_session_with_no_request_is_unknown():
    conversation = Conversation(
        session_id="s", interface="x", started_at="", turns=()
    )
    assert classify_session(conversation) == "unknown"


# ── What code did it touch? ───────────────────────────────────────────────


def test_edited_files_are_recognised_as_touched():
    session = _session(
        "fix the thing",
        tools=(
            ToolCall(name="Edit", arguments={"file_path": "/src/atlas/geo.py"}, result="ok"),
            ToolCall(name="Write", arguments={"file_path": "/src/atlas/retry.py"}, result="ok"),
        ),
    )
    assert touched_paths(session) == ["/src/atlas/geo.py", "/src/atlas/retry.py"]


def test_reading_a_file_is_not_touching_it():
    """A file an agent looked at is not a file the work changed."""
    session = _session(
        "look into it",
        tools=(ToolCall(name="Read", arguments={"file_path": "/src/atlas/geo.py"}, result="..."),),
    )
    assert touched_paths(session) == []


def test_a_failed_edit_did_not_touch_anything():
    session = _session(
        "fix it",
        tools=(
            ToolCall(
                name="Edit",
                arguments={"file_path": "/src/atlas/geo.py"},
                result="error",
                ok=False,
            ),
        ),
    )
    assert touched_paths(session) == []


def test_touched_paths_are_deduplicated_and_ordered():
    session = _session(
        "fix it",
        tools=(
            ToolCall(name="Edit", arguments={"file_path": "/b.py"}, result="ok"),
            ToolCall(name="Edit", arguments={"file_path": "/a.py"}, result="ok"),
            ToolCall(name="Edit", arguments={"file_path": "/b.py"}, result="ok"),
        ),
    )
    assert touched_paths(session) == ["/a.py", "/b.py"]
