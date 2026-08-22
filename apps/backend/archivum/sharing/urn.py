"""Resource URNs — the addressing scheme every grant is written against.

A urn is `{kind}:{wiki_id}:{local_id}`. The wiki segment sits in the middle so
that a urn is self-tenanting: two wikis can hold the same slug without their
grants ever colliding, and no caller has to remember to pass a wiki id
alongside.

The shape is chosen so that *inheritance is a string operation*. An entry's
folder ancestors fall out of splitting its local id on `/`, which means
resolving "does a grant on `work/` cover `work/notes/alice`?" is a handful of
prefix comparisons rather than a recursive walk of the folder table.
"""

from __future__ import annotations

from dataclasses import dataclass

# Things that live in the vault tree inherit access from the folders above
# them. Assets, scopes, and views do not — they sit outside the file hierarchy,
# so a grant on the vault root must not sweep them in.
TREE_KINDS: frozenset[str] = frozenset({"entry", "folder"})
FLAT_KINDS: frozenset[str] = frozenset({"source", "asset", "scope", "view"})
KINDS: frozenset[str] = TREE_KINDS | FLAT_KINDS

ROOT_LOCAL_ID = ""


class UrnError(ValueError):
    """Raised for a urn that is malformed, unknown, or unsafe."""


@dataclass(frozen=True)
class ResourceUrn:
    kind: str
    wiki_id: str
    local_id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.wiki_id}:{self.local_id}"


def _clean_local_id(local_id: str) -> str:
    """Collapse slashes and refuse anything that could climb out of the vault.

    Local ids reach the filesystem indirectly (a slug is a path under the vault
    root), so a `..` segment here is a traversal waiting to happen. Rejecting
    it at the addressing layer means no downstream consumer has to.
    """
    segments = [segment for segment in local_id.split("/") if segment != ""]
    for segment in segments:
        if segment in {".", ".."}:
            raise UrnError(f"Unsafe path segment in urn local id: {local_id!r}")
    return "/".join(segments)


def build(kind: str, wiki_id: str, local_id: str) -> str:
    """Return a normalised urn string, raising `UrnError` on bad input."""
    if kind not in KINDS:
        raise UrnError(f"Unknown resource kind: {kind!r}")
    if not wiki_id or ":" in wiki_id:
        raise UrnError(f"Invalid wiki id in urn: {wiki_id!r}")

    if kind in TREE_KINDS:
        local_id = _clean_local_id(local_id)
    elif not local_id:
        raise UrnError(f"A {kind} urn needs a local id")

    return f"{kind}:{wiki_id}:{local_id}"


def parse(urn: str) -> ResourceUrn:
    """Split a urn into its parts.

    The split stops after the wiki segment: asset and scope ids are themselves
    colon-separated (`memory:skill:deploy`, `person:self`), so an unbounded
    split would silently truncate them.
    """
    parts = urn.split(":", 2)
    if len(parts) != 3:
        raise UrnError(f"Malformed urn: {urn!r}")

    kind, wiki_id, local_id = parts
    normalised = build(kind, wiki_id, local_id)
    if normalised != urn:
        raise UrnError(f"Urn is not in normal form: {urn!r} (expected {normalised!r})")

    return ResourceUrn(kind=kind, wiki_id=wiki_id, local_id=local_id)


def folder_urn(wiki_id: str, path: str) -> str:
    """Address a folder, including the vault root as an empty local id."""
    return build("folder", wiki_id, path)


def ancestors(urn: str) -> list[str]:
    """Return the folder urns that a resource inherits from, nearest first.

    Nearest-first ordering is what lets the resolver stop at the first match and
    have that be the most specific grant.
    """
    resource = parse(urn)
    if resource.kind not in TREE_KINDS:
        return []

    segments = resource.local_id.split("/") if resource.local_id else []
    # The last segment always names the resource itself — a folder's own name,
    # or an entry's filename — so ancestry starts one level up either way.
    depth = len(segments) - 1

    return [
        folder_urn(resource.wiki_id, "/".join(segments[:index]))
        for index in range(depth, -1, -1)
    ]
