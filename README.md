# Archivum

Archivum is memory your coding agents use and you can audit. What your agents
learn about your repos — decisions, fixes, architecture — lives in one vault you
own, reviewed and cited, and follows you to every machine you work on.

## Why

**Same memory on every machine.** A `CLAUDE.md` lives on one laptop. Archivum
lives on a server you run, and every machine you link reaches the same fixes, the
same repo context, and the same decisions — one command per machine.

**Less context for the same answer.** Retrieval is ranked and scoped, so an agent
gets the passages that bear on the task instead of whole files pasted into the
window. A lookup is a network call and is therefore slower than reading a local
file; what it buys you is a smaller context and a memory that is not stuck on one
disk, not a faster read.

**You can see and correct what it knows.** Memory is promoted through review,
cited back to the page it came from, and versioned with its lineage. Anything
below the confidence threshold waits for you in the stream instead of being
written silently.

**Your data, your disk.** Plain markdown in volumes you own, on hardware you run,
with no third party in the loop. That is what makes "you can audit it" checkable
rather than a promise — you can open the files.

Archivum does not replace `CLAUDE.md`. That file stays the right place for
instructions that must load unconditionally. Archivum is for everything too large,
too situational, or too easily forgotten to keep there.

## Part of the Perceo stack

Archivum is part of [Perceo](https://perceo.ai) — a local-first developer suite. Related tools:

- [Archductor](https://github.com/perceo-ai/conductor-arch)
- [Archfleet](https://github.com/perceo-ai/archfleet)

Docs for the whole stack live at [docs.perceo.ai](https://docs.perceo.ai).

## Install

Run the server once, on whatever machine you want to hold the vault — a spare
box, a VM, or the laptop you use most.

Requirements:

- Docker Engine 24+ with Docker Compose v2
- Node.js 20+
- An Anthropic, OpenRouter, or OpenAI-compatible key — or a local Ollama setup

```bash
git clone https://github.com/pranavkannepalli/archivum.git
cd archivum
./install.sh
```

Windows PowerShell:

```powershell
git clone https://github.com/pranavkannepalli/archivum.git
cd archivum
.\install.ps1
```

The installer writes `.env`, generates missing secrets, and starts the stack using published images via `docker-compose.images.yml`. To build from local source instead:

```bash
./install.sh --build
```

Manual setup, without the installer:

```bash
cp .env.example .env
# Set OWNER_PASSWORD, JWT_SECRET, and your LLM provider key.
docker compose -f docker-compose.yml -f docker-compose.images.yml up -d --no-build
```

Required `.env` values: `OWNER_PASSWORD` and `JWT_SECRET` (`openssl rand -hex 32`).
`MCP_API_KEY` is a legacy shared credential — the installer still generates one so
existing configs keep working, but linked machines each get their own key and do
not need it. See [.env.example](.env.example) for the full reference.

Then log in at `http://localhost:8473` with `OWNER_USERNAME` (`admin` by default)
and your `OWNER_PASSWORD`.

## Link your agents

In Settings → Agent Access, click **Link a device**. Archivum shows a pairing
token. On the machine you want to link — including the one running the server —
run:

```bash
npx archivum@latest connect arch1_...
```

That writes MCP config for whichever of Claude Code, Cursor, and Codex it finds on
the machine, installs the `archivum-memory` skill into `~/.claude/skills/`, and
prints the vault it paired with. The pairing token works once and expires after
fifteen minutes, so issue a fresh one for each machine.

The machine needs no checkout and no `.env`: the server's URL travels inside the
token, which is why one string is all you copy.

```bash
archivum connect --status   # what is linked here, and whether the key still authenticates
archivum connect --revoke   # revoke this machine's key and delete its local record
```

`--revoke` will not delete the local record unless the server confirms the
revocation, so a failed revoke never leaves you with a live key and no note of
which device to revoke from Settings.

> **Getting the CLI today.** The CLI is published to GitHub Packages as
> `@pranavkannepalli/archivum` and is not yet on the public npm registry, so
> `npx archivum@latest` does not resolve. Until it is published there, clone the
> repo on the machine you are linking and run it from the checkout. No `npm
> install` and no `.env` — the CLI has no dependencies, and `connect` reads none
> of the repo's config:
>
> ```bash
> git clone https://github.com/pranavkannepalli/archivum.git
> cd archivum
> node packages/archivum-cli/src/index.js connect arch1_...
> ```

### Per-device keys

Every linked machine gets its own `amk_…` key. Settings lists them by name, with
when each was linked and last seen, and a **Revoke** button per row. Losing a
laptop costs you one key instead of a rotation across every client you own.

If you set `MCP_API_KEY`, it still authenticates, and Settings shows it as
`legacy shared key` — a credential to retire once each machine is linked
individually.

### Configuring a client by hand

`connect` writes ordinary config files. If your client is not one of the three, or
you would rather see exactly what goes where, this is it.

Over HTTP/SSE — the only form that works from a machine other than the server's:

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

Over stdio, which shells into the container with `docker exec` and therefore
**only works on the machine running that container**:

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

For claude.ai and ChatGPT, which cannot be configured from a terminal, `connect`
prints the connector URL and bearer header to paste into the browser.

See [agent access](docs/architecture/agent-access.md) for the whole picture,
including what to set when the server sits behind a reverse proxy.

## Where things listen

| URL | Purpose |
|---|---|
| `http://localhost:8473` | The interface |
| `http://localhost:8473/api/*` | REST API — the frontend container proxies `/api/` to `backend:8000` |
| `http://localhost:8001/sse` | MCP HTTP/SSE endpoint |

The backend's own port `8000` is not published to the host. It is reachable only
from inside the Compose network, which is why REST goes through `8473`.

MCP tools exposed to agents, by what you want:

| Want | Tools |
|---|---|
| Read the vault | `search_wiki`, `list_pages`, `get_page`, `query`, `retrieve_memory` |
| Write to it | `write_page`, `ingest_source`, `capture_conversation`, `record_work` |
| Understand code | `index_repository`, `list_repositories`, `retrieve_code_context`, `recall_fix` |
| See the shape of the vault | `vault_themes`, `summarise_vault`, `graph_neighbors`, `graph_shortest_path`, `graph_audit_report`, `build_context_package` |
| Govern memory | `list_memory_assets`, `catalog_memory_assets`, `load_agent_memory`, `distill_source` |
| Housekeeping | `lint_wiki`, `export_graph_demo`, `life_daily_note`, `life_register_project`, `dispatch_command` |

`connect` installs the `archivum-memory` skill, which is what turns 28 tools into
a habit: check `recall_fix` before debugging, load code context before changing
unfamiliar code, and `record_work` when something mattered. The source lives at
[`skills/archivum-memory/SKILL.md`](skills/archivum-memory/SKILL.md) and the server
serves the same file at `/api/mcp/skill`.

## How it works

1. **Editable markdown is the source of truth.** Markdown pages live in the `wiki_data` volume; original uploads land in `raw_data`. Canonical knowledge rows preserve page-authored content and its provenance, while indexes are derived and rebuildable.
2. **Ingest normalizes sources.** File paths and URLs are parsed into wiki pages with source metadata, then chunked and indexed. Supported inputs include markdown, PDF, HTML, EPUB, DOCX/PPTX/XLSX, CSV/JSON, ZIP archives, source code, RTF/XML, EML/MBOX, subtitles, images, and optional audio/video transcripts.
3. **Canonical knowledge powers the vault.** Canonical knowledge rows preserve the owner profile, page-authored content, projects, thoughts, extracted entities, relationships, citations, confidence, and extraction method. Qdrant (`qdrant_data`), Kuzu (`kuzu_data`), FTS, and code lexical indexes are rebuildable projections.
4. **Search and Q&A run over your content.** Retrieval defaults to `person:self` when the caller does not provide another seed, returns cited context and ranked excerpts, and `query` synthesizes an answer with citations back to the source pages.
5. **Captured sessions become governed memory.** Distillation turns a captured conversation into cited memory atoms, scenario memory, an owner profile, and — when real tool steps were recorded — a reusable skill. Anything below the confidence threshold goes to human review instead of being written silently, and an agent only receives the assets you bound to it. This path is deterministic and makes no LLM call. See [memory assets](docs/architecture/memory-assets.md).
6. **Agents reach the same vault over MCP** via stdio or HTTP/SSE — reading, writing, searching, and querying the identical data the browser sees. HTTP always authenticates; stdio does not, because it is a local pipe into a container you can already exec into.
7. **Put your own reverse proxy in front** if you want TLS or a hostname. Archivum exposes the UI on 8473, the API on 8000, and MCP on 8001; anything from Caddy to a Cloudflare tunnel can terminate in front of those. Set `API_PUBLIC_URL` and `MCP_PUBLIC_URL` when you do, so pairing tokens and device configs carry the URL clients actually use.

To refresh page vectors, page nodes, and wikilink reference edges in the legacy page-based Qdrant/Kuzu projections, run:

```bash
node packages/archivum-cli/src/index.js wiki rebuild-indexes
```

This command upserts those page records and reference edges. It does not remove stale page vectors or nodes, update entity/mention/relationship projections, or rebuild canonical knowledge projections, FTS, or the code lexical index.

## Features

Memory for agents:

- ✅ Per-device MCP keys — one revocable key per linked machine, issued by pairing token
- ✅ One-command linking (`archivum connect`) for Claude Code, Cursor, and Codex
- ✅ Governed memory assets — typed, versioned, reviewable memory that agents can be equipped with by name
- ✅ Deterministic session distillation — captured conversations become cited memory with no LLM call
- ✅ Semantic search over the vault (Qdrant)
- ✅ Question answering with citations back to source pages
- ✅ Built-in MCP server (stdio + HTTP/SSE) for Claude Code, Cursor, Codex, Claude Desktop, and VS Code

The vault underneath:

- ✅ Markdown pages stored on disk as canonical content
- ✅ File and URL ingest with a broad parser matrix and source metadata
- ✅ Docker Compose deployment with SQLite and Qdrant
- ✅ Pluggable LLM and embedding providers: Anthropic, OpenRouter, OpenAI-compatible, or local Ollama/fastembed
- ✅ Graph audit — clusters, shortest paths, surprising connections, and a plain-language provenance report
- 🚧 Browser vault navigation — folder/page APIs and file-tree UI exist; click-through needs release smoke
- 🚧 Wikilinks and backlinks — CodeMirror extension and backlinks API/UI exist; browser smoke pending
- 🚧 Inspecting what your agents know — the Kuzu graph API and frontend view show what is connected to what across pages, entities, and indexed repositories; browser smoke pending
- 🚧 Sharing, public wiki, and HTML/PDF export — code exists (`/share/{token}`, `/public`, `/api/export`); manual release smoke pending

Optional extras:

- 🚧 Local media transcription (Whisper/ffmpeg) — installable from Settings, emits timestamped audio/video transcript text when dependencies are installed, and is omitted from published images to keep installs small

Daily notes and projects (`life_daily_note`, `life_register_project`) exist as MCP
tools and REST routes. They are an early surface and not what Archivum is for, so
they are not listed above.

## Operations

```bash
./update.sh                         # back up precious data, pull/update, and restart
./update.sh --no-backup             # update without creating a pre-update backup
node packages/archivum-cli/src/index.js recovery backup
node packages/archivum-cli/src/index.js recovery validate backups/<backup-dir>
node packages/archivum-cli/src/index.js recovery restore backups/<backup-dir> --dry-run
node packages/archivum-cli/src/index.js recovery restore backups/<backup-dir> --yes
./uninstall.sh                      # remove containers/network, keep data
./uninstall.sh --volumes            # also delete wiki/raw/db/Kuzu/Qdrant/Ollama volumes

docker compose logs -f backend
docker compose logs -f mcp
docker compose restart backend
docker compose down
```

## Documentation

- [Documentation index](docs/README.md)
- [Agent access: linking machines, keys, and skills](docs/architecture/agent-access.md)
- [Infrastructure and storage](docs/architecture/infra.md)
- [Ingest pipeline](docs/architecture/ingest.md)
- [MCP server tools](docs/architecture/mcp.md)
- [Retrieval and context sizing](docs/architecture/retrieval.md)
- [Graph model and graph audit](docs/architecture/graph-model.md)
- [Memory assets, distillation, and agent loadouts](docs/architecture/memory-assets.md)
- [Daily use: the stream, tasks, and search](docs/architecture/daily-use.md)
- [Agent guide](docs/agent-guide.md)

## License

Apache-2.0. See [LICENSE](LICENSE).
