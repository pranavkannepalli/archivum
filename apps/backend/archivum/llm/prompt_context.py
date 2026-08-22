"""The situational preamble every prompt carries.

A model with no date reasons about "recently", "currently" and "last month"
from wherever its training stopped. For a vault whose entire job is remembering
*when* you thought something, that is not a rounding error: a community summary
that says "recent work on retrieval" while meaning a year ago is worse than no
summary, because it reads as a fact.

One builder, used by everything that talks to a model, so a new prompt cannot
quietly forget. Facts only — the date, the day, whose vault this is, and
whatever the caller knows about the material. No instructions about tone, no
persona; those belong to the prompt itself.
"""

from __future__ import annotations

from datetime import UTC, datetime


def prompt_preamble(
    *,
    now: datetime | None = None,
    owner: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """Facts the model needs before it can read anything correctly."""
    moment = now or datetime.now(UTC)
    lines = [
        "## Context for this request",
        "",
        f"Today is {moment.strftime('%A, %d %B %Y')} ({moment.strftime('%Y-%m-%d')}).",
        f"The current time is {moment.strftime('%H:%M %Z') or moment.strftime('%H:%M')}.",
    ]
    if owner and owner.strip():
        lines.append(f"This vault belongs to {owner.strip()}.")
    for key, value in (extra or {}).items():
        if value:
            lines.append(f"{key}: {value}")
    lines += [
        "",
        # The most common way a dated model gets this wrong: it reads "last
        # week" in a two-year-old note and resolves it against today.
        "Dates written relative to something — 'last week', 'yesterday', 'next "
        "sprint' — are relative to the material you are reading, not to today. "
        "Resolve them against the material's own date when it has one, and say "
        "the date is unknown rather than guessing.",
        "",
    ]
    return "\n".join(lines)


def with_context(
    instruction: str,
    *,
    now: datetime | None = None,
    owner: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """`instruction`, preceded by the facts needed to follow it correctly."""
    return f"{prompt_preamble(now=now, owner=owner, extra=extra)}\n{instruction}"
