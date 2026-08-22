from __future__ import annotations

import re
from dataclasses import dataclass, field

from archivum.archgraph.models import CodeEdge, CodeNode, Extraction
from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provenance:
    chunk_id: str
    span: str               # e.g. "L5-L6" from the node/edge source_location
    extraction_method: str  # ExtractionMethod value string


@dataclass(frozen=True)
class CandidateEntity:
    id: str
    kind: str
    name: str
    scope: str
    confidence: float
    extraction_method: str
    provenance: list[Provenance]
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateArtifact:
    id: str
    kind: str
    name: str
    scope: str
    confidence: float
    extraction_method: str
    provenance: list[Provenance]
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateRelationship:
    id: str
    src_id: str
    dst_id: str
    rel_type: str
    scope: str
    confidence: float
    extraction_method: str
    provenance: list[Provenance]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ARTIFACT_KINDS: frozenset[str] = frozenset({"file", "repo", "commit", "pr", "test", "deployment"})


def _slug(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _rel_id(src: str, rel: str, dst: str) -> str:
    return f"{_slug(src)}__{_slug(rel)}__{_slug(dst)}"


def _span_bounds(span: str) -> tuple[int | None, int | None]:
    """Convert source spans such as ``L5-L6`` into canonical line bounds."""
    numbers = [int(value) for value in re.findall(r"\d+", span)]
    if not numbers:
        return None, None
    return numbers[0], numbers[-1]


def _citations(provenance: list[Provenance]) -> list[Citation]:
    return [
        Citation(
            source_id=item.chunk_id,
            chunk_id=item.chunk_id,
            span_start=_span_bounds(item.span)[0],
            span_end=_span_bounds(item.span)[1],
            quote=item.span,
        )
        for item in provenance
    ]


def candidate_to_knowledge_object(
    candidate: CandidateEntity | CandidateArtifact,
) -> KnowledgeObject:
    """Adapt a deterministic code candidate to canonical knowledge storage."""
    return KnowledgeObject(
        id=candidate.id,
        kind=candidate.kind,
        label=candidate.name,
        scope=candidate.scope,
        confidence=candidate.confidence,
        extraction_method=candidate.extraction_method,
        citations=_citations(candidate.provenance),
        properties={"source_scope": candidate.scope, **getattr(candidate, "properties", {})},
    )


def candidate_to_knowledge_relationship(
    candidate: CandidateRelationship,
) -> KnowledgeRelationship:
    """Adapt a deterministic code relationship to canonical knowledge storage."""
    return KnowledgeRelationship(
        id=candidate.id,
        src_id=candidate.src_id,
        dst_id=candidate.dst_id,
        rel_type=candidate.rel_type,
        scope=candidate.scope,
        confidence=candidate.confidence,
        extraction_method=candidate.extraction_method,
        citations=_citations(candidate.provenance),
        properties={"source_scope": candidate.scope},
    )


def knowledge_to_candidate_object(object_: KnowledgeObject) -> CandidateEntity:
    """Adapt a canonical object for legacy graph export consumers."""
    return CandidateEntity(
        id=object_.id,
        kind=object_.kind,
        name=object_.label,
        scope=object_.scope,
        confidence=object_.confidence,
        extraction_method=object_.extraction_method,
        provenance=[
            Provenance(
                chunk_id=citation.chunk_id,
                span=citation.quote or "L0",
                extraction_method=object_.extraction_method,
            )
            for citation in object_.citations
        ],
    )


def knowledge_to_candidate_relationship(
    relationship: KnowledgeRelationship,
) -> CandidateRelationship:
    """Adapt a canonical relationship for legacy graph export consumers."""
    return CandidateRelationship(
        id=relationship.id,
        src_id=relationship.src_id,
        dst_id=relationship.dst_id,
        rel_type=relationship.rel_type,
        scope=relationship.scope,
        confidence=relationship.confidence,
        extraction_method=relationship.extraction_method,
        provenance=[
            Provenance(
                chunk_id=citation.chunk_id,
                span=citation.quote or "L0",
                extraction_method=relationship.extraction_method,
            )
            for citation in relationship.citations
        ],
    )


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

def map_extraction(
    ext: Extraction,
    *,
    scope: str,
    chunk_id: str,
) -> list[object]:
    """Return a flat list of CandidateEntity | CandidateArtifact | CandidateRelationship."""
    results: list[object] = []

    for node in ext.nodes:
        prov = [Provenance(chunk_id=chunk_id, span=node.source_location, extraction_method="EXTRACTED")]
        if node.kind in _ARTIFACT_KINDS:
            results.append(
                CandidateArtifact(
                    id=node.id,
                    kind=node.kind,
                    name=node.label,
                    scope=scope,
                    confidence=1.0,
                    extraction_method="EXTRACTED",
                    provenance=prov,
                    properties=dict(node.properties),
                )
            )
        else:
            results.append(
                CandidateEntity(
                    id=node.id,
                    kind=node.kind,
                    name=node.label,
                    scope=scope,
                    confidence=1.0,
                    extraction_method="EXTRACTED",
                    provenance=prov,
                    properties=dict(node.properties),
                )
            )

    for edge in ext.edges:
        method_str = edge.method.value
        prov = [Provenance(chunk_id=chunk_id, span=edge.source_location, extraction_method=method_str)]
        results.append(
            CandidateRelationship(
                id=_rel_id(edge.source, edge.relation, edge.target),
                src_id=edge.source,
                dst_id=edge.target,
                rel_type=edge.relation,
                scope=scope,
                confidence=edge.confidence,
                extraction_method=method_str,
                provenance=prov,
            )
        )

    return results
