# Sharing and Links — Design

Status: approved 2026-08-20. Supersedes the ad-hoc `share_links` mechanism.

## Problem

Archivum has four unrelated ways to let someone else see something, and none of
them lets you say "Alice can read this folder."

| Mechanism | Grain | Recipient | Revocable per person |
|---|---|---|---|
| `share_links` (`api/share.py`) | one page, or a frozen query snapshot | anonymous URL holder | no |
| `invite_tokens` (`api/auth.py`) | the entire wiki | a new full user account | no |
| `PUBLIC_WIKI_ENABLED` (`api/public.py`) | the entire wiki | the internet | n/a |
| `memory_assets.visibility` | one asset | nobody — unenforced | n/a |

The `visibility` column is decorative: nothing reads it. `share.py` never
consults it. Meanwhile `SharePage.tsx:112` renders citations as `/wiki/{slug}`
links, so a recipient who follows a source hits the login wall.

## Decisions

1. **Grants are the primitive.** A link-share is a grant whose subject is a
   link token; a person-share is a grant whose subject is a principal. One
   table, one resolver.
2. **Principals are thin.** A principal is a display name, a claim token, and a
   set of grants — not a `users` row. Recipients never become wiki members.
3. **Shareable:** entries, folders, memory assets, scopes, and live views. An
   asset shares its body, summary, and citation *titles*; each cited source is
   a separate explicit grant decision, surfaced as "3 sources cited, 0 shared".
4. **Membership is dynamic with a review gate.** New children of a shared
   container become visible immediately when a human filed them, and are
   **held** pending owner approval when an agent authored them.
5. **Roles: `viewer` and `commenter`.** A commenter's contribution becomes a
   `MemorySuggestion` with `status='pending'` in the existing review queue. No
   external principal ever writes to the vault directly.
6. **Claim links are out-of-band.** Archivum has no SMTP. Creating a
   person-share mints a claim URL the owner sends over their own channel.

## Resource URNs

Every shareable thing addresses as `{type}:{wiki_id}:{local_id}`:

```
entry:default:people/alice          a page-backed entry
source:default:src_01H...           a captured source with no page
folder:default:people               a container
asset:default:memory:skill:deploy   a governed memory asset
scope:default:person:self           a budgeted memory scope
view:default:vw_01H...              a saved live query
```

The scheme is load-bearing: **inheritance is a string prefix match**, not a
recursive walk. `entry:default:people/alice` has ancestors
`folder:default:people` and `folder:default:` (vault root), derived by
splitting the local id on `/`. Nearest ancestor wins, so a grant directly on an
entry overrides one inherited from its folder.

## Schema

Three tables, created by `archivum/sharing/schema.py` and applied from
`sqlite.init_db` alongside the other schema modules.

```sql
CREATE TABLE share_principals (
    id            TEXT PRIMARY KEY,        -- prn_<urlsafe>
    wiki_id       TEXT NOT NULL DEFAULT 'default',
    display_name  TEXT NOT NULL,
    claim_hash    TEXT,                    -- sha256 of the claim token; NULL once claimed
    claimed_at    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE share_grants (
    id            TEXT PRIMARY KEY,        -- grt_<urlsafe>
    wiki_id       TEXT NOT NULL DEFAULT 'default',
    subject_kind  TEXT NOT NULL CHECK (subject_kind IN ('principal', 'link')),
    subject_id    TEXT NOT NULL,           -- principal id, or sha256 of the link token
    resource_urn  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'commenter')),
    include_cited INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(subject_kind, subject_id, resource_urn)
);

CREATE TABLE share_holds (
    grant_id      TEXT NOT NULL,
    resource_urn  TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT 'agent_authored',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (grant_id, resource_urn)
);
```

`share_holds` is what makes decision 4 fail closed. A hold is a *withhold*: the
resolver denies a resource that an active grant would otherwise cover. Agent
writes insert holds; owner approval deletes them. A missing hold-insert can
only over-share if the agent write path forgets to call — so the hold is
inserted by the resolver's own `on_resource_created` hook, not by each caller.

Link tokens are stored hashed, matching how `refresh_tokens` already handles
`token_hash`. The raw token appears once, in the create response.

### Migration

Existing `share_links` rows become grants with `subject_kind='link'`:
`type='page'` → `entry:{wiki}:{target_id}`. `type='query'` rows keep their JSON
payload in a `view` row, preserving the frozen-snapshot behaviour they have
today. `/api/share/{token}` keeps working with unchanged response shape.

## Access resolution

`archivum/sharing/resolver.py` exposes one question:

```python
async def resolve(conn, subject: Subject, urn: str) -> Access | None
```

Order of evaluation, first match wins:

1. Grant revoked, expired, or principal revoked → `None`.
2. A hold exists for (grant, urn) → `None`.
3. A grant on the exact urn → that role.
4. A grant on the nearest ancestor folder → that role.
5. Otherwise → `None`.

`Access` carries the role and the granting `grant_id`, so every served response
can name why it was visible. Listing is the same predicate applied in reverse:
enumerate grants for the subject, expand folder grants to their members, then
subtract holds.

## Enforcement

The load-bearing choice is **namespace separation, not per-route checks**.

- `get_current_user` rejects tokens whose role is `recipient`. A recipient
  cannot reach `/api/entries`, `/api/pages`, `/api/memory`, MCP, or any other
  owner route — not because each one checks, but because the shared
  authentication dependency refuses the token.
- `/api/shared/*` is the only router that accepts recipient identity, via a
  separate `get_recipient` dependency. Every handler there resolves through
  `resolver.resolve` before returning anything.
- `/api/sharing/*` is owner-side management and requires `require_writer`.
- `/api/share/{token}` stays as the anonymous link viewer, now grant-backed.

A new owner-side route therefore cannot leak to recipients by omission. The
reviewable surface is one file, not nine.

## API

Owner side, `/api/sharing`:

```
POST   /principals                  {display_name} -> {principal, claim_url}
GET    /principals                  list, with grant counts
DELETE /principals/{id}             revoke principal and all its grants
POST   /grants                      {subject, resource_urn, role, expires_in_days, include_cited}
GET    /grants?resource_urn=        who can see this
DELETE /grants/{id}                 revoke
GET    /holds                       everything waiting on you
POST   /holds/{grant_id}/approve    {resource_urn}
DELETE /holds/{grant_id}            withhold permanently
```

Grant creation and listing also accept `resource_kind` + `resource_id` in place
of a full urn, with the wiki filled in from the session — the browser should not
need its own tenant id to share the page on screen.

Recipient side, `/api/shared`:

```
POST   /claim                       {claim_token} -> sets recipient + csrf cookies
GET    /                            what you can see
GET    /by-token/{token}            open a link without knowing its urn
GET    /resource?urn=&token=        one resource, resolved
POST   /comment                     {urn, text} -> MemorySuggestion(status='pending')
```

`/claim` is exempt from the CSRF double-submit check, as `/api/auth/refresh`
already is: it establishes the session, so it cannot present a token it has not
been issued. It is guarded by the unguessable claim token and the per-token
rate limit instead.

## UX

The app's stated doctrine (`AppShell.tsx:12`) is "nouns in the sidebar, verbs
are keyboard-first sheets." Sharing is a verb, so:

**ShareSheet** — `.overlay` → `.sheet`, matching `AskSheet`. Opens on `⌘⇧S` or
from the entry header. Top row is `.sheet-in` with the `users` icon and a
name field. Below, a `.sheet-list` of current access: one `.sheet-item` per
principal with an avatar, a role control, and a revoke `x`. Beneath that, a
"Anyone with the link" row using the `link` icon and the `.toggle` switch from
`SelfSurface.tsx:131`. Footer `.sheet-foot` carries `↵ share`, `esc close`,
and — when the resource is an asset — `3 sources cited · 0 shared` with a
one-click include.

**Shared with me / Shared by you** — nouns, so sidebar entries under a
`Shared` section, rendering with the existing `.row-i` list idiom.

**Holds** surface in the existing "Needs you" queue, reusing `.item.pending`
and the warn chip, phrased in the app's voice: "Archivum filed *Deploy notes*
into a folder Alice can read. Show her?"

Copy tone follows the established second-person register — "Alice can read
this. Nothing she writes lands without you."

**SharePage rewrite.** The current recipient viewer is written against the
legacy `index.css` HSL tokens and hand-rolls markdown with regex. It gets
rebuilt on `tokens.css` + `shell.css` so a shared page looks like Archivum, and
citations resolve to shared siblings or render as inert titles rather than
dead `/wiki/` links.

All new UI uses `styles/tokens.css` and `shell.css` semantic classes, not the
legacy shadcn variables, and the hand-rolled `Icon` set — which already ships
`link`, `users`, `lock`, `eye`, and `clock`.

## Error handling

- Unknown, revoked, or expired token → 404 with a neutral body. Never
  distinguish "wrong token" from "revoked token"; that difference is an oracle.
- Claim attempts are rate limited **per claim token**, not per IP.
  `rate_limit.get_client_ip` collapses everyone behind a proxy into one bucket,
  so IP keying would let one recipient's retries lock out another's claim.
- A held resource is indistinguishable from a nonexistent one to the recipient.
- Comment submission by a `viewer` → 403; the UI never offers the affordance.

## Testing

- `tests/sharing/test_urn.py` — parsing, ancestor derivation, malformed input.
- `tests/sharing/test_resolver.py` — the five resolution rules, nearest-ancestor
  override, holds, expiry, revoked principals.
- `tests/sharing/test_repository.py` — CRUD, token hashing, migration of
  existing `share_links` rows.
- `tests/api/test_sharing_api.py` — owner management routes.
- `tests/api/test_shared_api.py` — recipient routes, plus the negative case
  that matters most: **a recipient token is rejected by every owner router.**
- `tests/api/test_share.py` — existing suite must pass unchanged, proving the
  legacy link path survived migration.
- Frontend: `ShareSheet` interaction tests alongside the existing surface tests.

## Phases

All five landed on 2026-08-20.

1. ✅ `sharing/` module — urn, schema, repository, resolver. Tests first.
2. ✅ Owner API + migration of `share_links`; `tests/api/test_share.py` passed
   unchanged, since the legacy router was left in place rather than rewritten.
3. ✅ Recipient API + `get_current_user` recipient rejection.
4. ✅ Frontend: ShareSheet, shared surfaces, holds in the stream.
5. ✅ SharePage rebuilt on `tokens.css`.

Not built, and deliberately out of scope: an editor role, federated
instance-to-instance sharing, and enforcement of the legacy
`memory_assets.visibility` column, which remains decorative — sharing is
governed by grants, not by that field.
