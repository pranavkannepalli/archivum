from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExtractionMethod(str, Enum):
    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class CodeNode:
    id: str
    label: str
    kind: str            # symbol|module|type|package|file
    source_file: str
    source_location: str  # "L42" or "L42-L88"
    # What the extractor learned beyond identity — a signature, a summary. This
    # is what turns a retrieved record from a pointer into something an agent
    # can act on without opening the file first.
    #
    # Excluded from comparison so the node stays hashable and keeps its identity
    # in (id, kind, file, location): two records of the same symbol are the same
    # record whether or not one of them also captured a docstring.
    properties: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class CodeEdge:
    source: str
    target: str
    relation: str        # calls|imports|inherits|depends_on|references
    method: ExtractionMethod
    source_file: str
    source_location: str
    confidence: float = 1.0


@dataclass(frozen=True)
class Extraction:
    nodes: list[CodeNode]
    edges: list[CodeEdge]
    error: str | None = None
