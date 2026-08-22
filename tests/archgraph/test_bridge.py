from __future__ import annotations

from archivum.archgraph.bridge import bridge_evidence


class FakeL1:
    """In-memory stand-in for PER-317's L1 read API used by archgraph resolvers."""

    def __init__(self, objects=None):
        self._objects = list(objects or [])

    async def list_objects(self, kind=None, scope=None):
        return [
            o
            for o in self._objects
            if (kind is None or o.get("kind") == kind)
            and (scope is None or o.get("scope") == scope)
        ]


async def test_commit_shipped_in_pr_extracted():
    """Commit sha 'abc123def' appears in PR text → shipped_in edge, method EXTRACTED."""
    l1 = FakeL1([
        {"id": "commit-abc", "kind": "commit", "scope": "repo:a", "label": "abc123def"},
        {"id": "pr-42", "kind": "pr", "scope": "repo:a", "label": "PR #42", "text": "fixes abc123def and closes issue"},
    ])
    results = await bridge_evidence(l1)
    assert len(results) == 1
    r = results[0]
    assert r.rel_type == "shipped_in"
    assert r.src_id == "commit-abc"
    assert r.dst_id == "pr-42"
    assert r.extraction_method == "EXTRACTED"
    assert len(r.provenance) == 1
    assert r.provenance[0].chunk_id == "pr-42"


async def test_symbol_decided_in_conversation_inferred():
    """Symbol 'retrieve_code' appears as whole-word token in conversation → decided_in, INFERRED."""
    l1 = FakeL1([
        {"id": "sym-retrieve", "kind": "symbol", "scope": "repo:a", "label": "retrieve_code"},
        {"id": "conv-1", "kind": "conversation", "scope": "repo:a", "label": "design chat", "text": "we should refactor retrieve_code to be async"},
    ])
    results = await bridge_evidence(l1)
    assert len(results) == 1
    r = results[0]
    assert r.rel_type == "decided_in"
    assert r.src_id == "sym-retrieve"
    assert r.dst_id == "conv-1"
    assert r.extraction_method == "INFERRED"
    assert len(r.provenance) == 1
    assert r.provenance[0].chunk_id == "conv-1"


async def test_no_bridge_without_evidence():
    """Symbol 'foo' with no matching conversation or PR → returns []."""
    l1 = FakeL1([
        {"id": "sym-foo", "kind": "symbol", "scope": "repo:a", "label": "foo"},
        {"id": "conv-2", "kind": "conversation", "scope": "repo:a", "label": "other chat", "text": "we discussed bar and baz"},
    ])
    results = await bridge_evidence(l1)
    assert results == []


async def test_short_symbol_names_do_not_bridge():
    """A symbol called 'id' or 'run' matches ordinary prose, not a decision.

    Bridging reads free text, so a two- or three-character identifier would link
    to nearly every conversation in the vault and bury the edges that mean
    something. Real names earn the edge; noise words do not.
    """
    l1 = FakeL1([
        {"id": "sym-id", "kind": "symbol", "scope": "repo:a", "label": "id"},
        {"id": "sym-run", "kind": "symbol", "scope": "repo:a", "label": "run"},
        {
            "id": "conv-1",
            "kind": "conversation",
            "scope": "repo:a",
            "label": "chat",
            "text": "we run the id check before the deploy",
        },
    ])
    assert await bridge_evidence(l1) == []


async def test_bridging_scales_to_a_realistic_repo():
    """Thousands of symbols against hundreds of notes must not be quadratic-slow.

    The first implementation compiled a regex per (symbol, conversation) pair.
    At repo scale that is millions of regex searches on every ingest.
    """
    import time

    objects = [
        {"id": f"sym-{i}", "kind": "symbol", "scope": "repo:a", "label": f"handler_{i}"}
        for i in range(3000)
    ]
    objects += [
        {
            "id": f"conv-{j}",
            "kind": "conversation",
            "scope": "wiki:default",
            "label": "note",
            "text": "we discussed handler_7 and the retry policy at length " * 20,
        }
        for j in range(300)
    ]

    started = time.perf_counter()
    results = await bridge_evidence(FakeL1(objects))
    elapsed = time.perf_counter() - started

    assert len(results) == 300, "handler_7 should link to each note that names it"
    assert elapsed < 2.0, f"bridging took {elapsed:.1f}s"
