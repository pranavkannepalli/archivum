"""Notice agent sessions the way the vault watcher notices markdown.

Everything downstream of capture — atoms, decisions, skills, `decided_in` —
reads captured conversations. Capture itself was a tool an agent had to choose
to call, a frontend binding nothing called, and an importer nothing ran, so on a
real install none of that machinery ever saw a single session.

This closes that loop without asking anyone to remember: poll the directories
agents write transcripts into, and feed anything that moved through the importer
and redaction that already existed. A session appends while it runs, so the same
file legitimately arrives many times; capture is content-addressed, so the only
cost of re-reading an unchanged one is a hash.

Polling rather than filesystem events, for the same reasons `vault_watch` gives:
inotify is unreliable across bind mounts, and a stat sweep over a few hundred
transcripts is milliseconds.

Deployment note: the backend usually runs in a container, so the transcript
directory has to be mounted in and named by `TRANSCRIPT_DIRS`. Unset, this
watcher does nothing rather than guessing at a path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from archivum.capture.importers import connector_for
from archivum.capture.importers import chatgpt as _chatgpt  # noqa: F401 (self-register)
from archivum.capture.importers import claude_code as _claude_code  # noqa: F401
from archivum.capture.store import CaptureResult, CaptureStore
from archivum.config import Settings
from archivum.db import sqlite
from archivum.knowledge.repository import KnowledgeRepository
from archivum.sessions import record_session_work

logger = logging.getLogger(__name__)

TRANSCRIPT_SUFFIXES = (".jsonl",)


def scan_transcripts(settings: Settings) -> dict[str, float]:
    """Path → mtime for every transcript currently on disk."""
    seen: dict[str, float] = {}
    for directory in settings.transcript_dirs:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for suffix in TRANSCRIPT_SUFFIXES:
            for path in root.rglob(f"*{suffix}"):
                try:
                    seen[str(path)] = path.stat().st_mtime
                except OSError:
                    # Vanished between listing and stat; the next sweep settles it.
                    continue
    return seen


def changed_transcripts(
    previous: dict[str, float], current: dict[str, float]
) -> set[str]:
    """Which transcripts are new or have grown since the last sweep.

    Removals are deliberately ignored: a session that has been captured is
    memory of work that happened, and deleting the transcript afterwards does
    not unhappen it.
    """
    return {
        path
        for path, mtime in current.items()
        if path not in previous or previous[path] != mtime
    }


async def capture_transcript(
    path: Path, *, wiki_id: str, settings: Settings
) -> list[CaptureResult]:
    """Import one transcript and record it as evidence."""
    connector = connector_for(path)
    if connector is None:
        return []

    imported = connector.parse(path)
    store = CaptureStore(wiki_id=wiki_id, settings=settings)
    results: list[CaptureResult] = []
    for conversation in imported.conversations:
        if not conversation.turns:
            continue
        result = await store.capture(conversation)
        results.append(result)

        # Record what kind of work this was and which code it touched, so the
        # session is findable from the code rather than only from its text.
        async with sqlite.get_db() as conn:
            await record_session_work(
                KnowledgeRepository(conn),
                conversation=conversation,
                source_id=result.source_id,
                wiki_id=wiki_id,
            )
            await conn.commit()

        # Capture is the pump; distillation is what turns it into memory. It is
        # queued rather than run here because it may reach a model.
        await sqlite.enqueue_distillation(result.source_id, wiki_id, kind="source")
    return results


async def sweep_transcripts(
    *, wiki_id: str, settings: Settings, previous: dict[str, float]
) -> tuple[dict[str, float], list[CaptureResult]]:
    """One pass. Returns the new snapshot and what it captured."""
    current = scan_transcripts(settings)
    results: list[CaptureResult] = []

    for path in sorted(changed_transcripts(previous, current)):
        try:
            results.extend(
                await capture_transcript(Path(path), wiki_id=wiki_id, settings=settings)
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not cost the rest
            logger.warning("Could not capture transcript %s: %s", path, exc)
            continue

    return current, results


async def run_transcript_watcher(settings: Settings, *, wiki_id: str = "default") -> None:
    """Watch agent transcripts for the lifetime of the process."""
    if not settings.transcript_dirs:
        logger.info("No transcript directories configured; session capture is off")
        return

    interval = max(settings.transcript_watch_interval_seconds, 1)
    logger.info(
        "Watching agent transcripts",
        extra={
            "directories": [str(d) for d in settings.transcript_dirs],
            "interval_s": interval,
        },
    )
    # Seed from what is already on disk so a restart does not re-import the
    # backlog. Capture would deduplicate it, but the work is still wasted.
    snapshot = scan_transcripts(settings)

    while True:
        try:
            await asyncio.sleep(interval)
            snapshot, results = await sweep_transcripts(
                wiki_id=wiki_id, settings=settings, previous=snapshot
            )
            if results:
                logger.info("Captured %d session(s)", len(results))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad sweep must not kill the watcher
            logger.warning("Transcript sweep failed: %s", exc)
