"""Summarise each community once, so global questions become answerable.

Retrieval answers local questions well: it finds the records that match. It
cannot answer "what have I been thinking about this month?", because no single
record contains that answer — it is a property of the whole cluster.

This is the half of GraphRAG that was missing. Communities were already detected
deterministically; this writes one summary per community and stores it as a
canonical record, so a global question reads summaries instead of scanning
everything.

Two rules, because a summary is the one place a model writes prose that is later
read back as fact:

* **It cites its members.** A summary with no provenance is an assertion.
* **A failed model leaves nothing.** An invented summary is worse than an absent
  one, because the absence is obvious and the invention is not.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from archivum.knowledge.graph_audit import detect_communities, load_graph
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.repository import KnowledgeRepository
from archivum.llm.cli_client import CliModelError, cli_chat_completion
from archivum.llm.prompt_context import with_context

logger = logging.getLogger(__name__)

# A cluster of one is a fact about that record, not a theme.
MIN_COMMUNITY_SIZE = 2

# Enough members to characterise a cluster without pasting the vault into a
# prompt. The canonical records remain the complete answer.
MAX_MEMBERS_IN_PROMPT = 40
MAX_SUMMARY_CHARS = 1200


def summary_id_for(scope: str, community_id: str) -> str:
    return f"summary:{scope}:{community_id}"


def _instruction(labels: list[str]) -> str:
    listed = "\n".join(f"- {label}" for label in labels)
    return (
        "These records are more connected to each other than to the rest of the "
        "vault. Describe, in three sentences or fewer, what this group is about "
        "and why these things belong together.\n\n"
        "Write only what the labels support. If they do not share an obvious "
        "theme, say that plainly rather than inventing one — a wrong theme is "
        "read back later as a fact about the vault.\n\n"
        f"Records:\n{listed}\n\n"
        "Reply with the description alone, no preamble."
    )


async def summarise_communities(
    repo: KnowledgeRepository,
    *,
    wiki_id: str,
    provider: str,
    model: str = "",
    owner: str | None = None,
    scope: str | None = None,
    limit: int = 40,
) -> list[str]:
    """Write one summary per community. Returns the ids that were written."""
    target = scope or f"wiki:{wiki_id}"
    nodes, edges = await load_graph(repo, scope=target, allowed_scopes={target})
    communities = [
        community
        for community in detect_communities(nodes, edges)
        if community.size >= MIN_COMMUNITY_SIZE
    ][:limit]
    if not communities:
        return []

    labels = {node.id: node.label for node in nodes}
    citations_by_id = {node.id: node.citations for node in nodes}
    written: list[str] = []

    for community in communities:
        members = [
            labels[member] for member in community.member_ids if member in labels
        ][:MAX_MEMBERS_IN_PROMPT]
        if not members:
            continue

        now = datetime.now(UTC)
        prompt = with_context(
            _instruction(members),
            now=now,
            owner=owner,
            extra={
                "Scope": target,
                "Cluster": community.label,
                "Records in cluster": str(community.size),
            },
        )
        try:
            summary = (
                await cli_chat_completion(provider=provider, model=model, prompt=prompt)
            ).strip()
        except (CliModelError, Exception) as exc:  # noqa: BLE001 - see module docstring
            logger.warning("Could not summarise %s: %s", community.id, exc)
            continue
        if not summary:
            continue

        # Cite the members the summary was written from, so a reader can check
        # it rather than take it on trust.
        citations: list[Citation] = []
        for member in community.member_ids[:MAX_MEMBERS_IN_PROMPT]:
            citations.extend(citations_by_id.get(member, [])[:1])
        if not citations:
            continue

        summary_id = summary_id_for(target, community.id)
        await repo.upsert_object(
            KnowledgeObject(
                id=summary_id,
                kind="community_summary",
                label=community.label,
                scope=target,
                # Written by a model from records it was shown, so inferred
                # rather than extracted, and never fully certain.
                confidence=0.7,
                extraction_method="INFERRED",
                citations=citations,
                properties={
                    "summary": summary[:MAX_SUMMARY_CHARS],
                    "community_id": community.id,
                    "member_count": community.size,
                    "member_ids": list(community.member_ids),
                    # So a stale summary is recognisable as stale rather than
                    # quietly describing a vault that has moved on.
                    "written_at": now.isoformat(),
                    "provider": provider,
                },
            )
        )
        # Join the summary to what it summarises. Naming the members in a
        # property is not a link: without an edge the summary is an orphan, and
        # a record nothing points at is unreachable from the person the vault is
        # about.
        for member in community.member_ids:
            await repo.upsert_relationship(
                KnowledgeRelationship(
                    id=f"rel:{summary_id}:summarises:{member}",
                    src_id=summary_id,
                    dst_id=member,
                    rel_type="summarises",
                    scope=target,
                    confidence=0.7,
                    extraction_method="INFERRED",
                    citations=citations[:1],
                    properties={"community_id": community.id},
                )
            )

        written.append(summary_id)

    return written


async def global_answer_context(
    repo: KnowledgeRepository, *, wiki_id: str, limit: int = 20
) -> list[KnowledgeObject]:
    """The summaries a global question should reason over, freshest first."""
    objects = await repo.list_objects(scope=f"wiki:{wiki_id}", limit=10_000)
    summaries = [obj for obj in objects if obj.kind == "community_summary"]
    summaries.sort(
        key=lambda obj: (str(obj.properties.get("written_at", "")), obj.id), reverse=True
    )
    return summaries[:limit]


async def run_summary_worker(settings) -> None:
    """Keep cluster summaries current, for the lifetime of the process.

    Slow and periodic on purpose: summarising is the one place Archivum spends
    real model time, and a vault's themes change over days rather than minutes.
    Runs through the configured synthesis provider, which can be a signed-in
    CLI — so on a subscription this costs nothing per run.
    """
    import asyncio

    from archivum.db import sqlite

    interval = max(settings.summary_worker_interval_seconds, 60)
    logger.info("Summary worker started", extra={"interval_s": interval})
    while True:
        try:
            await asyncio.sleep(interval)
            async with sqlite.get_db() as conn:
                written = await summarise_communities(
                    KnowledgeRepository(conn),
                    wiki_id="default",
                    provider=settings.llm_synthesis_provider,
                    model=settings.llm_model,
                    owner=settings.owner_username or None,
                )
                await conn.commit()
            if written:
                logger.info("Refreshed %d cluster summaries", len(written))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad pass must not stop the pump
            logger.warning("Summary sweep failed: %s", exc)
