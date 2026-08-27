---
title: "Coding-Agent Memory: Positioning and Multi-Machine Linking"
date: "2026-08-26"
status: "proposed"
supersedes_positioning_in:
  - "README.md"
  - "AGENTS.md"
related_docs:
  - "docs/2026-08-12-archivum-clean-memory-ux-backend-strategy.md"
  - "docs/architecture/agent-access.md"
  - "docs/architecture/memory-assets.md"
  - "docs/architecture/deploy.md"
---

# Coding-Agent Memory: Positioning and Multi-Machine Linking

## Decision

Archivum's public framing moves from *"self-hosted, Obsidian-style second brain"*
to **memory for coding agents that a human can audit**, and the product commits to
the multi-machine case as its default deployment shape.

The wedge is the developer with agents on more than one machine who wants repo
context and past fixes to follow them. Everyday-user surfaces are not dropped;
they are deferred, and they arrive later as additional *scopes* on the same
governed store rather than as a second product.

## Why the current framing has to change

Three problems, all visible in the repo today.

**It picks a losing fight.** `README.md:3` opens with "Obsidian-style". Obsidian
is a free download with no dependencies; Archivum is a git clone plus Docker plus
Qdrant plus Kuzu plus an LLM key. Framing the product as a variant of a thing that
installs in ten seconds invites the comparison Archivum cannot win, on the one axis
where it is weakest.

**It inherits a known churn curve.** The "second brain" category has a documented
failure mode: the tools demand upkeep, users describe setup as a second job, and
they revert to whatever demands nothing. Archivum's actual claim is the opposite of
upkeep — the evaluator prunes so the human does not have to — but the category name
carries the baggage regardless of what the product does.

**The differentiator is buried.** Review-gated, cited, scoped, versioned memory with
lineage, per-scope budgets, deterministic distillation, and `build_context_package`
is the product. In `README.md` it is item 7 of "How it works" and one bullet under
Features.

The internal strategy already says this.
`docs/2026-08-12-archivum-clean-memory-ux-backend-strategy.md:15-21` states the goal
as "it keeps my knowledge clean" and "my coding agents work better because they know
me and my context". `AGENTS.md:27` forbids saying so in public docs. That
contradiction is resolved here in favour of the strategy document.

## Positioning

**One-liner.**

> Archivum is memory your coding agents use and you can audit. What your agents
> learn about your repos — decisions, fixes, architecture — lives in one vault you
> own, reviewed and cited, and follows you to every machine you work on.

**Supporting claims, in priority order.**

1. Same memory on every machine. A `CLAUDE.md` lives on one laptop.
2. Less context for the same answer. Ranked retrieval instead of loading whole files.
3. You can see and correct what it knows. Review-gated promotion, citations, lineage.
4. Your data, your disk. Markdown on disk, self-hosted, no third party.

**Markdown-on-disk is demoted from headline to proof.** It stays prominent — it is
how the trust claim is substantiated — but it is not what the product *is*.

### The claim we must not make

"Faster recall than markdown files on disk" does not survive contact with reality.
Reading a local `CLAUDE.md` is a filesystem read; Archivum retrieval is a network
round trip plus ranking. Archivum is slower per call and that is fine, because the
comparison that matters is different: a local markdown file does not sync across
machines, does not rank, and forces whole-file loading into the context window.

Claim **less context and the same memory everywhere**. Never claim lower latency.

### What Archivum is not

Not a note-taking app. Not a wiki that happens to have an MCP server. Not a team
knowledge base — the store is centred on `person:self` (see the strategy doc's core
thesis), and team framing contradicts it.

## The blockers this design exists to remove

Two came out of the wedge decision; the third was found while confirming them.

### Blocker 1 — the documented linking path is single-machine

`README.md:75-84` and `docs/architecture/agent-access.md:10-20` both present the
stdio recipe as the way in:

```json
{ "command": "docker", "args": ["exec", "-i", "archivum-mcp", "..."] }
```

`docker exec` requires the container to be on the machine running the agent. A second
laptop cannot use this path at all. Its only option is HTTP/SSE, which
`agent-access.md:32-48` gates behind standing up a reverse proxy with TLS and
hand-configuring a bearer header per client per machine.

So the documented easy path is the case the wedge user does not have, and the case
they do have is undocumented beyond "put it behind the reverse proxy you already run".

`packages/archivum-cli/src/mcp.js` has the beginnings of a fix — `archivum mcp config`
— but it (a) prints JSON to stdout for the human to paste, (b) calls
`ensureRoot()` and so requires a repo checkout with a populated `.env`, which laptop
#2 does not have, and (c) hardcodes `http://localhost:8001/sse`.

### Blocker 2 — capture is blind to every machine except the server's

`docs/architecture/deploy.md:42-48`: "the container cannot see your laptop." Session
capture reads transcript directories mounted into the backend
(`docker-compose.yml:59`, `TRANSCRIPT_DIRS`). On a hosted deployment with agents
running on two laptops, neither laptop's transcripts are visible, so nothing is
captured automatically from either.

Explicit `record_work` and `recall_fix` calls still work — they are MCP tools and go
over the wire. But `docs/architecture/daily-use.md:19-21` makes the point that
"capture you cannot see is hard to tell apart from no capture at all", and right now
remote capture is invisible because it is not happening.

### Blocker 3 (security, found alongside) — auth fails open

`apps/backend/archivum/mcp/server.py:75` wires the bearer verifier only when
`mcp_api_key` is non-empty, and `_require_key()` at lines 111-113 returns early when
it is empty. An empty `MCP_API_KEY` therefore serves the entire vault unauthenticated
to anyone who can reach the port. `docker-compose.yml:26,99` default it to empty.

This is acceptable for stdio, where the transport is a local pipe into a process the
user already controls. It is not acceptable for HTTP/SSE, which is exactly the
transport the multi-machine case requires.

## Design

### 1. Device keys replace the single shared key

Today one `MCP_API_KEY` is shared by every client on every machine. Losing a laptop
means rotating the key and reconfiguring every other client.

Introduce per-device keys:

| Field | Notes |
|---|---|
| `id` | opaque |
| `name` | human label, e.g. "work laptop / Claude Code" |
| `key_hash` | hash, never the key itself |
| `created_at`, `last_seen_at` | `last_seen_at` updated on use |
| `revoked_at` | null when active |

Verification changes from a single `hmac.compare_digest` against
`settings.mcp_api_key` to a lookup over active device keys. The existing
`MCP_API_KEY`, when set, remains valid as a legacy key so current installs keep
working; it is presented in Settings as one more entry in the device list, named
"legacy shared key", with the same revoke affordance.

Settings gains a **Devices** view: what is linked, when each was last seen, and a
revoke button per row.

### 2. Pairing tokens

Linking a new machine should not require the user to handle the vault's long-lived
credential, and should not require them to have the repo.

- Settings issues a pairing token: `POST /api/mcp/pairing-tokens`.
- The token is **short-lived (15 minutes) and single-use**, and encodes the server's
  base URL alongside a one-time secret, so the user copies exactly one string.
- `POST /api/mcp/pairing/redeem` exchanges the secret for a newly minted device key
  plus server metadata (SSE URL, vault name, whether the skill should be installed).
- Redemption burns the token. A token that has been redeemed, or has expired, returns
  the same error — no oracle for which.

Rate-limit redemption per source address. Note the constraint recorded in
`docs/architecture/deploy.md:9-13`: health and auth traffic through the Cloudflare
tunnel shares a login rate limiter, so the pairing endpoint needs its own bucket
rather than inheriting that one.

### 3. `archivum connect`

The command that makes linking one step, runnable with no checkout:

```bash
npx archivum@latest connect <pairing-token>
```

It must not call `ensureRoot()` or read `.env` — that assumption is what makes the
existing `archivum mcp config` unusable on a second machine. Everything it needs
comes from the token and the redeem response.

What it does, in order:

1. Redeem the token; obtain a device key scoped to this machine.
2. Detect installed clients and write their configs directly rather than printing
   JSON to paste:
   - Claude Code — via `claude mcp add`, falling back to editing the config file
   - Cursor — `~/.cursor/mcp.json`
   - Codex — `~/.codex/config.toml`
3. Install or update the `archivum-memory` skill into `~/.claude/skills/`. This
   replaces `cp -r skills/archivum-memory ~/.claude/skills/`
   (`docs/architecture/agent-access.md:25`), which needs a checkout and has no update
   path once copied.
4. Verify: call one read-only MCP tool and report the vault it reached.
5. Print, for browser clients that cannot be configured from a terminal, the
   connector URL and key for claude.ai / ChatGPT.

Default the device name to `<hostname> / <client>`, overridable with `--name`.

`archivum connect --status` reports what is linked on this machine and whether the
key still authenticates. `archivum connect --revoke` removes local config and revokes
the device key server-side.

### 4. Transcript shipping

`archivum agent watch` runs on the machine where the agent runs, tails the local
transcript directories, and posts new lines to the server, which feeds them into the
existing capture pipeline. This is what makes the stream on a hosted deployment show
work done on a laptop.

It reuses the device key from `connect` and ships only from directories the user names.

Until it exists, the docs must stop implying capture is automatic in the remote case.
`record_work` becomes the documented primary path for hosted deployments, and the
skill is what makes agents call it without being asked.

### 5. Fail closed

`_require_key()` gains transport awareness:

- **stdio** — no bearer required. The transport is a local pipe; the caller already
  has whatever access the container has.
- **HTTP/SSE** — a valid device key or legacy key is required. When no key is
  configured at all, the HTTP listener refuses to start and logs why, rather than
  starting and serving the vault open.

### 6. The context pack becomes a visible surface

`build_context_package` is the clearest expression of the positioning and currently
has no screen. Give it one: for a task, show what was included, what was excluded and
why (budget, staleness, trust), the citations, and the token count.

This is what makes "you can audit it" literal rather than a claim, and it is the
screen the demo is built around.

### 7. Proof, not features

Ship a reproducible before/after: one task, one agent, run with and without an
Archivum context pack, showing tokens loaded and whether the task succeeded. The
comparison is against a populated `CLAUDE.md`, not against an empty context — the
honest baseline is what a competent developer already does.

The metric to own is **tokens loaded versus task success**. It doubles as a
user-visible feature because the context pack screen already shows the numerator.

## Docs changes

**`README.md`**

- Replace the opening line with the positioning above.
- Restructure the quickstart around the multi-machine case: server once, `connect`
  per machine. The localhost-only path becomes a subsection, not the default.
- Move markdown-on-disk from the headline into a trust section.
- Prune the Features list per the cuts below.

**`AGENTS.md`**

- Amend the Product Direction section. Line 27 currently forbids describing Archivum
  as project memory; that prohibition is lifted and inverted — coding-agent memory is
  the primary framing, and wiki/graph/backlink surfaces are described as how memory is
  inspected.
- Keep the existing prohibition on Archductor and Archgraph framing.

**`docs/architecture/agent-access.md`**

- Lead with `archivum connect`. Demote the hand-written stdio and SSE JSON to a
  manual-configuration appendix for people who want to see what `connect` writes.
- State plainly that stdio is single-machine only.

**`docs/architecture/deploy.md`**

- Add the pairing/device-key steps.
- Correct the capture section to say what happens on a hosted deployment before the
  shipper exists.

## Feature cuts

From `README.md:132-149`.

| Feature | Disposition | Reason |
|---|---|---|
| Life OS workflows | Remove from Features; keep MCP tools | README already says "not the main positioning" |
| Local media transcription | Move to an optional-extras section | Not on the wedge path; omitted from published images anyway |
| Sharing / public wiki / export | Keep, demote below memory features | Real, but not why a developer adopts |
| Graph exploration | Keep, reframe | Becomes "inspect what your agents know", not "explore a graph" |
| Wikilinks and backlinks | Keep as-is | Part of the inspection story |
| Semantic search, cited Q&A | Promote | Directly serve recall |

Nothing is deleted from the codebase by this document. The cuts are to what the
public docs lead with.

## Phasing

**Phase 1 — linking.** Device keys, pairing tokens, `archivum connect`, fail-closed
auth, Settings devices view. Docs rewritten to match. This alone delivers the wedge:
repo context and fixes reachable from every machine, linked in one command.

**Phase 2 — capture.** `archivum agent watch`. Automatic capture from any machine,
which is what makes the stream trustworthy on a hosted deployment.

**Phase 3 — proof.** Context pack screen, before/after benchmark, the tokens-versus-
success metric.

Phases 1 and 2 are the product for this wedge. Phase 3 is how it gets sold.

## Non-goals

- Team or multi-user accounts. The store is `person:self`-centred and stays so.
- A hosted Archivum service. Self-hosted only; pairing assumes the user runs the server.
- Replacing `CLAUDE.md`. Archivum supplements it — the file stays the right place for
  instructions that must load unconditionally.
- Everyday-user surfaces. Deferred, and they arrive as scopes, not as a second product.

## Testing

Per `AGENTS.md:43-50`:

```bash
npm test --workspace apps/frontend
npm run build --workspace apps/frontend
npm test --workspace packages/archivum-cli
cd apps/backend && uv run --group dev pytest ../../tests -q
```

Specific coverage this design requires:

- Pairing token: redeems once; second redemption fails; expired token fails; expired
  and already-redeemed return indistinguishable errors.
- Device key: authenticates; revoked key is refused; `last_seen_at` advances on use.
- Legacy `MCP_API_KEY` still authenticates over HTTP.
- HTTP listener refuses to start with no key configured; stdio starts without one.
- `archivum connect` writes correct config for each client, is idempotent on re-run,
  and does not require a repo checkout or `.env` — assert this by running it from a
  directory with neither.

Docs-only stale-language scan, also per `AGENTS.md:52-56`:

```bash
rg -n "scripts/[b]ootstrap|[N]eo4j|[y]ou@youremail|[L]ast updated: 2026-06|[f]eature complete" -g "*.md" -g "!node_modules/**" -g "!apps/backend/.venv/**"
```

## Open questions

1. Pairing token lifetime — 15 minutes assumed. Long enough to walk to another
   machine, short enough that a leaked token is close to worthless. Confirm.
2. Should `connect` configure clients globally or per-project? Claude Code supports
   both; global matches "this machine is linked", per-project matches how repo scoping
   works. Assumed global, with `--project` as an override.
3. Does the transcript shipper send raw transcript lines, or filter locally first?
   Raw is simpler and keeps the evaluator server-side where it already lives; it also
   sends more of the user's session content over the wire than they may expect.
4. When a device key is revoked, do memories recorded through it stay? Assumed yes —
   revocation is about access, not about rewriting history — but the lineage should
   record which device wrote what.
