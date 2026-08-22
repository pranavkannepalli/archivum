"""Owner-centered ontology helpers for the canonical knowledge store."""

from __future__ import annotations

from archivum.knowledge.models import Citation, KnowledgeObject, KnowledgeRelationship
from archivum.knowledge.repository import KnowledgeRepository


SELF_ID = "person:self"
SELF_SCOPE = "person:self"

PERSONAL_RELATIONSHIP_TYPES = frozenset(
    {
        "owns_project",
        "authored_thought",
        "cares_about",
        "decided",
        "works_on",
        "knows_person",
        "saved_source",
        "asked_question",
        "uses_code",
        # Work the owner did: a captured session, joined to what it changed.
        "did_work",
        # Governed memory assets.
        "owns_asset",
        "remembers",
        "learned_skill",
        "describes_self",
    }
)


async def ensure_personal_root(
    repo: KnowledgeRepository, *, display_name: str = "Me", wiki_id: str = "default"
) -> KnowledgeObject:
    """Create or update the owner node and return its canonical representation."""
    root = KnowledgeObject(
        id=SELF_ID,
        kind="person",
        label=display_name,
        scope=SELF_SCOPE,
        confidence=1.0,
        extraction_method="USER_AUTHORED",
        citations=[
            Citation(
                source_id=SELF_ID,
                chunk_id=SELF_ID,
                span_start=None,
                span_end=None,
                quote=display_name,
            )
        ],
        properties={"is_owner": True},
    )
    await repo.upsert_object(root)
    return root


async def link_to_self(
    repo: KnowledgeRepository,
    object_id: str,
    rel_type: str,
    *,
    citation: Citation,
    confidence: float = 1.0,
) -> KnowledgeRelationship:
    """Create or update a typed, cited relationship from the owner to an object."""
    if rel_type not in PERSONAL_RELATIONSHIP_TYPES:
        raise ValueError(f"Unsupported personal relationship type: {rel_type}")

    root = await repo.get_object(SELF_ID)
    if root is None:
        raise ValueError("Personal root must exist before creating relationships")
    target = await repo.get_object(object_id)

    relationship = KnowledgeRelationship(
        id=f"rel:{SELF_ID}:{rel_type}:{object_id}",
        src_id=SELF_ID,
        dst_id=object_id,
        rel_type=rel_type,
        scope=target.scope if target is not None else root.scope,
        confidence=confidence,
        extraction_method="USER_AUTHORED",
        citations=[citation],
        properties={},
    )
    await repo.upsert_relationship(relationship)
    return relationship
