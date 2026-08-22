"""The single question the rest of the system asks about sharing.

Everything a recipient can reach goes through `resolve`. Keeping it to one
function is deliberate: access checks scattered across handlers are checks that
can be forgotten, and a forgotten check here is a disclosure rather than a bug.
"""

from __future__ import annotations

from archivum.sharing.models import Access, Grant, Subject
from archivum.sharing.repository import SharingRepository
from archivum.sharing.urn import UrnError, ancestors, parse


async def resolve(
    repo: SharingRepository, subject: Subject, resource_urn: str
) -> Access | None:
    """Return the access *subject* has to *resource_urn*, or None.

    Rules, first match wins:

    1. A malformed urn resolves to nothing.
    2. Revoked/expired grants and revoked principals do not count.
    3. A hold on the resource withholds it, whichever grant would have covered it.
    4. A grant on the resource itself wins.
    5. Otherwise the nearest ancestor folder grant wins.
    """
    try:
        parse(resource_urn)
    except UrnError:
        return None

    grants = await repo.list_grants_for_subject(subject)
    if not grants:
        return None

    # Nearest first: the resource itself, then each folder above it.
    candidates = [resource_urn, *ancestors(resource_urn)]
    by_urn: dict[str, Grant] = {}
    for grant in grants:
        if grant.resource_urn in candidates:
            by_urn.setdefault(grant.resource_urn, grant)

    if not by_urn:
        return None

    # A hold applies to the resource, not to one path to it. If any grant that
    # could reach this resource is holding it, it stays hidden — otherwise a
    # second, broader grant would quietly defeat the review gate.
    held = await repo.held_urns(grant.id for grant in by_urn.values())
    if resource_urn in held:
        return None

    for candidate in candidates:
        grant = by_urn.get(candidate)
        if grant is None:
            continue
        return Access(
            resource_urn=resource_urn,
            role=grant.role,
            grant_id=grant.id,
            inherited_from=None if candidate == resource_urn else candidate,
        )

    return None


async def list_visible(repo: SharingRepository, subject: Subject) -> list[Access]:
    """Every resource explicitly granted to *subject*.

    This lists what was *shared*, not the full expansion of shared folders —
    enumerating a folder's members needs the page store, so the API layer
    composes that on top rather than the resolver reaching across stores.
    """
    grants = await repo.list_grants_for_subject(subject)
    held = await repo.held_urns(grant.id for grant in grants)

    return [
        Access(
            resource_urn=grant.resource_urn,
            role=grant.role,
            grant_id=grant.id,
        )
        for grant in grants
        if grant.resource_urn not in held
    ]
