"""Unified activity stream.

The redesigned home screen is a single reverse-chronological feed rather than a
dashboard, so it needs page edits, agent suggestions, ingests, and newly
distilled memory in one ordered list. Each source is queried for a capped slice,
merged here, and truncated — the client never pages four cursors by hand.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from archivum.auth import CurrentUser, get_current_user, require_writer
from archivum.config import Settings, get_settings
from archivum.db import sqlite
from archivum.knowledge.suggestions import SuggestionRepository, wiki_scope
from archivum.knowledge.repository import KnowledgeRepository
from archivum.memory.registry import MemoryAssetRegistry
from archivum.tasks import list_open_tasks, set_task_done
from archivum.timestamps import normalise_timestamp

router = APIRouter(prefix="/api", tags=["activity"])

# Sessions and fixes have no time-ordered index of their own yet, so the feed
# scans them. Bounded the same way the audit and the projection rebuild are.
_WORK_SCAN_LIMIT = 2_000

ActivityKind = Literal[
    "page_created",
    "page_edited",
    "suggestion",
    "ingest",
    "memory",
    # Work that happened. Capture runs automatically, and capture you cannot see
    # is hard to tell apart from no capture at all.
    "session",
    "fix",
]

# Who caused the item to exist. Drives the accent edge in the stream: agent work
# is marked, your own work is not.
ActivityActor = Literal["you", "agent", "system"]


class ActivityItem(BaseModel):
    id: str
    kind: ActivityKind
    at: str
    title: str
    summary: str = ""
    actor: ActivityActor = "system"
    slug: str | None = None
    needs_review: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class OpenTask(BaseModel):
    text: str
    slug: str
    page_title: str
    line: int


class ActivityFeed(BaseModel):
    items: list[ActivityItem]
    next_before: str | None = None
    pending_review: int = 0
    # Not part of the timeline — what is still outstanding. The stream is where
    # you would look for it, so it rides along rather than needing its own trip.
    open_tasks: list[OpenTask] = Field(default_factory=list)


def _parse_tags(raw: object) -> list[str]:
    """Tags live in a JSON text column; the stream wants a real list."""
    if isinstance(raw, list):
        return [str(tag) for tag in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [str(tag) for tag in parsed]
    return []


def _page_items(rows: list[dict[str, Any]]) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    for row in rows:
        created = normalise_timestamp(row.get("created_at"))
        updated = normalise_timestamp(row.get("updated_at")) or created
        is_new = bool(created) and created == updated
        by_agent = (row.get("authored_by") or "user") == "agent"
        items.append(
            ActivityItem(
                id=f"page:{row['slug']}:{updated}",
                kind="page_created" if is_new else "page_edited",
                at=updated,
                title=row.get("title") or row["slug"],
                actor="agent" if by_agent else "you",
                slug=row["slug"],
                payload={"tags": _parse_tags(row.get("tags"))},
            )
        )
    return items


def _slug_from_target(target_id: str, wiki_id: str) -> str | None:
    """Page suggestions carry a `page:{wiki_id}:{slug}` target; others don't."""
    prefix = f"page:{wiki_id}:"
    return target_id[len(prefix) :] if target_id.startswith(prefix) else None


def _suggestion_items(suggestions: list[Any], wiki_id: str) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    for s in suggestions:
        at = normalise_timestamp(s.updated_at) or normalise_timestamp(s.created_at)
        if not at:
            # Rows written before timestamps were exposed. Skipping keeps the
            # feed honestly ordered rather than pinning them to the epoch.
            continue
        target = s.target_id or ""
        items.append(
            ActivityItem(
                id=f"suggestion:{s.id}",
                kind="suggestion",
                at=at,
                title=s.proposed_markdown.strip().splitlines()[0][:140]
                if s.proposed_markdown.strip()
                else s.suggestion_type.replace("_", " "),
                summary=s.rationale,
                actor="agent",
                slug=_slug_from_target(target, wiki_id),
                needs_review=s.status == "pending",
                payload={
                    "suggestion_id": s.id,
                    "suggestion_type": s.suggestion_type,
                    "status": s.status,
                    "target_id": s.target_id,
                    "scopes": s.proposed_scopes,
                    "conflicts": s.conflicts,
                    "duplicates": s.duplicates,
                    "citations": s.citations,
                    "proposed_markdown": s.proposed_markdown,
                },
            )
        )
    return items


def _ingest_items(rows: list[dict[str, Any]]) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    for row in rows:
        created = normalise_timestamp(row.get("created_at"))
        pages = (row.get("pages_created") or 0) + (row.get("pages_updated") or 0)
        items.append(
            ActivityItem(
                id=f"ingest:{row['id']}",
                kind="ingest",
                at=created,
                title=row.get("source_path") or row.get("source_type") or "Ingest",
                summary=row.get("error") or "",
                actor="you",
                payload={
                    "source_type": row.get("source_type"),
                    "status": row.get("status"),
                    "pages_created": row.get("pages_created") or 0,
                    "pages_updated": row.get("pages_updated") or 0,
                    "pages_touched": pages,
                },
            )
        )
    return items


def _work_items(objects: list[Any]) -> list[ActivityItem]:
    """Sessions and fixes: the record of what was actually done.

    Both are dated by when the work started rather than when the record was
    written, so a session that took an hour sits where it belongs in the day.
    """
    items: list[ActivityItem] = []
    for object_ in objects:
        at = normalise_timestamp(object_.properties.get("started_at", ""))
        if not at:
            continue
        if object_.kind == "session":
            items.append(
                ActivityItem(
                    id=f"session:{object_.id}",
                    kind="session",
                    at=at,
                    title=object_.label,
                    summary=", ".join(
                        str(path).rsplit("/", 1)[-1]
                        for path in object_.properties.get("touched_paths", [])[:4]
                    ),
                    actor="agent",
                    payload={
                        "session_kind": object_.properties.get("kind", "unknown"),
                        "touched_paths": object_.properties.get("touched_paths", []),
                        "source_id": object_.citations[0].source_id
                        if object_.citations
                        else "",
                    },
                )
            )
        elif object_.kind == "fix":
            items.append(
                ActivityItem(
                    id=f"fix:{object_.id}",
                    kind="fix",
                    at=at,
                    title=str(object_.properties.get("symptom", "")) or object_.label,
                    summary=str(object_.properties.get("diagnosis", "")),
                    actor="agent",
                    payload={
                        "verified_by": object_.properties.get("verified_by", ""),
                        "changed_paths": object_.properties.get("changed_paths", []),
                        "confidence": object_.confidence,
                    },
                )
            )
    return items


def _memory_items(assets: list[Any]) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    for asset in assets:
        at = normalise_timestamp(asset.updated_at) or normalise_timestamp(asset.created_at)
        if not at:
            continue
        items.append(
            ActivityItem(
                id=f"memory:{asset.id}:{asset.version}",
                kind="memory",
                at=at,
                title=asset.name or asset.summary[:140],
                summary=asset.summary,
                actor="system",
                slug=asset.page_slug,
                payload={
                    "asset_id": asset.id,
                    "layer": asset.layer,
                    "asset_type": asset.asset_type,
                    "status": asset.status,
                    "scope": asset.scope,
                    "disputed": bool(asset.conflict_lineage),
                },
            )
        )
    return items


# The feed is ordered by (timestamp, id) descending, so a cursor has to carry
# both halves of that key to resume exactly where the last page stopped. It is
# base64url-encoded because the raw key contains characters a query string
# mangles: an ISO offset's "+" decodes back as a space.
_CURSOR_SEP = "\x1f"


def _source_anchor(
    cursor: tuple[str, str] | None, prefix: str
) -> tuple[str | None, str | None, bool]:
    """Where a single source should resume, given the merged cursor.

    The feed orders on (timestamp, activity id) descending, and activity ids
    are prefixed by source. For records tied on the cursor's timestamp, whether
    a source still has rows to emit depends on how its prefix compares with the
    cursor's:

    * same source  -> resume strictly below the cursor's own id
    * sorts after   -> every tied row is still ahead, keep them all
    * sorts before  -> its tied rows were already emitted, skip that second

    Returns (before_at, before_id, exclude_tied).
    """
    if not cursor:
        return (None, None, False)
    at, item_id = cursor
    cursor_prefix = f"{item_id.split(':', 1)[0]}:" if ":" in item_id else ""
    if cursor_prefix == prefix:
        return (at, _source_id_from_item(item_id, prefix, at), False)
    if cursor_prefix > prefix:
        return (at, None, False)
    return (at, None, True)


def _source_id_from_item(item_id: str, prefix: str, at: str) -> str | None:
    """Recover the store's own id from an activity id."""
    body = item_id[len(prefix) :]
    # Return the key the source's SQL compares on, which is the whole tail of
    # the activity id — for memory that includes the version suffix.
    return body or None


def _page_slug_from_id(item_id: str, at: str) -> str | None:
    """Page item ids are `page:{slug}:{timestamp}`; the SQL cursor needs the slug.

    Split using the timestamp we already hold rather than by separator: an ISO
    timestamp contains colons, so `rsplit(":")` would cut inside it and yield a
    slug that matches nothing.
    """
    suffix = f":{at}"
    if not item_id.startswith("page:") or not item_id.endswith(suffix):
        return None
    return item_id[len("page:") : -len(suffix)] or None


def _key(item: ActivityItem) -> tuple[str, str]:
    return (item.at, item.id)


def _encode_cursor(item: ActivityItem) -> str:
    raw = f"{item.at}{_CURSOR_SEP}{item.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(raw: str | None) -> tuple[str, str] | None:
    """Decode an opaque cursor, tolerating a bare timestamp.

    A plain timestamp is what older links carry, and what a human poking at the
    API is most likely to type; it still pages, just without tie-breaking.
    """
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return (raw, "")
    at, sep, item_id = decoded.partition(_CURSOR_SEP)
    return (at, item_id) if sep else (at, "")


@router.get("/activity", response_model=ActivityFeed)
async def get_activity(
    limit: int = Query(default=40, ge=1, le=200),
    before: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> ActivityFeed:
    """Merged, reverse-chronological feed of everything that happened."""
    wiki_id = current_user.wiki_id
    cursor = _decode_cursor(before)
    # Over-fetch each source so the merge has enough candidates to fill `limit`
    # even when one source dominates a time window.
    slice_size = min(limit * 2, 200)

    at = cursor[0] if cursor else None
    # Anchor every source at the cursor. Without this each keeps returning its
    # newest rows, and pagination stalls on any run of records sharing a
    # timestamp — which a batch import produces routinely.
    page_at, _page_id, page_skip_tied = _source_anchor(cursor, "page:")
    pages = await sqlite.list_recent_pages(
        wiki_id,
        limit=slice_size,
        before_inclusive=page_at,
        before_slug=_page_slug_from_id(cursor[1], cursor[0]) if cursor else None,
        exclude_tied=page_skip_tied,
    )

    ingest_at, ingest_id, ingest_skip_tied = _source_anchor(cursor, "ingest:")
    ingests = await sqlite.list_ingest_logs(
        wiki_id,
        limit=slice_size,
        before_inclusive=ingest_at,
        before_id=ingest_id,
        exclude_tied=ingest_skip_tied,
    )

    sugg_at, sugg_id, sugg_skip_tied = _source_anchor(cursor, "suggestion:")
    asset_at, asset_id, asset_skip_tied = _source_anchor(cursor, "memory:")

    async with sqlite.get_db() as conn:
        repo = SuggestionRepository(conn)
        # Scoped: an unfiltered listing would serialise every wiki's proposals
        # into this feed.
        suggestions = await repo.list_suggestions(
            status=None,
            before_inclusive=sugg_at,
            before_id=sugg_id,
            exclude_tied=sugg_skip_tied,
            **wiki_scope(wiki_id),
        )
        # Counted separately: `suggestions` is anchored at the cursor, so
        # deriving the badge from it would undercount on later pages.
        pending = await repo.list_suggestions(status="pending", **wiki_scope(wiki_id))
        assets = await MemoryAssetRegistry(conn).list_assets(
            wiki_id=wiki_id,
            limit=slice_size,
            before_inclusive=asset_at,
            before_id=asset_id,
            exclude_tied=asset_skip_tied,
        )
        # Sessions and fixes are canonical records rather than rows, so they are
        # read whole and filtered here; the feed's cursor still orders them.
        knowledge = KnowledgeRepository(conn)
        work = [
            object_
            for object_ in await knowledge.list_objects(
                scope=f"wiki:{wiki_id}", limit=_WORK_SCAN_LIMIT
            )
            if object_.kind in ("session", "fix")
        ]

    items = (
        _page_items(pages)
        + _suggestion_items(suggestions, wiki_id)
        + _ingest_items(ingests)
        + _memory_items(assets)
        + _work_items(work)
    )
    if cursor:
        items = [item for item in items if item.at and _key(item) < cursor]

    items.sort(key=_key, reverse=True)
    window = items[:limit]
    # The cursor carries the whole ordering key. A timestamp alone would skip
    # every record tied with the boundary, and ties are common: a batch import
    # writes many rows in the same second.
    next_before = _encode_cursor(window[-1]) if len(items) > limit and window else None

    # Only on the first page: outstanding work is a standing list, not part of
    # the timeline, and repeating it under every scroll would be noise.
    open_tasks = (
        [
            OpenTask(
                text=task.text,
                slug=task.slug,
                page_title=task.page_title,
                line=task.line,
            )
            for task in await list_open_tasks(wiki_id=wiki_id)
        ]
        if cursor is None
        else []
    )

    return ActivityFeed(
        items=window,
        next_before=next_before,
        pending_review=len(pending),
        open_tasks=open_tasks,
    )


class ToggleTaskRequest(BaseModel):
    slug: str
    line: int
    done: bool


@router.post("/tasks/toggle", response_model=OpenTask)
async def toggle_task(
    body: ToggleTaskRequest,
    current_user: CurrentUser = Depends(require_writer),
    settings: Settings = Depends(get_settings),
) -> OpenTask:
    """Check or uncheck a task by rewriting its line in the page.

    The file is the source of truth, so this edits the markdown rather than
    updating a record beside it — which is what keeps a task the same object
    whether you ticked it here or in your own editor.
    """
    try:
        task = await set_task_done(
            slug=body.slug,
            line=body.line,
            done=body.done,
            wiki_id=current_user.wiki_id,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": "not_a_task"},
        ) from exc
    return OpenTask(
        text=task.text, slug=task.slug, page_title=task.page_title, line=task.line
    )
