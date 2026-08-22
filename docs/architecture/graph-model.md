# Graph Model

Markdown pages are the human editing surface. Canonical knowledge rows preserve the owner profile, page-authored content, projects, thoughts, extracted entities, relationships, citations, confidence, and extraction method. Qdrant, Kuzu, FTS, and code lexical indexes are rebuildable projections. Retrieval defaults to `person:self` when the caller does not provide another seed.

Archivum stores the graph projection in embedded Kuzu. The graph powers graph APIs, graph UI, and MCP neighbor lookups, while canonical rows remain the source for rebuilding and cited context.

## Owner-Centered Nodes

The canonical graph starts at `person:self`, the owner profile. Page-authored content links the owner to projects and thoughts, and extracted entities and relationships extend the graph to people, code, sources, and decisions.

## Canonical Knowledge Graph

Canonical objects are stored as knowledge rows before they are projected into graph tables:

| Record | Key | Properties |
|---|---|---|
| `KnowledgeNode` | `id` | `kind`, `label`, `scope`, `confidence`, `extraction_method`, `citations`, `properties` |
| `KnowledgeRelationship` | `id` | `src_id`, `dst_id`, `rel_type`, `scope`, `confidence`, `extraction_method`, `citations`, `properties` |

`KnowledgeNode.kind` covers the owner profile, page-authored content, projects, thoughts, sources, extracted entities, people, code, and decisions as applicable. Relationships retain their citations and provenance metadata when projected into Kuzu.

## Graph Audit

The canonical graph can be inspected without an LLM. All four analyses are deterministic, so the same graph always produces the same answer.

| Route | Answers |
|---|---|
| `GET /api/graph/audit` | Full report: cluster count, provenance breakdown, gaps, surprising links, plain-language narrative |
| `GET /api/graph/communities` | Which records cluster together |
| `GET /api/graph/surprising` | Which connections are least predictable from the rest of the graph |
| `GET /api/graph/path` | Shortest relationship path between two records |

Details:

- **Communities** are found by greedy modularity maximisation: two communities are merged while the merge raises modularity, taking the best gain each round and breaking ties on sorted ids. Above `MAX_MODULARITY_NODES` the audit falls back to connected components.
- **Surprise** scores each edge as `0.6 * (1 - neighbour overlap) + 0.4 * (crosses a cluster boundary)`. An edge between two records that share no other connections and sit in different clusters is the one a reader would not have predicted.
- **Paths** are breadth-first over undirected relationships, with deterministic neighbour ordering.
- **Provenance honesty**: because every canonical object must carry at least one citation, the audit reports records that cite *only themselves* — owner-root and page-title citations are self-referential by construction and should not read as corroboration. It also reports low-confidence records and records with no relationships at all.

The owner root is always included in a scoped audit, even though `person:self` lives in its own scope, because it is the hub most of the graph hangs off.

A scope may be a wiki (`wiki:default`), the owner (`person:self`), or an indexed repository (`repo:atlas`) — see [code memory](./code-memory.md). Link scopes (`bridge`, `cross_repo`) hold edges that join two other scopes and belong to neither, so a scoped load also pulls in link edges touching its nodes together with what they point at. Repository scopes are authorised by the register rather than by the scope string.

## Legacy Compatibility Projection

The following `Page` and `Entity` tables and edges are the legacy compatibility projection used by existing graph APIs and wikilink behavior. They are derived from canonical knowledge and should not be read as the complete canonical object model.

| Node | Key | Properties |
|---|---|---|
| `Page` | `slug` | `title`, `wiki_id` |
| `Entity` | `name` | `type`, `wiki_id` |

## Edge Types

| Edge | Source | Notes |
|---|---|---|
| `Page -[:REFERENCES]-> Page` | `[[wikilink]]` syntax | Created only when the target page exists at edge-build time |
| `Page -[:MENTIONS]-> Entity` | Extracted entity names in page markdown | Case-insensitive substring match |
| `Entity -[:RELATED_TO]-> Entity` | Extraction LLM `relationships[]` | No separate verification pass |

## Rebuild

Use the legacy index refresh command when content changes outside the normal write path or when ingest order left missing `REFERENCES` edges:

```bash
node packages/archivum-cli/src/index.js wiki rebuild-indexes
```

This command upserts page vectors, page nodes, and wikilink `REFERENCES` edges in the legacy page-based Qdrant/Kuzu projections from SQLite page content and metadata. It does not remove stale page vectors or nodes, update entity/mention/relationship projections, or rebuild canonical knowledge projections, FTS, or the code lexical index.
