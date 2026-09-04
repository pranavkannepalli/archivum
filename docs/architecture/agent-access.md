# Agent Access

Archivum is meant to be used from outside Archivum. This is how a machine gets a
key, what that key can do, and what to do when the easy path does not fit.

## Linking a machine

Two steps, one of them in the browser.

1. In Settings → Agent Access, click **Link a device**. Archivum issues a pairing
   token — an `arch1_…` string that is single-use, expires after fifteen minutes,
   and carries the server's own API base URL inside it.
2. On the machine you are linking, run:

```bash
npx archivum@latest connect arch1_...
```

That is the whole thing. `connect` needs no checkout and no `.env`, which is the
point: the second laptop has neither. Everything it needs arrives in the token
and in the redeem response.

The CLI is published to GitHub Packages as `@pranavkannepalli/archivum` and is not
on the public npm registry yet, so `npx archivum@latest` does not resolve today.
Until it is published there, clone the repo on the machine you are linking and run
it from the checkout:

```bash
git clone https://github.com/pranavkannepalli/archivum.git
cd archivum
node packages/archivum-cli/src/index.js connect arch1_...
```

That needs Node 20+ and nothing else — the CLI has no dependencies, so there is no
`npm install` step, and `connect` still reads no `.env`. It is a worse story than
one `npx` line, and it goes away when the package is published.

In order, `connect`:

1. Posts the token's secret to `POST /api/mcp/pairing/redeem`, which burns the
   token and mints a device key (`amk_…`) named `<hostname> / <clients>` unless
   you pass `--name`.
2. Saves that key to `~/.archivum/connection.json`, mode `0600`, *before* it
   touches any client config. The token is spent the instant redeem returns, so a
   writer failing halfway must still leave the key recoverable rather than
   orphaned on the server.
3. Writes MCP config for every supported client it detects — `~/.claude.json`,
   `~/.cursor/mcp.json`, `~/.codex/config.toml` — each at mode `0600`, tightening
   the file even if it already existed with looser permissions. Pass
   `--client claude|cursor|codex` to pin the set instead of detecting it.
4. Installs the `archivum-memory` skill to `~/.claude/skills/archivum-memory/`,
   fetched from the server at `GET /api/mcp/skill`. A server that does not bundle
   the skill is still a working server, so a missing skill does not fail the link.
5. Prints the connector URL and bearer header for claude.ai and ChatGPT, which
   cannot be configured from a terminal.

Restart the agent afterwards; MCP clients read their config at startup.

The Codex writer edits `~/.codex/config.toml` between `# >>> archivum >>>` and
`# <<< archivum <<<` markers and leaves everything outside them alone, so a
hand-written Codex config survives a re-link. A `~/.claude.json` or
`~/.cursor/mcp.json` that is not valid JSON stops the run with an error rather
than being overwritten.

## Checking and unlinking

```bash
archivum connect --status
archivum connect --revoke
```

`--status` prints the server and device id from `~/.archivum/connection.json`,
which clients were configured, and whether the key still works — it calls
`GET /api/mcp/devices/self`, which authenticates with the device's own key, so a
200 is a direct answer to "does this key still work" rather than an owner-only
listing that would refuse any device key.

`--revoke` calls `DELETE /api/mcp/devices/self` and only then removes the local
record. If the server cannot be reached, or has already forgotten the key, it
errors and leaves the local file in place: the alternative is printing "Revoked."
while a live key sits in three client configs and the only note of which
`device_id` to revoke from Settings has just been deleted. It does not remove the
`archivum` entry from the client configs; the key stops working, but the stanza
stays on disk until you delete it.

## One machine or many

The transport decides this, and it is worth being blunt about it.

**stdio is single-machine only.** The stdio recipe runs
`docker exec -i archivum-mcp …`, which requires the container to be on the same
machine as the agent. There is no way to point it at a server across a network.
It is the right choice on the box running the stack and useless anywhere else.

**HTTP/SSE is what every other machine uses.** It is also what `connect` writes,
on the server's machine too, so that one path works everywhere rather than two
paths that diverge.

By default MCP binds to `127.0.0.1:8001`, so a second machine cannot reach it
until you publish it. Put it behind the reverse proxy you already run — one
`ingress` entry for a Cloudflare tunnel, one site block for Caddy — and then set:

- `MCP_PUBLIC_URL` to the public SSE URL (e.g. `https://archivum-mcp.example.com/sse`).
  This is the URL handed to every device that pairs. Left unset, redeem returns
  `http://localhost:8001/sse`, which is correct on the server and wrong on a laptop.
- `API_PUBLIC_URL` to the public REST base (e.g. `https://archivum.example.com`).
  Pairing tokens embed this, and so do the skill URLs `connect` fetches. Left
  unset, the URL is derived from the request uvicorn sees, whose scheme can differ
  from the one the client actually used behind a TLS-terminating proxy.

Redemption is rate-limited on its own bucket — ten requests per ten minutes per
source address — deliberately separate from the login limiter, because a scripted
pairing attempt should not lock you out of the UI.

## Security

**HTTP always authenticates.** Every request over HTTP/SSE must carry a bearer
token that resolves to an active device key or to `MCP_API_KEY` when that is set.
An empty `MCP_API_KEY` no longer means "no auth" — it means only device keys are
accepted. This used to fail open, and an exposed port served the whole vault to
anyone who could reach it.

**stdio does not, on purpose.** The transport is a local pipe into a container the
caller can already `docker exec` into. A bearer check there would protect nothing
it does not already have.

**Keys are per device and revoked one at a time.** Each linked client gets its own
`amk_…` key; only its hash is stored, so a leaked database does not hand over
working access, and a lost key can be revoked but never recovered. Settings lists
every device with when it was linked and last seen, and revokes any single row
without disturbing the others.

**The legacy shared key still works.** `MCP_API_KEY`, when set, authenticates as
before, so installs that predate device keys keep running. Settings shows it as
`legacy shared key`. It is one credential shared by every client that holds it, so
retire it once each machine is linked individually — leave it unset and only
per-device keys remain.

Revoking a device does not delete what it wrote. Revocation is about access, not
about rewriting history.

## Chat clients (claude.ai, ChatGPT)

These cannot run a terminal command, so they are configured by hand from what
`connect` prints:

- URL: the value of `MCP_PUBLIC_URL`, or `http://localhost:8001/sse` when unset
- Header: `Authorization: Bearer amk_...`

A browser client needs the endpoint published, so this is the case that most needs
the proxy and `MCP_PUBLIC_URL` above. Prefer putting access control in front of the
hostname as well as the key.

## REST

Everything the interface does is a REST route under `/api`. Session cookies for
the browser, and the same routes work with a token for scripting.

## What agents should reach for

| Want | Tool |
|---|---|
| Have I hit this error before? | `recall_fix(symptom)` — **before** debugging |
| Understand unfamiliar code | `retrieve_code_context(query, repo, include_source)` |
| What is this vault about? | `vault_themes()` — cluster summaries, cited |
| Refresh those summaries | `summarise_vault()` |
| Answer a specific question | `query(question)` — cited, refuses when uncited |
| Record what mattered | `record_work(request, outcome, changed_paths, verified_by)` |
| What should I know here? | `load_agent_memory(agent_key)` |

The `archivum-memory` skill is what makes an agent reach for these unprompted.
Without it an agent sees 28 tools and no reason to call `recall_fix` before
digging into a stack trace. `connect` installs it and fetches it from the server,
so re-running `connect` updates it. Copying `skills/archivum-memory` into
`~/.claude/skills/` by hand still works, but it needs a checkout and the copy has
no update path once made — it will not track changes to the skill.

## Manual configuration

What `connect` writes, if you would rather write it yourself or your client is not
one of the three.

Claude Code (`~/.claude.json`) and Cursor (`~/.cursor/mcp.json`) take the same
shape:

```json
{
  "mcpServers": {
    "archivum": {
      "url": "http://localhost:8001/sse",
      "headers": { "Authorization": "Bearer amk_..." }
    }
  }
}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.archivum]
url = "http://localhost:8001/sse"
http_headers = { Authorization = "Bearer amk_..." }
```

stdio, on the machine running the container only. No key is needed, because stdio
does not authenticate:

```json
{
  "mcpServers": {
    "archivum": {
      "command": "docker",
      "args": ["exec", "-i", "archivum-mcp", "python", "-m", "archivum.mcp.server", "--stdio"]
    }
  }
}
```

Get a key by pairing a device and reading `~/.archivum/connection.json`, or use
`MCP_API_KEY` if you still have one set.

## Prompt context

Every prompt Archivum sends carries the current date, the day of the week, the
owner's name, and whatever the caller knows about the material — built in one
place (`llm/prompt_context.py`) so a new prompt cannot quietly omit it.

This is not decoration. A model with no date resolves "recently", "currently"
and "last month" against its training cutoff. For a vault whose whole job is
remembering *when* you thought something, an undated prompt produces answers
that are wrong in a way that reads as right. The preamble also states that
relative dates in the material are relative to *the material*, not to today.
