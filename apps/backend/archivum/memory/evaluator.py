"""Hybrid evaluator: deterministic skeleton, LLM-assisted semantic judgment.

Deterministic rules alone miss meaning; LLMs alone over-save eloquent junk.
This module adds the optional LLM half: it re-scores rule-extracted atoms on
the strategy's dimensions, assigns semantic types, and proposes additional
candidate atoms. Everything it returns feeds review cards — the LLM never
promotes memory on its own, and every failure falls back to deterministic
scoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from archivum.capture.schema import Conversation
from archivum.config import Settings
from archivum.llm.prompt_context import with_context
from archivum.memory.atoms import SEMANTIC_ATOM_TYPES, Atom

logger = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 8_000
_MAX_RESPONSE_TOKENS = 2_000

SCORE_DIMENSIONS = (
    "human_relevance",
    "future_utility",
    "durability",
    "specificity",
    "novelty",
)

_SYSTEM_PROMPT = f"""You are the memory evaluator for a human-centered \
knowledge system. You judge candidate memory atoms extracted from one \
conversation. Optimize for minimum high-signal memory: a candidate should \
survive only if it will be useful later to the human or an agent acting for \
them.

Score each listed atom on these dimensions from 0.0 to 1.0: \
{", ".join(SCORE_DIMENSIONS)}. Assign one semantic type from: \
{", ".join(sorted(SEMANTIC_ATOM_TYPES))}. Set keep=false for noise, \
redundancy, or anything harmful, sensitive, misleading, or stale. You may \
also propose up to 5 additional atoms the rules missed, each anchored to a \
turn index.

Respond with only JSON:
{{"atoms": [{{"index": 0, "keep": true, "semantic_type": "preference",
"scores": {{"human_relevance": 0.8, "future_utility": 0.7, "durability": 0.9,
"specificity": 0.8, "novelty": 0.6}}, "rationale": "...",
"durability_estimate": "long"}}],
"proposed": [{{"text": "...", "semantic_type": "decision", "turn_index": 0,
"rationale": "..."}}]}}"""


@dataclass(frozen=True)
class AtomEvaluation:
    keep: bool
    semantic_type: str | None
    scores: dict[str, float]
    rationale: str
    durability_estimate: str

    @property
    def composite(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(self.scores.values()) / len(self.scores), 4)


@dataclass(frozen=True)
class ProposedAtom:
    text: str
    semantic_type: str
    turn_index: int
    rationale: str


@dataclass(frozen=True)
class EvaluationResult:
    evaluations: dict[int, AtomEvaluation] = field(default_factory=dict)
    proposed: list[ProposedAtom] = field(default_factory=list)


async def evaluate_conversation(
    conversation: Conversation,
    atoms: list[Atom],
    *,
    settings: Settings,
) -> EvaluationResult | None:
    """Ask the configured LLM to judge extracted atoms. None on any failure."""
    if not atoms and not conversation.turns:
        return None
    try:
        raw = await _chat(
            settings,
            # Dated at call time: a module-level constant would carry
            # whenever the process started, which for a long-lived server is
            # not today.
            system=with_context(_SYSTEM_PROMPT),
            user=_render_user_prompt(conversation, atoms),
        )
        return _parse_response(raw, atom_count=len(atoms))
    except Exception:
        logger.warning(
            "LLM atom evaluation failed; falling back to deterministic scores",
            exc_info=True,
        )
        return None


def blend_confidence(deterministic: float, evaluation: AtomEvaluation) -> float:
    """Combine rule and LLM judgment; a keep=false verdict caps below any
    promotion threshold so vetoed atoms still reach review, never canonical."""
    blended = round((deterministic + evaluation.composite) / 2, 4)
    if not evaluation.keep:
        return min(blended, 0.1)
    return blended


def _render_user_prompt(conversation: Conversation, atoms: list[Atom]) -> str:
    transcript_lines = [
        f"[{index}] {turn.role}: {turn.text}"
        for index, turn in enumerate(conversation.turns)
    ]
    transcript = "\n".join(transcript_lines)[:_MAX_TRANSCRIPT_CHARS]
    atom_lines = [
        f"{index}. ({atom.atom_type}, rule confidence {atom.confidence:.2f}) {atom.text}"
        for index, atom in enumerate(atoms)
    ]
    return (
        "Conversation turns (index: role: text):\n"
        f"{transcript}\n\n"
        "Rule-extracted candidate atoms:\n"
        + ("\n".join(atom_lines) if atom_lines else "(none)")
    )


def _parse_response(raw: str, *, atom_count: int) -> EvaluationResult | None:
    payload = _extract_json(raw)
    if payload is None:
        logger.warning("LLM atom evaluation returned unparseable output")
        return None
    evaluations: dict[int, AtomEvaluation] = {}
    for entry in payload.get("atoms", []):
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < atom_count:
            continue
        semantic_type = entry.get("semantic_type")
        if semantic_type not in SEMANTIC_ATOM_TYPES:
            semantic_type = None
        evaluations[index] = AtomEvaluation(
            keep=bool(entry.get("keep", True)),
            semantic_type=semantic_type,
            scores=_clean_scores(entry.get("scores")),
            rationale=str(entry.get("rationale", "")).strip(),
            durability_estimate=str(entry.get("durability_estimate", "")).strip(),
        )
    proposed: list[ProposedAtom] = []
    for entry in payload.get("proposed", []):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        semantic_type = entry.get("semantic_type")
        if not text or semantic_type not in SEMANTIC_ATOM_TYPES:
            continue
        turn_index = entry.get("turn_index")
        proposed.append(
            ProposedAtom(
                text=text,
                semantic_type=str(semantic_type),
                turn_index=turn_index if isinstance(turn_index, int) else 0,
                rationale=str(entry.get("rationale", "")).strip(),
            )
        )
    return EvaluationResult(evaluations=evaluations, proposed=proposed[:5])


def _clean_scores(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    scores: dict[str, float] = {}
    for dimension in SCORE_DIMENSIONS:
        value = raw.get(dimension)
        if isinstance(value, (int, float)):
            scores[dimension] = round(min(max(float(value), 0.0), 1.0), 4)
    return scores


def _extract_json(raw: str) -> dict | None:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _chat(settings: Settings, *, system: str, user: str) -> str:
    provider = settings.llm_extraction_provider
    if provider == "anthropic":
        return await _anthropic_chat(settings, system=system, user=user)
    if provider == "openrouter":
        from archivum.llm.openrouter_client import openrouter_chat_completion

        return await openrouter_chat_completion(
            settings=settings,
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=_MAX_RESPONSE_TOKENS,
        )
    if provider in {"openai_compat", "ollama"}:
        from archivum.llm.openai_compat_client import openai_compat_chat_completion

        return await openai_compat_chat_completion(
            settings=settings,
            provider=provider,
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=_MAX_RESPONSE_TOKENS,
        )
    raise RuntimeError(f"Unsupported llm_extraction_provider: {provider}")


async def _anthropic_chat(settings: Settings, *, system: str, user: str) -> str:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def _call() -> str:
        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=_MAX_RESPONSE_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in response.content if hasattr(block, "text")
        )

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call)
