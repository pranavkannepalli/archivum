"""Sessions have to arrive without being asked for.

Every piece of memory machinery downstream — atoms, decisions, skills,
`decided_in` — reads captured conversations. Capture was a tool an agent had to
choose to call, a frontend binding with no caller, and an importer with no
runner, so on a real install none of it ever ran.

This watches agent transcripts the way the vault watcher watches markdown: poll,
notice what moved, feed it through the importer that already existed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.capture.transcript_watch import (
    changed_transcripts,
    scan_transcripts,
    sweep_transcripts,
)
from archivum.config import Settings
from archivum.store.repository import SourceStore


def _session(path: Path, session_id: str, user_text: str, assistant_text: str) -> None:
    """Write a Claude Code style transcript."""
    lines = [
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-08-20T10:00:00Z",
            "message": {"role": "user", "content": user_text},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-08-20T10:01:00Z",
            "message": {"role": "assistant", "content": assistant_text},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


@pytest.fixture
def vault(tmp_path):
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    return Settings(
        db_path=tmp_path / "archivum.db",
        blob_dir=tmp_path / "blobs",
        wiki_dir=tmp_path / "wiki",
        transcript_dirs=[transcripts],
    )


async def test_a_transcript_that_appears_is_captured(vault):
    await sqlite_mod.init_db(vault)
    _session(
        vault.transcript_dirs[0] / "proj" / "abc.jsonl",
        "abc",
        "We decided to use uv over pip.",
        "Noted.",
    )

    snapshot, results = await sweep_transcripts(
        wiki_id="default", settings=vault, previous={}
    )

    assert len(results) == 1
    sources = await SourceStore().list_sources(wiki_id="default")
    assert len(sources) == 1
    assert sources[0].source_type.value == "conversation"


async def test_an_unchanged_transcript_is_not_captured_twice(vault):
    await sqlite_mod.init_db(vault)
    _session(vault.transcript_dirs[0] / "abc.jsonl", "abc", "hello", "hi")

    snapshot, first = await sweep_transcripts(
        wiki_id="default", settings=vault, previous={}
    )
    snapshot, second = await sweep_transcripts(
        wiki_id="default", settings=vault, previous=snapshot
    )

    assert len(first) == 1
    assert second == [], "an untouched transcript is not work to redo"


async def test_a_transcript_that_grows_is_captured_again(vault):
    """A session appends as it runs, so the same file arrives repeatedly."""
    await sqlite_mod.init_db(vault)
    path = vault.transcript_dirs[0] / "abc.jsonl"
    _session(path, "abc", "hello", "hi")

    snapshot, _ = await sweep_transcripts(wiki_id="default", settings=vault, previous={})
    _session(path, "abc", "hello", "hi there, at length")
    snapshot, second = await sweep_transcripts(
        wiki_id="default", settings=vault, previous=snapshot
    )

    assert len(second) == 1


async def test_captured_sessions_are_queued_for_distillation(vault):
    """Capture is the pump; distillation is what turns it into memory."""
    await sqlite_mod.init_db(vault)
    _session(vault.transcript_dirs[0] / "abc.jsonl", "abc", "I prefer uv.", "ok")

    await sweep_transcripts(wiki_id="default", settings=vault, previous={})

    async with sqlite_mod.get_db() as conn:
        async with conn.execute("SELECT source_id, kind FROM distill_queue") as cursor:
            queued = await cursor.fetchall()
    assert [row["kind"] for row in queued] == ["source"]


async def test_a_malformed_transcript_does_not_stop_the_sweep(vault):
    await sqlite_mod.init_db(vault)
    (vault.transcript_dirs[0] / "broken.jsonl").write_text("{not json", encoding="utf-8")
    _session(vault.transcript_dirs[0] / "good.jsonl", "good", "hello", "hi")

    snapshot, results = await sweep_transcripts(
        wiki_id="default", settings=vault, previous={}
    )

    assert len(results) == 1, "one bad file must not cost the others"


def test_scanning_a_directory_that_does_not_exist_is_empty(tmp_path):
    settings = Settings(transcript_dirs=[tmp_path / "nowhere"])
    assert scan_transcripts(settings) == {}


def test_changed_transcripts_notices_new_and_grown_files():
    previous = {"a.jsonl": 1.0, "b.jsonl": 2.0}
    current = {"a.jsonl": 1.0, "b.jsonl": 9.0, "c.jsonl": 3.0}

    assert changed_transcripts(previous, current) == {"b.jsonl", "c.jsonl"}
