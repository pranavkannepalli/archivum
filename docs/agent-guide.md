# Archivum Agent Guide

Use this file when coding agents need fast repo context.

## Product Positioning

Archivum is self-hosted memory for coding agents that a human can audit. The
wedge is the developer whose agents run on more than one machine.

Keep public docs and product language focused on:

- Memory agents use: governed memory assets, session distillation, cited recall, code context
- Linking machines: pairing tokens, per-device keys, `archivum connect`
- MCP access for agents over stdio and HTTP/SSE
- File and URL ingest into that memory
- Docker Compose self-hosting, with markdown on disk as the proof the vault is yours

The wiki surfaces — vault navigation, wikilinks and backlinks, graph, search,
sharing, and export — stay in the product. Describe them as how memory is
inspected and corrected, not as the product itself.

Do not describe Archivum as Archductor, Archgraph, or generic GraphRAG work
unless the text is clearly a private integration note. Never claim Archivum recalls faster than markdown files
on disk; it is a network round trip plus ranking, and the honest claim is less
context for the same answer and the same memory on every machine. See
[AGENTS.md](../AGENTS.md).

## Source of Truth

- Customer/install docs: [README.md](../README.md)
- Docs index: [docs/README.md](./README.md)
- Architecture: [docs/architecture](./architecture)
- Current status: [docs/project/progress.md](./project/progress.md)
- Environment reference: [.env.example](../.env.example)
- Compose runtime: [docker-compose.yml](../docker-compose.yml), [docker-compose.images.yml](../docker-compose.images.yml)

## Main Code Areas

| Area | Path |
|---|---|
| Backend API | `apps/backend/archivum/api` |
| Ingest pipeline | `apps/backend/archivum/ingest` |
| MCP server | `apps/backend/archivum/mcp/server.py` |
| Storage adapters | `apps/backend/archivum/db` |
| Frontend app | `apps/frontend/src` |
| CLI installer/update/uninstall | `packages/archivum-cli/src` |
| Docker | `docker-compose.yml`, `docker-compose.images.yml` |

## Work Rules

- Read existing code before changing behavior.
- Prefer small diffs and existing patterns.
- Do not add new docs unless they replace real duplication or capture current operational truth.
- Keep README customer-facing; put implementation details in `docs/architecture`.
- Do not claim a feature is verified unless backed by a current test, command, or manual smoke.
- If a Linear issue is used, move it to `In Progress` at start and `In Review` when ready for human review.

## Verification Commands

Run the relevant subset for the files changed:

```bash
npm test --workspace apps/frontend
npm run build --workspace apps/frontend
npm test --workspace packages/archivum-cli
cd apps/backend && uv run --group dev pytest ../../tests -q
```

For docs-only changes, also check links and stale language:

```bash
rg -n "scripts/[b]ootstrap|[N]eo4j|[y]ou@youremail|[L]ast updated: 2026-06|[f]eature complete" -g "*.md" -g "!node_modules/**" -g "!apps/backend/.venv/**"
```
