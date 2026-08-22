"""Every prompt has to say when it is being written.

A model with no date reasons about "recently", "last month" and "current" from
whenever its training stopped. For a vault whose whole job is remembering *when*
you thought something, that is not a rounding error — a community summary that
says "recent work on X" while meaning a year ago is worse than no summary.

So there is one place that builds the situational preamble, and everything that
talks to a model uses it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from archivum.llm.prompt_context import prompt_preamble, with_context


def test_the_preamble_states_today_s_date():
    preamble = prompt_preamble(now=datetime(2026, 8, 20, 14, 30, tzinfo=UTC))
    assert "2026-08-20" in preamble


def test_the_preamble_names_the_day_of_the_week():
    """"Last Thursday" is only resolvable if the model knows what today is."""
    preamble = prompt_preamble(now=datetime(2026, 8, 20, tzinfo=UTC))
    assert "Thursday" in preamble


def test_the_preamble_says_whose_vault_it_is():
    preamble = prompt_preamble(now=datetime(2026, 8, 20, tzinfo=UTC), owner="Pranav")
    assert "Pranav" in preamble


def test_an_unnamed_owner_is_not_invented():
    """No owner line at all, rather than a line saying the owner is unknown."""
    preamble = prompt_preamble(now=datetime(2026, 8, 20, tzinfo=UTC))
    assert "This vault belongs to" not in preamble
    assert "None" not in preamble


def test_the_preamble_tells_the_model_to_date_from_the_material():
    """Relative dates in a note are relative to the note, not to today."""
    preamble = prompt_preamble(now=datetime(2026, 8, 20, tzinfo=UTC))
    assert "relative" in preamble.lower()


def test_with_context_puts_the_preamble_before_the_instruction():
    combined = with_context(
        "Do the thing.", now=datetime(2026, 8, 20, tzinfo=UTC), owner="Pranav"
    )
    assert combined.index("2026-08-20") < combined.index("Do the thing.")


def test_with_context_leaves_the_instruction_intact():
    combined = with_context("Do the thing.", now=datetime(2026, 8, 20, tzinfo=UTC))
    assert "Do the thing." in combined


def test_extra_facts_are_included_when_given():
    combined = with_context(
        "Summarise.",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        extra={"Repository": "archivum", "Records": "412"},
    )
    assert "Repository: archivum" in combined
    assert "Records: 412" in combined


def test_the_preamble_defaults_to_now_without_being_asked():
    """A caller that forgets the date should still get one."""
    assert str(datetime.now(UTC).year) in prompt_preamble()
