# MCP Server Tools

Archivum exposes the editable markdown wiki, cited retrieval, and owner-centered agent context through a built-in MCP server implemented in `apps/backend/archivum/mcp/server.py`.

Agents read and write the same markdown pages that humans edit. Canonical knowledge rows preserve citations, confidence, and extraction method, while Qdrant, Kuzu, FTS, and code lexical indexes remain rebuildable projections. Context retrieval defaults to `person:self` when the caller does not provide another seed.

## Transports

| Transport | Use |
|---|---|
| stdio | Desktop clients that run a local command, such as Claude Desktop |
| HTTP/SSE | Editors and web clients that connect to `http://localhost:8001/sse` |

Container default is SSE. Use `--stdio` when shelling into the MCP container from a desktop client.

## Client Examples

stdio:

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

HTTP/SSE:

```json
{
  "mcpServers": {
    "archivum": {
      "url": "http://localhost:8001/sse",
      "headers": { "Authorization": "Bearer your-mcp-api-key" }
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `ingest_source(source, wiki_id)` | Process a file path or URL into the wiki |
| `search_wiki(query, top_k, wiki_id)` | Search the indexed markdown vault |
| `list_pages(wiki_id)` | List wiki pages from SQLite |
| `get_page(slug, wiki_id)` | Read full markdown for one page |
| `write_page(title, content, slug, tags, wiki_id)` | Queue a page create/update and wait for re-indexing |
| `life_daily_note(day, wiki_id)` | Create or return a daily note |
| `life_register_project(key, name, summary, status, wiki_id)` | Register a project and create its page |
| `life_create_task(title, project_key, page_slug, due_date, wiki_id)` | Create a Life OS task |
| `graph_neighbors(node_id, wiki_id)` | Return one-hop Kuzu neighbors |
| `export_graph_demo(output_dir)` | Write a self-contained demo graph export |
| `lint_wiki(wiki_id)` | Report broken wikilinks, orphan pages, and contradictory claims |
| `query(question, wiki_id)` | Retrieve cited context and synthesize an answer |
| `dispatch_command(command, wiki_id)` | Text wrapper for ingest/search/query/pages/open/write/lint/graph actions |
| `capture_conversation(session_id, interface, turns, scope)` | Record a user-visible AI session as an immutable source |
| `build_context_package(query, scope, depth, max_nodes, relations)` | Bounded cited subgraph without page bodies |
| `retrieve_memory(query, wiki_id, limit)` | Compact hybrid evidence with citations |
| `distill_source(source_id, wiki_id, scenario_key)` | Promote a captured session into cited atoms, scenario, persona, and skill memory |
| `catalog_memory_assets(wiki_id)` | Register existing pages, sources, and code graphs as governed assets |
| `index_repository(path, name, wiki_id)` | Read a repository into code memory: graph, governed asset, and vault pages |
| `list_repositories(wiki_id)` | The repositories this vault has indexed, and how fresh each one is |
| `retrieve_code_context(query, repo, depth, max_nodes, wiki_id)` | Cited code context scoped to one indexed repository |
| `list_memory_assets(asset_type, status, wiki_id, limit)` | List governed memory assets |
| `load_agent_memory(agent_key, query, wiki_id, limit)` | Return only the memory assets this agent is equipped with, cited |
| `graph_audit_report(wiki_id, surprise_limit)` | Clusters, provenance breakdown, gaps, and surprising links |
| `graph_shortest_path(source, target, wiki_id)` | Shortest relationship path between two canonical records |

`distill_source`, `graph_audit_report`, and `graph_shortest_path` are deterministic and make no LLM call. See [memory assets](./memory-assets.md) and [graph model](./graph-model.md).

Life OS tools are early product surfaces. Keep public positioning centered on the wiki, ingest, search, graph, sharing, export, and agent access.

## Security Note

`MCP_API_KEY` is configured for clients, but stdio clients run locally inside the trusted container context. Do not expose MCP SSE publicly without a trusted network, reverse proxy, or additional access controls.

## Validation

List MCP tools with Inspector:

```bash
cd apps/backend
UV_PYTHON=python3.12 npx @modelcontextprotocol/inspector --cli --method tools/list uv run python -m archivum.mcp.server --stdio
```
