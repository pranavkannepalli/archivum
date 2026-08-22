# Sharing: principals, grants, and links

Sharing lets an owner give somebody access to part of their vault without
giving them the vault. It replaces four unrelated mechanisms — anonymous page
links, whole-wiki invites, a global public toggle, and an unenforced
`visibility` column — with one primitive.

## The primitive

A **grant** is one subject's access to one resource:

```
(subject, resource_urn, role, expires_at, revoked)
```

The subject is either a **principal** (a named person) or a **link** (whoever
holds the token). Sharing with Alice and turning on "anyone with the link" are
the same record with a different subject, which is why they can sit in one
list, resolve through one code path, and be revoked the same way.

A principal is deliberately thin: a display name, a claim token, and a set of
grants. It is not a `users` row, carries no wiki role, and cannot log in.

| Table | Holds |
|---|---|
| `share_principals` | recipients and their one-time claim hash |
| `share_grants` | who may see what, at what role |
| `share_holds` | resources withheld from a grant pending review |
| `share_views` | frozen query snapshots and saved live queries |

## Resource URNs

Everything shareable addresses as `{kind}:{wiki_id}:{local_id}`
(`archivum/sharing/urn.py`):

```
entry:default:people/alice
folder:default:people
asset:default:memory:skill:deploy
scope:default:person:self
view:default:vw_01H...
```

Parsing splits on the first two colons only, because asset and scope ids are
themselves colon-separated.

The wiki sits in the middle so a urn is self-tenanting. The API also accepts
`resource_kind` + `resource_id` and fills the wiki in from the session, so the
browser never needs to know its own tenant id to share the page it is showing.

**Inheritance is a string prefix operation.** An entry's folder ancestors come
from splitting its local id on `/`, nearest first, so resolution is a handful
of comparisons rather than a recursive walk. `entry` and `folder` inherit;
`asset`, `scope`, `view`, and `source` do not — they sit outside the vault
tree, so a grant on the root folder must not sweep them in.

## Resolution

`sharing/resolver.py` answers one question, and every recipient-facing read
goes through it. First match wins:

1. A malformed urn resolves to nothing.
2. Revoked or expired grants, and revoked principals, do not count.
3. A **hold** on the resource withholds it — whichever grant would have covered
   it. If one grant holds a resource, a second broader grant cannot defeat
   that; otherwise a hold would be a suggestion rather than a gate.
4. A grant on the resource itself wins.
5. Otherwise the nearest ancestor folder grant wins.

## The review gate

Shared folders are live: drop a note into one and the recipient sees it,
because filing something there is a deliberate act. But agents write into this
vault unattended — `pages.authored_by` defaults to `agent`, and the ingest
pipeline creates records on its own. So when an *agent* files something into a
shared container, a row goes into `share_holds` and the recipient sees nothing
until the owner approves it.

Holds surface in the stream (`surfaces/ShareHolds.tsx`) next to the existing
review queue, because they are the same kind of decision: nothing enters, and
nothing leaves, without you.

## Enforcement

Enforcement is **namespace separation, not per-route checks**.

- `get_current_user` rejects any token whose type or role marks it a share
  recipient. A recipient cannot reach `/api/entries`, `/api/pages`,
  `/api/memory`, MCP, or any other owner route — not because each one checks,
  but because the shared authentication dependency refuses the token.
- `/api/shared/*` is the only router that accepts recipient identity, and every
  handler in it resolves through `resolver.resolve` first.
- `/api/sharing/*` is owner-side management behind `require_writer`.

A new owner route therefore cannot leak to a recipient by omission. The
reviewable surface is one file rather than nine. `tests/api/test_shared_api.py`
asserts the invariant directly against every owner router.

## Roles

| Role | Can |
|---|---|
| `viewer` | read what was shared, follow shared citations |
| `commenter` | the above, plus propose changes |

A commenter's contribution becomes a `MemorySuggestion` with `status='pending'`
carrying `author_principal_id` — the same review queue that already holds agent
proposals. No external principal writes to the vault. There is no editor role.

## Citations

Sharing a memory asset ships its body, summary, and citation *titles*. A cited
source becomes reachable only if it was granted in its own right; otherwise it
renders as a named but unlinked entry. One share of a distilled memory must not
quietly drag its evidence along, and a citation that links into a login wall is
worse than one that does not link at all.

## Claiming

There is no SMTP in Archivum, so invitations are out-of-band. Creating a
person-share mints a claim URL the owner sends over their own channel. The
token is the credential, works once, and is stored hashed. Claiming sets a
recipient session cookie plus a CSRF token, since recipient writes go through
the same double-submit check as the owner's.

Claim attempts are limited **per token**, not per IP: `get_client_ip` collapses
everyone behind a proxy into one bucket, so IP keying would let one recipient's
retries lock another's claim out.

## Denials

Unknown, revoked, expired, and held all return the same flat 404 with the same
body. A recipient must not be able to distinguish "does not exist" from "exists
but is not yours" — that difference is an oracle for probing the vault.

## Legacy links

`share_links` rows are migrated into grants on every boot
(`sharing/migration.py`), idempotently and preserving their revoked flag and
expiry. A URL handed out before any of this existed still resolves, through
`GET /api/shared/by-token/{token}`.
