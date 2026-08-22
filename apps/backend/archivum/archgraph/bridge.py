from __future__ import annotations

import re

from archivum.archgraph.mapper import CandidateRelationship, Provenance

_CONVERSATION_KINDS: frozenset[str] = frozenset({"conversation", "chunk"})

# Below this length an identifier is an ordinary English word as often as it is
# a symbol — `id`, `run`, `get`, `map`. Linking those would attach a decision to
# nearly every note in the vault and bury the edges that carry meaning.
_MIN_BRIDGE_NAME_LENGTH = 4

_WORD_RE = re.compile(r"\w+")


def _contains_sha(text: str, sha: str) -> bool:
    """Return True if *sha* appears as a substring in *text*."""
    return sha in text


def _tokens(text: str) -> set[str]:
    """The whole-word tokens in *text*.

    Tokenising each conversation once and testing symbols against the set turns
    what was a regex compile per (symbol, conversation) pair into a hash lookup.
    At repo scale — thousands of symbols, hundreds of notes — that is the
    difference between seconds and milliseconds on every ingest.
    """
    return set(_WORD_RE.findall(text))


async def bridge_evidence(l1) -> list[CandidateRelationship]:
    """Link code objects to PR / conversation / deployment evidence found in l1."""
    all_objects = await l1.list_objects()

    # Partition by kind
    commits = [o for o in all_objects if o.get("kind") == "commit"]
    prs = [o for o in all_objects if o.get("kind") == "pr"]
    symbols = [o for o in all_objects if o.get("kind") == "symbol"]
    conversations = [o for o in all_objects if o.get("kind") in _CONVERSATION_KINDS]
    deployments = [o for o in all_objects if o.get("kind") == "deployment"]

    results: list[CandidateRelationship] = []

    # 1. commit shipped_in pr: sha substring in PR text → EXTRACTED
    for commit in commits:
        sha = commit.get("label", "")
        if not sha:
            continue
        for pr in prs:
            pr_text = pr.get("text", "")
            if _contains_sha(pr_text, sha):
                prov = Provenance(
                    chunk_id=pr["id"],
                    span="L0",
                    extraction_method="EXTRACTED",
                )
                results.append(
                    CandidateRelationship(
                        id=f"{commit['id']}__shipped_in__{pr['id']}",
                        src_id=commit["id"],
                        dst_id=pr["id"],
                        rel_type="shipped_in",
                        scope="bridge",
                        confidence=1.0,
                        extraction_method="EXTRACTED",
                        provenance=[prov],
                    )
                )

    # 2. symbol decided_in conversation: whole-word match in conversation text → INFERRED
    conversation_tokens = [(conv, _tokens(conv.get("text", ""))) for conv in conversations]
    for symbol in symbols:
        name = symbol.get("label", "")
        if len(name) < _MIN_BRIDGE_NAME_LENGTH:
            continue
        for conv, tokens in conversation_tokens:
            if name in tokens:
                prov = Provenance(
                    chunk_id=conv["id"],
                    span="L0",
                    extraction_method="INFERRED",
                )
                results.append(
                    CandidateRelationship(
                        id=f"{symbol['id']}__decided_in__{conv['id']}",
                        src_id=symbol["id"],
                        dst_id=conv["id"],
                        rel_type="decided_in",
                        scope="bridge",
                        confidence=0.8,
                        extraction_method="INFERRED",
                        provenance=[prov],
                    )
                )

    # 3. (optional) commit deployed_in deployment: sha in deployment text → INFERRED
    for commit in commits:
        sha = commit.get("label", "")
        if not sha:
            continue
        for deploy in deployments:
            deploy_text = deploy.get("text", "")
            if _contains_sha(deploy_text, sha):
                prov = Provenance(
                    chunk_id=deploy["id"],
                    span="L0",
                    extraction_method="INFERRED",
                )
                results.append(
                    CandidateRelationship(
                        id=f"{commit['id']}__deployed_in__{deploy['id']}",
                        src_id=commit["id"],
                        dst_id=deploy["id"],
                        rel_type="deployed_in",
                        scope="bridge",
                        confidence=0.9,
                        extraction_method="INFERRED",
                        provenance=[prov],
                    )
                )

    return results
