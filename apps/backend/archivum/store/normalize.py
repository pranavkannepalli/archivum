"""Normalization: adapt existing parsers into a NormalizedDoc (text + mime)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from archivum.ingest.parsers import parse_source

_TYPE_TO_MIME: dict[str, str] = {
    "md": "text/markdown",
    "txt": "text/plain",
    "text": "text/plain",
    "rst": "text/x-rst",
    "log": "text/plain",
    "rtf": "application/rtf",
    "xml": "application/xml",
    "pdf": "application/pdf",
    "html": "text/html",
    "url": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "json": "application/json",
    "jsonl": "application/jsonl",
    "epub": "application/epub+zip",
    "code": "text/x-code",
    "eml": "message/rfc822",
    "mbox": "application/mbox",
    "archive": "application/zip",
    "image": "image/*",
    "audio": "audio/*",
    "video": "video/*",
}


@dataclass(frozen=True, slots=True)
class NormalizedDoc:
    text: str
    mime: str
    metadata: dict[str, Any]


def mime_for_doc_type(doc_type: str) -> str:
    """The mime a parser's `type` metadata stands for, defaulting to plain text."""
    return _TYPE_TO_MIME.get(doc_type.lower(), "text/plain")


async def normalize(origin_uri: str) -> NormalizedDoc:
    """Parse `origin_uri` into normalized text and a mime type."""
    parsed = await parse_source(origin_uri)
    return NormalizedDoc(
        text=parsed.text,
        mime=mime_for_doc_type(str(parsed.metadata.get("type", ""))),
        metadata=dict(parsed.metadata),
    )
