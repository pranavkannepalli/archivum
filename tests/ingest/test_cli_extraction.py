"""Ingest has to be able to run on a subscription, not only on API tokens.

Extraction is the heaviest model user in the system — every file you drop goes
through it — and it dispatched only to paid API providers. The CLI providers
existed and were wired to answer synthesis alone, so the cheap half of the
pipeline was the half that barely runs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from archivum.ingest.agent import WikiAgent
from archivum.ingest.parsers import ParsedDoc

_RESPONSE = json.dumps(
    {
        "pages": [
            {"slug": "jane-doe", "title": "Jane Doe", "content": "# Jane Doe", "tags": ["person"]}
        ],
        "entities": [{"name": "Jane Doe", "type": "person"}],
        "relationships": [],
    }
)


def _settings(provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        llm_extraction_provider=provider,
        llm_model="test-model",
        anthropic_api_key="",
        owner_username="Pranav",
    )


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
async def test_extraction_can_run_through_a_cli_subscription(provider):
    doc = ParsedDoc(text="Jane Doe built Archivum.", source="notes.md", metadata={})
    agent = WikiAgent(_settings(provider))

    with patch(
        "archivum.ingest.agent.cli_chat_completion", new=AsyncMock(return_value=_RESPONSE)
    ) as called:
        result = await agent.extract(doc)

    assert [page.slug for page in result.pages] == ["jane-doe"]
    assert called.await_args.kwargs["provider"] == provider


async def test_the_extraction_prompt_carries_today_s_date():
    """Without it the model dates 'recent' from whenever training stopped."""
    doc = ParsedDoc(text="Jane Doe built Archivum.", source="notes.md", metadata={})
    agent = WikiAgent(_settings("claude_cli"))

    with patch(
        "archivum.ingest.agent.cli_chat_completion", new=AsyncMock(return_value=_RESPONSE)
    ) as called:
        await agent.extract(doc)

    from datetime import UTC, datetime

    prompt = called.await_args.kwargs["prompt"]
    assert datetime.now(UTC).strftime("%Y-%m-%d") in prompt
    assert "Jane Doe built Archivum." in prompt


async def test_a_cli_answer_wrapped_in_prose_is_still_read():
    """CLIs narrate. The JSON is in there; the wrapper is not a failure."""
    doc = ParsedDoc(text="Jane Doe built Archivum.", source="notes.md", metadata={})
    agent = WikiAgent(_settings("claude_cli"))
    noisy = f"Sure, here you go:\n\n```json\n{_RESPONSE}\n```\n\nLet me know."

    with patch(
        "archivum.ingest.agent.cli_chat_completion", new=AsyncMock(return_value=noisy)
    ):
        result = await agent.extract(doc)

    assert [page.slug for page in result.pages] == ["jane-doe"]


async def test_a_cli_that_returns_nothing_usable_falls_back_rather_than_failing():
    """A dropped file should still land, even if the model was no help."""
    doc = ParsedDoc(text="Jane Doe built Archivum.", source="notes.md", metadata={})
    agent = WikiAgent(_settings("claude_cli"))

    with patch(
        "archivum.ingest.agent.cli_chat_completion", new=AsyncMock(return_value="I cannot help.")
    ):
        result = await agent.extract(doc)

    assert result.pages, "ingest must still produce a page"
