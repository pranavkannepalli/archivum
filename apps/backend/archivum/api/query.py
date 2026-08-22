from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncGenerator

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from archivum.auth import CurrentUser, get_current_user
from archivum.config import Settings, get_settings
from archivum.db import sqlite
from archivum.knowledge.repository import KnowledgeRepository
from archivum.llm.openrouter_client import openrouter_chat_completion, openrouter_stream_tokens
from archivum.llm.openai_compat_client import (
    openai_compat_chat_completion,
    openai_compat_stream_tokens,
)
from archivum.llm.cli_client import cli_chat_completion
from archivum.llm.prompt_context import with_context
from archivum.retrieval.hybrid import hybrid_retrieve
from archivum.retrieval.context import ContextRequest, build_context_package

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["query"])

_MAX_CONTEXTS = 6
_MAX_EXCERPT_CHARS = 1_200
_CITATION_PATTERN = re.compile(r"\[(\d+)\]")
_INSUFFICIENT_EVIDENCE = "Insufficient evidence to answer this question from the retrieved context."


class QueryRequest(BaseModel):
    question: str


def _build_prompt(question: str, contexts: list[dict[str, Any]]) -> str:
    """The question, its evidence, and the date it is being asked on.

    Without the date a model resolves "recently" and "currently" against its
    training cutoff, which for a vault whose job is remembering *when* you
    thought something quietly produces wrong answers that read as right.
    """
    ctx_lines: list[str] = []
    for i, c in enumerate(contexts[:_MAX_CONTEXTS], start=1):
        slug = c.get("slug", "")
        title = c.get("title", "")
        excerpt = (c.get("excerpt") or "").strip()[:_MAX_EXCERPT_CHARS]
        if not excerpt:
            continue
        ctx_lines.append(f"[{i}] {title} ({slug})\n{excerpt}\n")

    ctx_block = "\n\n".join(ctx_lines) if ctx_lines else "(No relevant context found.)"

    return with_context(
        "You are Archivum, a knowledge base assistant. Answer using ONLY the provided context. "
        "Cite every factual claim with its bracketed context number. "
        "If the context is insufficient, explicitly say so.\n\n"
        f"Question:\n{question}\n\n"
        f"Context snippets:\n{ctx_block}\n\n"
        "Write a concise, helpful answer in markdown."
    )


@router.post("/query")
async def query(
    body: QueryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> EventSourceResponse:
    question = body.question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "Question cannot be empty", "code": "empty_question"},
        )

    if settings.llm_synthesis_provider == "anthropic" and not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "ANTHROPIC_API_KEY not configured", "code": "missing_api_key"},
        )
    if settings.llm_synthesis_provider == "openrouter" and not settings.openrouter_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "OPENROUTER_API_KEY not configured", "code": "missing_api_key"},
        )
    if settings.llm_synthesis_provider == "openai_compat" and not settings.openai_compat_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": "OPENAI_COMPAT_API_KEY not configured", "code": "missing_api_key"},
        )

    logger.info(
        "API query start",
        extra={
            "wiki_id": current_user.wiki_id,
            "question_chars": len(question),
            "provider": settings.llm_synthesis_provider,
            "model": settings.llm_synthesis_model,
        },
    )

    evidence = await prepare_query_evidence(question, current_user.wiki_id, settings)
    contexts = evidence["contexts"]
    citations = evidence["citations"]
    logger.info(
        "API query context ready",
        extra={
            "wiki_id": current_user.wiki_id,
            "hybrid_hits": evidence["hybrid_hits"],
            "scoped_hits": evidence["scoped_hits"],
            "contexts": len(contexts),
            "citations": len(citations),
        },
    )

    prompt = _build_prompt(question, contexts)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        # Send citations first so UI can render sources panel early
        yield {"data": json.dumps({"type": "citations", "citations": citations})}

        try:
            if not contexts:
                yield {
                    "data": json.dumps(
                        {"type": "token", "token": _INSUFFICIENT_EVIDENCE}
                    )
                }
                return

            answer_parts: list[str] = []
            if settings.llm_synthesis_provider == "anthropic":
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                stream = await client.messages.create(
                    model=settings.llm_synthesis_model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )

                async for event in stream:
                    # anthropic SDK events vary; handle the common delta events
                    try:
                        if event.type == "content_block_delta" and getattr(event, "delta", None):
                            text = getattr(event.delta, "text", None)
                            if text:
                                answer_parts.append(text)
                    except Exception:
                        continue

            elif settings.llm_synthesis_provider == "openrouter":
                async for token in openrouter_stream_tokens(
                    settings=settings,
                    model=settings.llm_synthesis_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.2,
                ):
                    answer_parts.append(token)
            elif settings.llm_synthesis_provider in {"openai_compat", "ollama"}:
                async for token in openai_compat_stream_tokens(
                    settings=settings,
                    provider=settings.llm_synthesis_provider,
                    model=settings.llm_synthesis_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.2,
                ):
                    answer_parts.append(token)
            elif settings.llm_synthesis_provider in {"codex_cli", "claude_cli"}:
                answer_parts.append(
                    await cli_chat_completion(
                        provider=settings.llm_synthesis_provider,
                        model=settings.llm_synthesis_model,
                        prompt=prompt,
                    )
                )
            else:
                raise ValueError(f"Unsupported llm_synthesis_provider: {settings.llm_synthesis_provider}")

            answer = _enforce_citations("".join(answer_parts), len(contexts))
            yield {"data": json.dumps({"type": "token", "token": answer})}

        except Exception as exc:
            logger.exception("Query synthesis error")
            yield {"data": json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"})}
        finally:
            logger.info("API query finished", extra={"wiki_id": current_user.wiki_id})
            yield {"data": "[DONE]"}

    return EventSourceResponse(event_generator())


def _slug_from_page_id(page_id: str, wiki_id: str) -> str | None:
    prefix = f"page:{wiki_id}:"
    return page_id.removeprefix(prefix) if page_id.startswith(prefix) else None


async def prepare_query_evidence(
    question: str, wiki_id: str, settings: Settings
) -> dict[str, Any]:
    """Return bounded, cited evidence shared by REST and MCP query surfaces."""
    hits = await hybrid_retrieve(question, wiki_id, limit=_MAX_CONTEXTS, settings=settings)
    scoped_hits = await _scope_hits_to_context_package(question, wiki_id, hits)
    evidence_hits = [hit for hit in scoped_hits if _has_usable_citation(hit)]
    contexts = [
        {
            "slug": _context_identifier(hit.id, wiki_id),
            "title": hit.label,
            "excerpt": hit.citation.quote or "",
            "score": hit.score,
        }
        for hit in evidence_hits
    ]
    slugs = [
        slug
        for hit in evidence_hits
        if (slug := _slug_from_page_id(hit.id, wiki_id)) is not None
    ]

    citations: list[dict[str, Any]] = []
    citation_rows = await sqlite.get_pages(slugs[:8], wiki_id)
    hits_by_id = {hit.id: hit for hit in evidence_hits}
    emitted_ids: set[str] = set()
    for row in citation_rows:
        hit = hits_by_id.get(f"page:{wiki_id}:{row['slug']}")
        if hit is None:
            continue
        citations.append(
            {
                "slug": row["slug"],
                "title": row["title"],
                "content": row["content"],
                "tags": json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "authored_by": row["authored_by"],
                "citation": hit.citation.model_dump(),
            }
        )
        emitted_ids.add(hit.id)
    for hit in evidence_hits:
        if hit.id in emitted_ids:
            continue
        citations.append(_canonical_citation_payload(hit, wiki_id))

    return {
        "contexts": contexts,
        "citations": citations,
        "hybrid_hits": len(hits),
        "scoped_hits": len(scoped_hits),
        "insufficient_evidence": not bool(contexts),
        "reason": _INSUFFICIENT_EVIDENCE if not contexts else None,
    }


async def synthesize_query_answer(question: str, wiki_id: str, settings: Settings) -> dict[str, Any]:
    """Return a non-streaming cited answer using the same evidence path as REST."""
    evidence = await prepare_query_evidence(question, wiki_id, settings)
    contexts = evidence["contexts"]
    if not contexts:
        return {
            "answer": _INSUFFICIENT_EVIDENCE,
            "citations": evidence["citations"],
            "insufficient_evidence": True,
            "reason": evidence["reason"],
        }

    prompt = _build_prompt(question, contexts)
    if settings.llm_synthesis_provider == "anthropic":
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.llm_synthesis_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = (response.content[0].text if response.content else "").strip()
    elif settings.llm_synthesis_provider == "openrouter":
        answer = await openrouter_chat_completion(
            settings=settings,
            model=settings.llm_synthesis_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.2,
        )
    elif settings.llm_synthesis_provider in {"openai_compat", "ollama"}:
        answer = await openai_compat_chat_completion(
            settings=settings,
            provider=settings.llm_synthesis_provider,
            model=settings.llm_synthesis_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.2,
        )
    else:
        raise ValueError(f"Unsupported llm_synthesis_provider: {settings.llm_synthesis_provider}")

    answer = _enforce_citations(answer, len(contexts))
    return {
        "answer": answer,
        "citations": evidence["citations"],
        "insufficient_evidence": answer == _INSUFFICIENT_EVIDENCE,
        "reason": _INSUFFICIENT_EVIDENCE if answer == _INSUFFICIENT_EVIDENCE else None,
    }


async def _scope_hits_to_context_package(question: str, wiki_id: str, hits: list):
    """Restrict hybrid evidence to the matching bounded canonical subgraph.

    Page/vector retrieval remains the textual evidence source for synthesis. The
    context package establishes the canonical scope. If that derived index is
    unavailable or contains none of the retrieved ids, preserve Task 7's cited
    hybrid behavior rather than treating an index rebuild gap as no evidence.
    """
    if not hits:
        return hits
    try:
        async with sqlite.get_db() as connection:
            package = await build_context_package(
                KnowledgeRepository(connection),
                ContextRequest(
                    query=question,
                    scope=f"wiki:{wiki_id}",
                    seed_ids=[hit.id for hit in hits],
                    depth=1,
                    max_nodes=_MAX_CONTEXTS,
                ),
            )
    except Exception:
        logger.warning("Query context package unavailable", exc_info=True)
        return hits

    scoped_ids = {node.id for node in package.nodes}
    return [hit for hit in hits if hit.id in scoped_ids]


def _context_identifier(hit_id: str, wiki_id: str) -> str:
    return _slug_from_page_id(hit_id, wiki_id) or hit_id


def _has_usable_citation(hit) -> bool:
    if hit.id == "person:self":
        return False
    return bool((hit.citation.quote or "").strip())


def _enforce_citations(answer: str, context_count: int) -> str:
    answer = answer.strip()
    if not answer or context_count <= 0:
        return _INSUFFICIENT_EVIDENCE
    citation_indices = [int(match) for match in _CITATION_PATTERN.findall(answer)]
    if any(1 <= index <= context_count for index in citation_indices):
        return answer
    return _INSUFFICIENT_EVIDENCE


def _canonical_citation_payload(hit, wiki_id: str) -> dict[str, Any]:
    return {
        "slug": _context_identifier(hit.id, wiki_id),
        "title": hit.label,
        "content": hit.citation.quote or "",
        "tags": [],
        "created_at": None,
        "updated_at": None,
        "authored_by": "agent",
        "citation": hit.citation.model_dump(),
    }
