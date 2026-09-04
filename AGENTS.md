# Archivum Agent Instructions

## Project Context

Archivum work lives in the Linear `Archivum` project.

When pulling work from Linear in this repository:

- Query the `Archivum` project specifically.
- Be specific with Linear queries: project, status, assignee, issue key, labels, and relevant text.
- When starting a Linear task, move it to `In Progress`.
- When finishing a Linear task, move it to `In Review` so the user can review and push.

## Product Direction

Archivum is self-hosted memory for coding agents that a human can audit. The
wedge is the developer whose agents run on more than one machine and who wants
repo context, decisions, and past fixes to follow them.

Keep public-facing docs and work focused on:

- Memory agents use: governed memory assets, session distillation, cited recall, code context
- Linking machines: pairing tokens, per-device keys, `archivum connect`
- MCP access for agents over stdio and HTTP/SSE
- Ingest of files and URLs into that memory
- Docker Compose self-hosting, with markdown on disk as the proof that the vault is yours

The wiki surfaces — vault navigation, wikilinks and backlinks, graph views,
search, sharing, and export — stay in the product and stay documented. Describe
them as how memory is inspected and corrected, not as the product itself.

Two constraints on how this is written:

- Do not describe Archivum as Archductor, Archgraph, or generic GraphRAG work
  unless it is clearly a private integration note.
- Never claim Archivum recalls faster than markdown files on disk. Reading a
  local `CLAUDE.md` is a filesystem read; Archivum retrieval is a network round
  trip plus ranking, so it is slower per call. The honest and stronger claim is
  less context for the same answer, and the same memory on every machine.

## Agent Source of Truth

Read these before making product/docs changes:

- `README.md` for customer-facing install/product docs.
- `docs/README.md` for the docs map.
- `docs/agent-guide.md` for coding-agent orientation.
- `docs/project/progress.md` for verified/partial/unknown status.
- `.env.example`, `docker-compose.yml`, and `docker-compose.images.yml` for runtime truth.

Old PRD, operator handoff, and root `Progress.md` docs were intentionally pruned. Do not recreate them unless the user explicitly asks.

## Verification

Run the relevant checks before claiming completion:

```bash
npm test --workspace apps/frontend
npm run build --workspace apps/frontend
npm test --workspace packages/archivum-cli
cd apps/backend && uv run --group dev pytest ../../tests -q
```

For docs-only changes, also scan for stale language:

```bash
rg -n "scripts/[b]ootstrap|[N]eo4j|[y]ou@youremail|[L]ast updated: 2026-06|[f]eature complete" -g "*.md" -g "!node_modules/**" -g "!apps/backend/.venv/**"
```
