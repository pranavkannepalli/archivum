# Agent Access

Archivum is meant to be used from outside Archivum. Three ways in, all against
the same vault.

## 1. Coding agents over stdio (Claude Code, Cursor, Codex)

Point the client at the MCP container:

```json
{
  "mcpServers": {
    "archivum": {
      "command": "docker",
      "args": ["exec", "-i", "archivum-mcp", "python", "-m", "archivum.mcp.server", "--stdio"],
      "env": { "MCP_API_KEY": "your-mcp-api-key" }
    }
  }
}
```

Then install the skill so the agent knows *when* to use which tool:

```bash
cp -r skills/archivum-memory ~/.claude/skills/
```

Without the skill an agent has 28 tools and no idea that `recall_fix` should
come before debugging. With it, the loop is: check before digging, load context
before changing unfamiliar code, record what mattered.

## 2. Chat clients over HTTP (claude.ai, ChatGPT)

The MCP server also speaks HTTP/SSE, but it binds to `127.0.0.1` by default —
a remote chat client cannot reach a loopback port. To use Archivum from a
browser chat, put it behind the reverse proxy you already run:

1. Set `MCP_API_KEY` to a long random value (`openssl rand -hex 24`). Bearer auth
   is enforced whenever this is set, and **not enforced when it is empty** —
   never expose the port without it.
2. Publish `archivum-mcp:8001` through your proxy at a hostname with TLS. With a
   Cloudflare tunnel that is one `ingress` entry; with Caddy, one site block.
3. Add it as a custom connector in the chat client, URL `https://<host>/sse`,
   header `Authorization: Bearer <MCP_API_KEY>`.

The bearer key is the only thing between the internet and your entire vault.
Treat it like a password, rotate it if it leaks, and prefer putting the hostname
behind access control as well as the key.

## 3. REST

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

## Prompt context

Every prompt Archivum sends carries the current date, the day of the week, the
owner's name, and whatever the caller knows about the material — built in one
place (`llm/prompt_context.py`) so a new prompt cannot quietly omit it.

This is not decoration. A model with no date resolves "recently", "currently"
and "last month" against its training cutoff. For a vault whose whole job is
remembering *when* you thought something, an undated prompt produces answers
that are wrong in a way that reads as right. The preamble also states that
relative dates in the material are relative to *the material*, not to today.
