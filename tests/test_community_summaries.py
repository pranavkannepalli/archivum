"""Global questions need summaries, not just retrieval.

"What have I been thinking about this month?" cannot be answered by matching
words: no single record contains the answer. Real GraphRAG summarises each
community once, then reasons over the summaries. Archivum had the communities
and never summarised them, so it answered local questions well and global ones
not at all.

Summaries are the one place a model writes prose that is later read as fact, so
they cite the records they came from and carry the date they were written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema
from archivum.summaries import summarise_communities, summary_id_for


def _obj(id_: str, label: str) -> KnowledgeObject:
    return KnowledgeObject(
        id=id_,
        kind="page",
        label=label,
        scope="wiki:default",
        confidence=1.0,
        extraction_method="USER_AUTHORED",
        citations=[Citation(source_id=id_, chunk_id=id_, span_start=None, span_end=None, quote=label)],
        properties={},
    )


def _rel(src: str, dst: str) -> KnowledgeRelationship:
    return KnowledgeRelationship(
        id=f"{src}->{dst}",
        src_id=src,
        dst_id=dst,
        rel_type="references",
        scope="wiki:default",
        confidence=1.0,
        extraction_method="USER_AUTHORED",
        citations=[Citation(source_id=src, chunk_id=src, span_start=None, span_end=None, quote="x")],
        properties={},
    )


async def _seed(conn) -> KnowledgeRepository:
    repo = KnowledgeRepository(conn)
    for id_, label in [
        ("a", "Retrieval design"),
        ("b", "Chunking strategy"),
        ("c", "Embedding choice"),
    ]:
        await repo.upsert_object(_obj(id_, label))
    await repo.upsert_relationship(_rel("a", "b"))
    await repo.upsert_relationship(_rel("b", "c"))
    return repo


async def test_a_community_gets_a_summary_that_cites_its_members():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = await _seed(conn)

        with patch(
            "archivum.summaries.cli_chat_completion",
            new=AsyncMock(return_value="Work on how retrieval finds things."),
        ):
            written = await summarise_communities(repo, wiki_id="default", provider="claude_cli")

        assert written, "a connected cluster should be summarised"
        stored = await repo.get_object(written[0])

    assert stored is not None
    assert stored.kind == "community_summary"
    assert "retrieval" in stored.properties["summary"].lower()
    assert stored.citations, "a summary is prose read as fact; it must cite"


async def test_the_summary_prompt_carries_the_date_and_the_members():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = await _seed(conn)

        with patch(
            "archivum.summaries.cli_chat_completion",
            new=AsyncMock(return_value="Summary."),
        ) as called:
            await summarise_communities(repo, wiki_id="default", provider="claude_cli")

        prompt = called.await_args.kwargs["prompt"]

    assert datetime.now(UTC).strftime("%Y-%m-%d") in prompt
    assert "Retrieval design" in prompt
    assert "relative" in prompt.lower(), "relative dates must be anchored"


async def test_a_summary_records_when_it_was_written():
    """A stale summary should be recognisable as stale."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = await _seed(conn)

        with patch(
            "archivum.summaries.cli_chat_completion", new=AsyncMock(return_value="Summary.")
        ):
            written = await summarise_communities(repo, wiki_id="default", provider="claude_cli")
        stored = await repo.get_object(written[0])

    assert stored.properties["written_at"].startswith(datetime.now(UTC).strftime("%Y-%m-%d"))
    assert stored.properties["member_count"] == 3


async def test_a_model_that_fails_leaves_no_summary_rather_than_a_bad_one():
    """An invented summary is worse than none: it is read as fact afterwards."""
    from archivum.llm.cli_client import CliModelError

    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = await _seed(conn)

        with patch(
            "archivum.summaries.cli_chat_completion",
            new=AsyncMock(side_effect=CliModelError("not installed")),
        ):
            written = await summarise_communities(repo, wiki_id="default", provider="claude_cli")

        assert written == []
        assert await repo.get_object(summary_id_for("wiki:default", "a")) is None


async def test_a_lone_record_is_not_a_community_worth_summarising():
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = KnowledgeRepository(conn)
        await repo.upsert_object(_obj("solo", "By itself"))

        with patch(
            "archivum.summaries.cli_chat_completion", new=AsyncMock(return_value="Summary.")
        ) as called:
            written = await summarise_communities(repo, wiki_id="default", provider="claude_cli")

    assert written == []
    called.assert_not_awaited()


async def test_a_summary_is_joined_to_what_it_summarises():
    """A summary that names its members only in a property is an orphan.

    `member_ids` sat in properties and the citations named the members, but no
    edge was ever written — so every community summary was disconnected from
    the graph it describes, unreachable from the owner and invisible to any
    traversal.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await init_knowledge_schema(conn)
        repo = await _seed(conn)

        with patch(
            "archivum.summaries.cli_chat_completion",
            new=AsyncMock(return_value="Work on how retrieval finds things."),
        ):
            written = await summarise_communities(repo, wiki_id="default", provider="claude_cli")

        relationships = await repo.list_relationships(node_id=written[0])

    summarised = {rel.dst_id for rel in relationships if rel.rel_type == "summarises"}
    assert summarised, "the summary must be joined to the records it was written from"
    assert summarised <= {"a", "b", "c"}
