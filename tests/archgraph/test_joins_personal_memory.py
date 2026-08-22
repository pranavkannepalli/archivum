"""Code has to join the rest of the vault, not sit beside it.

Both derived resolvers read from an L1 view. That view used to be built from the
candidates of the single ingest run that was in progress, which made two whole
features unreachable no matter what was in the store:

* `resolve_cross_repo` compares scopes, and one run only ever has one scope, so
  `same_symbol_as` could never fire.
* `bridge_evidence` looks for conversation and PR evidence, and a code-only run
  contains neither, so `decided_in` could never fire — the edge that answers
  "why does this function exist?", which is the whole point of code memory for
  someone who writes code.

Both had passing tests built on a hand-made view holding data the production
view could not contain. These run against the real repository instead.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import aiosqlite
import pytest

from archivum.archgraph.ingest import ingest_repo
from archivum.knowledge.models import Citation, KnowledgeObject
from archivum.knowledge.repository import KnowledgeRepository, init_knowledge_schema


def _commit_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    for relative, body in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@example.com", "-c", "user.name=Test",
            "commit", "-q", "-m", "initial",
        ],
        check=True,
    )
    return repo


async def _ingest(repo: Path, scope: str, knowledge, tmp_path: Path, conn, related=None):
    """Index one repo. `related` names the other scopes links may reach."""
    cache = tmp_path / f"cache-{scope.replace(':', '-')}"
    cache.mkdir(parents=True, exist_ok=True)
    return await ingest_repo(
        repo,
        scope=scope,
        cache_dir=cache,
        knowledge=knowledge,
        lexical_conn=conn,
        related_scopes=related,
    )


@pytest.fixture(autouse=True)
def _needs_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")


CALC = "def hypot(a, b):\n    return helper(a) + helper(b)\n\n\ndef helper(x):\n    return x * x\n"
SHAPES = "class RetryPolicy:\n    def attempt(self):\n        return 1\n"


@pytest.mark.asyncio
async def test_a_symbol_links_to_the_decision_that_mentions_it(tmp_path):
    repo = _commit_repo(tmp_path, "a", {"calc.py": CALC})

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        # A distilled decision already in memory, of the kind capture produces.
        await knowledge.upsert_object(
            KnowledgeObject(
                id="memory:atom:wiki:default:decision:1",
                kind="memory_atom",
                label="Use hypot for the distance check",
                scope="wiki:default",
                confidence=0.9,
                extraction_method="EXTRACTED",
                citations=[Citation(source_id="s1", chunk_id="c1", span_start=None, span_end=None, quote="hypot")],
                properties={
                    "atom_type": "decision",
                    "text": "We decided hypot should stay pure so it can be tested directly.",
                },
            )
        )

        await _ingest(repo, "repo:a", knowledge, tmp_path, conn, related={"wiki:default"})
        bridged = await knowledge.list_relationships(scope="bridge")

    decided = [rel for rel in bridged if rel.rel_type == "decided_in"]
    assert decided, "a symbol named in a recorded decision must link to it"
    assert any(rel.src_id.endswith("hypot") for rel in decided)
    assert all(rel.extraction_method == "INFERRED" for rel in decided)
    assert all(rel.citations for rel in decided)


@pytest.mark.asyncio
async def test_a_symbol_nobody_discussed_gets_no_decision_link(tmp_path):
    repo = _commit_repo(tmp_path, "a", {"calc.py": CALC})

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        await knowledge.upsert_object(
            KnowledgeObject(
                id="memory:atom:wiki:default:decision:2",
                kind="memory_atom",
                label="Unrelated",
                scope="wiki:default",
                confidence=0.9,
                extraction_method="EXTRACTED",
                citations=[Citation(source_id="s1", chunk_id="c1", span_start=None, span_end=None, quote="x")],
                properties={"atom_type": "decision", "text": "We talked about deployments."},
            )
        )

        await _ingest(repo, "repo:a", knowledge, tmp_path, conn)
        bridged = await knowledge.list_relationships(scope="bridge")

    assert bridged == []


@pytest.mark.asyncio
async def test_the_same_type_in_two_repos_is_recognised_as_one_thing(tmp_path):
    repo_a = _commit_repo(tmp_path, "a", {"shapes.py": SHAPES})
    repo_b = _commit_repo(tmp_path, "b", {"shapes.py": SHAPES})

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        await _ingest(repo_a, "repo:a", knowledge, tmp_path, conn)
        # Two repositories in one vault may recognise a shared type.
        await _ingest(repo_b, "repo:b", knowledge, tmp_path, conn, related={"repo:a"})
        cross = await knowledge.list_relationships(scope="cross_repo")

    same = [rel for rel in cross if rel.rel_type == "same_symbol_as"]
    assert same, "RetryPolicy exists in both repos and should be linked across them"
    assert {rel.src_id.split(":")[0] for rel in same}


@pytest.mark.asyncio
async def test_one_repo_alone_produces_no_cross_repo_claims(tmp_path):
    repo = _commit_repo(tmp_path, "a", {"shapes.py": SHAPES})

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        await _ingest(repo, "repo:a", knowledge, tmp_path, conn)
        cross = await knowledge.list_relationships(scope="cross_repo")

    assert cross == []


@pytest.mark.asyncio
async def test_bridging_does_not_reach_into_another_vault(tmp_path):
    """A decision link may only be drawn from memory the repository's vault owns.

    Derived links are resolved against the whole store, which is what lets a
    symbol find a decision recorded months earlier. Left unbounded that same
    sweep would draw an edge from this repository into another vault's memory,
    manufacturing a cross-tenant link that no reader ever authorised.
    """
    repo = _commit_repo(tmp_path, "a", {"calc.py": CALC})

    async with aiosqlite.connect(tmp_path / "k.db") as conn:
        await init_knowledge_schema(conn)
        knowledge = KnowledgeRepository(conn)
        for scope, suffix in (("wiki:mine", "mine"), ("wiki:theirs", "theirs")):
            await knowledge.upsert_object(
                KnowledgeObject(
                    id=f"memory:atom:{suffix}",
                    kind="memory_atom",
                    label=f"decision {suffix}",
                    scope=scope,
                    confidence=0.9,
                    extraction_method="EXTRACTED",
                    citations=[
                        Citation(
                            source_id="s", chunk_id="c",
                            span_start=None, span_end=None, quote="hypot",
                        )
                    ],
                    properties={"atom_type": "decision", "text": "hypot should stay pure."},
                )
            )

        cache = tmp_path / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        await ingest_repo(
            repo,
            scope="repo:mine:a",
            cache_dir=cache,
            knowledge=knowledge,
            lexical_conn=conn,
            related_scopes={"repo:mine:a", "wiki:mine"},
        )
        bridged = await knowledge.list_relationships(scope="bridge")

    targets = {rel.dst_id for rel in bridged}
    assert "memory:atom:mine" in targets, "the owning vault's decision should link"
    assert "memory:atom:theirs" not in targets, "another vault's must not"
