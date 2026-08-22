"""The folders a new vault starts with.

An empty vault makes every capture a decision: where does this go? The composer
already guesses a folder, but it can only guess among folders that exist, so on
a fresh install it always guesses "the root" and everything piles up there.

These are a starting point, not a policy. They are created once, on a vault that
has no folders of its own. Delete one and it stays deleted; arrive with your own
structure and nothing is added — being handed a second organising scheme on top
of your own is worse than being handed none.

The names are deliberately about *what a thing is to you* rather than what
Archivum did to it. `code/`, `memory/` and `skills/` are written by the system
and are excluded from this list for that reason: they are output, not places you
put things.
"""

from __future__ import annotations

import logging
from pathlib import Path

from archivum.config import Settings
from archivum.db import sqlite

logger = logging.getLogger(__name__)

# Ordered as they should read in a sidebar: the things you add to daily first,
# the reference material after.
DEFAULT_FOLDERS: tuple[str, ...] = (
    "inbox",
    "daily",
    "notes",
    "projects",
    "areas",
    "people",
    "decisions",
    "reading",
    "reference",
    "sources",
    "archive",
)

FOLDER_PURPOSE: dict[str, str] = {
    "inbox": "Anything captured before you decided where it goes.",
    "daily": "One note per day. `T` opens today's.",
    "notes": "Thinking that is not about one project.",
    "projects": "A page per thing you are actually building.",
    "areas": "Ongoing responsibilities rather than finite projects.",
    "people": "Who you work with, and what you know about them.",
    "decisions": "What you chose and why, so future-you can check the reasoning.",
    "reading": "Papers, posts and books, with what you took from them.",
    "reference": "Documentation and specs you come back to.",
    "sources": "Things you brought in from elsewhere.",
    "archive": "Done, but not deleted.",
}


async def ensure_default_folders(
    *, wiki_id: str, settings: Settings | None = None
) -> list[str]:
    """Create the starting folders on a vault that has none. Returns what it made.

    Deliberately all-or-nothing on the *first* run: if any folder already
    exists, the vault has a shape and this does not add to it. That is what
    keeps a deleted folder deleted, rather than having it reappear on every
    restart.
    """
    existing = {row["path"] for row in await sqlite.list_folders(wiki_id)}
    if existing:
        return []

    created: list[str] = []
    for path in DEFAULT_FOLDERS:
        try:
            await sqlite.create_folder(path, wiki_id)
            created.append(path)
        except Exception as exc:  # noqa: BLE001 - a folder is not worth a failed boot
            logger.warning("Could not create default folder %s: %s", path, exc)
    if created:
        logger.info("Created %d starting folders", len(created))
    return created


VAULT_MARKER = ".archivum-wiki"


class VaultOwnershipError(RuntimeError):
    """The vault directory already belongs to a different wiki."""


def claim_vault_dir(wiki_dir: Path, *, wiki_id: str) -> None:
    """Record which wiki owns this directory, and refuse to share it.

    Page paths are `wiki_dir/<slug>.md` — the wiki appears nowhere in them. That
    is deliberate: the vault is a plain markdown folder people open in other
    editors, and burying it under an id nobody types would cost the person using
    it something real. The cost is that two backends configured with different
    `WIKI_ID` but the same `WIKI_DIR` would quietly overwrite each other's
    files, and reconciliation would then persist one wiki's content into the
    other's rows.

    Nothing in the app can create a second wiki, so this is a misconfiguration
    rather than an attack — but a misconfiguration that loses pages silently is
    worth a loud failure at startup.

    A directory with no marker is adopted, so upgrading never needs a migration.
    """
    marker = wiki_dir / VAULT_MARKER
    try:
        owner = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        owner = ""
    except OSError as exc:  # noqa: BLE001 - unreadable marker is not proof of conflict
        logger.warning("Could not read %s: %s", marker, exc)
        return

    if owner and owner != wiki_id:
        raise VaultOwnershipError(
            f"{wiki_dir} holds the vault for wiki {owner!r}, but this process is "
            f"configured as {wiki_id!r}. Two wikis cannot share one vault "
            f"directory: page paths do not carry the wiki, so they would "
            f"overwrite each other. Point WIKI_DIR somewhere else."
        )

    if owner != wiki_id:
        try:
            wiki_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{wiki_id}\n", encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - a read-only vault still reads
            logger.warning("Could not claim %s: %s", marker, exc)
