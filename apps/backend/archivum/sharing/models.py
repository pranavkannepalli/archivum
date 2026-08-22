"""Typed models for principals, grants, holds, and resolved access."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

SubjectKind = Literal["principal", "link"]
ShareRole = Literal["viewer", "commenter"]

SUBJECT_KINDS: frozenset[str] = frozenset({"principal", "link"})
SHARE_ROLES: frozenset[str] = frozenset({"viewer", "commenter"})

# Ordered weakest first, so "can this subject comment?" is a comparison rather
# than a set membership test scattered across call sites.
ROLE_RANK: dict[str, int] = {"viewer": 0, "commenter": 1}


def hash_token(raw: str) -> str:
    """Hash a link or claim token for storage.

    Share tokens are bearer credentials, so they are stored hashed for the same
    reason `refresh_tokens.token_hash` is: a leaked database should not hand
    over working access to every share ever created.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Subject:
    """Who is asking — a named principal, or whoever holds a link."""

    kind: SubjectKind
    id: str

    @staticmethod
    def principal(principal_id: str) -> "Subject":
        return Subject(kind="principal", id=principal_id)

    @staticmethod
    def link(token_hash: str) -> "Subject":
        return Subject(kind="link", id=token_hash)

    @staticmethod
    def link_from_token(raw_token: str) -> "Subject":
        return Subject(kind="link", id=hash_token(raw_token))


class Principal(BaseModel):
    """A recipient. Deliberately not a `users` row — no password, no wiki role."""

    id: str
    wiki_id: str
    display_name: str
    claimed_at: str | None = None
    revoked: bool = False
    created_at: str = ""

    @property
    def initials(self) -> str:
        parts = [part for part in self.display_name.split() if part]
        if not parts:
            return "··"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Grant(BaseModel):
    """One subject's access to one resource."""

    id: str
    wiki_id: str
    subject_kind: SubjectKind
    subject_id: str
    resource_urn: str
    role: ShareRole
    include_cited: bool = False
    created_by: str
    created_at: str = ""
    expires_at: str | None = None
    revoked: bool = False


class Hold(BaseModel):
    """A resource withheld from a grant that would otherwise cover it."""

    grant_id: str
    resource_urn: str
    reason: str = "agent_authored"
    created_at: str = ""


class Access(BaseModel):
    """The resolver's answer: what this subject may do, and why."""

    resource_urn: str
    role: ShareRole
    grant_id: str
    # The ancestor folder the access came from, or None when granted directly.
    inherited_from: str | None = None

    def may_comment(self) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK["commenter"]


class ShareTarget(BaseModel):
    """A resource plus the citation decisions attached to sharing it."""

    resource_urn: str
    cited_urns: list[str] = Field(default_factory=list)
