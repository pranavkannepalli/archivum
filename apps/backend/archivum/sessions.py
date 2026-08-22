"""What a session did, joined to the code it did it to.

Capture stores the conversation. On its own that is prose in a blob: searchable,
but it cannot answer "what happened to this function?" — the question a person
actually has when they open a file they wrote three months ago.

So each captured session also becomes a small canonical record — what kind of
work it was, which files it changed — linked to the symbols it touched. The link
lives in the `bridge` scope because it joins a wiki's memory to a repository's
code and belongs to neither.

Everything here is derived from what the session did, not from a model's reading
of it, so the same transcript always produces the same record.
"""

from __future__ import annotations

import logging
from pathlib import Path

from archivum.capture.classify import classify_session, touched_paths
from archivum.capture.schema import Conversation
from archivum.code_repos import list_repos
from archivum.fixes import extract_fix, fix_id_for, fix_to_object
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.personal_root import ensure_personal_root, link_to_self
from archivum.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

# Enough of the request to recognise the session in a list.
_MAX_LABEL = 160

# Symbols in a touched file. A file with hundreds of symbols would otherwise
# attach the whole module to one session and drown the signal.
_MAX_TOUCHED_SYMBOLS = 40


def session_id_for(source_id: str) -> str:
    return f"session:{source_id}"


def _label(conversation: Conversation) -> str:
    for turn in conversation.turns:
        if turn.role == "user" and turn.text.strip():
            return " ".join(turn.text.split())[:_MAX_LABEL]
    return conversation.session_id or "Session"


async def _symbols_touched(
    repo: KnowledgeRepository, *, paths: list[str], wiki_id: str
) -> list[KnowledgeObject]:
    """Symbols defined in the files a session changed.

    A path is only meaningful once it is inside a repository this vault has
    registered; work in an unindexed directory links to nothing rather than
    being guessed at.
    """
    repos = await list_repos(wiki_id=wiki_id)
    if not repos:
        return []

    resolved = [Path(path) for path in paths]
    touched: list[KnowledgeObject] = []
    for repo_entry in repos:
        root = Path(repo_entry.path)
        inside = [
            path
            for path in resolved
            if path == root or root in path.parents
        ]
        if not inside:
            continue
        wanted = {str(path) for path in inside}
        for object_ in await repo.list_objects(scope=repo_entry.scope, limit=10_000):
            if object_.kind not in ("symbol", "type"):
                continue
            citation = object_.citations[0] if object_.citations else None
            if citation is None or citation.chunk_id not in wanted:
                continue
            touched.append(object_)
            if len(touched) >= _MAX_TOUCHED_SYMBOLS:
                return touched
    return touched


async def record_session_work(
    repo: KnowledgeRepository,
    *,
    conversation: Conversation,
    source_id: str,
    wiki_id: str,
) -> KnowledgeObject:
    """Record one captured session and link it to the code it changed."""
    session_id = session_id_for(source_id)
    scope = f"wiki:{wiki_id}"
    kind = classify_session(conversation)
    paths = touched_paths(conversation)

    citation = Citation(
        source_id=source_id,
        chunk_id=source_id,
        span_start=None,
        span_end=None,
        quote=_label(conversation),
    )
    session = KnowledgeObject(
        id=session_id,
        kind="session",
        label=_label(conversation),
        scope=scope,
        confidence=1.0,
        extraction_method="EXTRACTED",
        citations=[citation],
        properties={
            "kind": kind,
            "session_id": conversation.session_id,
            "interface": conversation.interface,
            "started_at": conversation.started_at,
            "touched_paths": paths,
        },
    )
    await repo.upsert_object(session)
    await ensure_personal_root(repo, wiki_id=wiki_id)
    await link_to_self(repo, session_id, "did_work", citation=citation)

    touched = await _symbols_touched(repo, paths=paths, wiki_id=wiki_id)

    # A bug fix leaves a second record: what the trouble was and what settled
    # it. This is the one people actually come back for.
    fix = extract_fix(conversation)
    if fix is not None:
        await repo.upsert_object(
            fix_to_object(fix, source_id=source_id, wiki_id=wiki_id)
        )
        await link_to_self(repo, fix_id_for(source_id), "remembers", citation=citation)
        for symbol in touched:
            await repo.upsert_relationship(
                KnowledgeRelationship(
                    id=f"{fix_id_for(source_id)}__fixes__{symbol.id}",
                    src_id=fix_id_for(source_id),
                    dst_id=symbol.id,
                    rel_type="fixes",
                    scope="bridge",
                    confidence=0.9 if fix.verified_by else 0.6,
                    extraction_method="EXTRACTED",
                    citations=[citation],
                    properties={"verified_by": fix.verified_by},
                )
            )

    for symbol in touched:
        await repo.upsert_relationship(
            KnowledgeRelationship(
                # Deterministic id, so re-capturing a session that grew does not
                # accumulate duplicate links to the same symbol.
                id=f"{session_id}__touched__{symbol.id}",
                src_id=session_id,
                dst_id=symbol.id,
                rel_type="touched",
                scope="bridge",
                confidence=1.0,
                extraction_method="EXTRACTED",
                citations=[citation],
                properties={"session_kind": kind},
            )
        )
    return session
