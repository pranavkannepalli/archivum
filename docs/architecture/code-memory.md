# Code Memory

Archivum is a second brain for someone who writes code, so a repository is not an import target — it is one of the things being remembered. Indexing a repo reads it into the same canonical knowledge store as pages and sources, registers it as a governed memory asset, and writes markdown into the vault. Code is ordinary vault content from that point on.

## Flow

1. Register a repository (`POST /api/repos`, or the `index_repository` MCP tool).
2. A queue worker reads it. Indexing is never inline: parsing a repository is slow and CPU-bound, and the wiki has to stay responsive.
3. Tree-sitter extraction produces `file`, `type`, and `symbol` records with `EXTRACTED` / `INFERRED` / `AMBIGUOUS` provenance and a file-and-line citation each, plus `defines`, `calls`, `imports`, and `inherits` edges.
4. Unresolved targets are resolved — same-file first (certain, so `EXTRACTED`), then cross-file (`INFERRED`, or `AMBIGUOUS` when several names match). Anything still unresolved is dropped rather than stored as a pointer to nothing.
5. Derived links are resolved against the **whole store**: identical types across repositories (`same_symbol_as`), and symbols named in recorded decisions (`decided_in`).
6. The code graph is registered as an L2 memory asset owned by `person:self`.
7. Communities are written into the vault as markdown, one page per cluster plus an index.

| Concern | Path |
|---|---|
| Application service and queue | `apps/backend/archivum/code_repos.py` |
| Vault page generation | `apps/backend/archivum/code_pages.py` |
| Extraction | `apps/backend/archivum/archgraph/extract.py` |
| Call resolution | `apps/backend/archivum/archgraph/resolve.py` |
| Derived links | `apps/backend/archivum/archgraph/{cross_repo,bridge}.py` |
| REST routes | `apps/backend/archivum/api/repos.py` |
| CLI / git hook | `apps/backend/archivum/archgraph/hook.py` |
| Frontend | `apps/frontend/src/surfaces/CodeRepos.tsx` |

## Scopes

Code records live under `repo:{wiki_id}:{name}`, not `wiki:{id}`, because a repository is not owned by one wiki's page namespace. The vault id is part of the scope because `api` or `web` is a name many vaults will use, and keying on the name alone would let the second vault to register take over the first vault's records.

Scope still cannot carry authorisation the way it does for pages, so **a repository is authorised by being in the register**: you may read a repo you registered. Registering is owner-only — it points the backend at a directory on the host and publishes what it finds, which is a host-level capability rather than ordinary write access. Repository names are validated as a single path segment, since the name becomes a directory under the cache root and a folder in the vault.

Two scopes exist only to join others:

| Scope | Holds |
|---|---|
| `cross_repo` | `same_symbol_as` — one type recognised across repositories |
| `bridge` | `decided_in`, `shipped_in`, `deployed_in` — code joined to the evidence that explains it |

A scoped graph load pulls in link-scope edges that touch its nodes, along with what they point at. Without that, the one edge explaining *why* a function exists would be the one edge nobody could see, since by construction it belongs to neither side.

Following a link crosses a scope boundary, so it carries its own authorisation. Callers pass the set of scopes they may read; a far end outside that set is neither fetched nor hinted at, and the edge itself is dropped so the connection's existence is not disclosed either. The same boundary applies when links are *created*: derived links are resolved against this repository, the vault that owns it, and that vault's other repositories — never another tenant's memory.

The audit reports `node_labels` and `node_kinds` for every record it covers, so anything drawing the graph can name and shape its nodes from the report alone rather than making a second, differently-scoped request.

`GET /api/graph/audit|communities|surprising|path` and `POST /api/context-package` all accept a `scope`, so the same deterministic clustering, surprise scoring and path finding that serve the vault also serve a repository. The algorithms were always scope-agnostic; only the routes were not.

## Vault Pages

Every indexed repository writes into `code/{name}/`:

- `index.md` — record and relationship counts, provenance breakdown, links to each cluster, the least predictable connections, and the plain-language narrative.
- One page per cluster — its members with `path:line`, how they connect internally, and what they reach outside the cluster.

Clusters come from the deterministic graph audit, not a model, so the same repository always produces the same pages and re-indexing an unchanged repo is a no-op. A cluster is named after the file or type that anchors it rather than its most-called member — a shared helper tops the degree count precisely because it says the least about what the cluster *is*. Locations render repo-relative (`lexical.py:91-93`); the absolute path on the indexing machine stays in the register, so pages remain portable and shareable. Clusters that hold only repository bookkeeping (the repo node and its commit) get no page. Pages go through `reindex_page` like every other write, so they are backlinked, searchable, editable, and portable. Distillation is skipped for them: they came *out* of the graph, and feeding them back in would propose the repository's own structure as things to remember.

## Language Coverage

| Language | Suffixes |
|---|---|
| Python | `.py` |
| TypeScript | `.ts`, `.mts`, `.cts` |
| TypeScript JSX | `.tsx` |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` |

JavaScript is read with the TSX grammar, which is a superset of it — no extra dependency, and it handles JSX. Rare JS-only ambiguities may parse imperfectly; a file that fails to parse is reported rather than silently skipped.

Files in other languages are still ingestible as prose through the normal ingest pipeline, but they do not produce code records. Adding a language is a `LanguageConfig` entry in `archgraph/registry.py` plus its tree-sitter grammar.

## Incremental Indexing

Re-indexing an already-known repository diffs against the last indexed SHA, re-extracts only changed files, prunes records whose every citation was in a deleted file, and clears canonical records for touched files before upserting. The parsed-AST cache lives under `CODE_CACHE_DIR`, beside the deployment's other data rather than inside the repository — indexing should not dirty a working tree you are trying to keep clean.

## MCP

| Tool | Purpose |
|---|---|
| `index_repository(path, name, wiki_id)` | Read a repository into code memory. Runs to completion rather than queueing, because the caller is waiting on it |
| `list_repositories(wiki_id)` | What has been indexed, and how fresh each one is |
| `retrieve_code_context(query, repo, …)` | Cited code context scoped to one repository |

## In the Interface

**Settings → Code memory** registers a repository and shows each one's status, record counts, and page count, polling while indexing runs.

**Visualized** gains a scope picker once a repository is indexed: *Your vault* plus one button per repo. Pointed at a repository, the graph puts the repo at its centre rather than the owner, names cluster members from the report, and makes each cluster ring open the page written for it.

## Remembering the Work

Indexing remembers the code as it stands. Capture remembers how it got that way.

Agent transcripts are watched and imported automatically — the same importer and
redaction the manual route used, run on a poll rather than on request. Nothing
downstream of capture (atoms, decisions, skills, `decided_in`) ever ran before,
because nothing was capturing. Name the directories in `TRANSCRIPT_DIRS`; unset,
the watcher does nothing rather than guessing at a path it cannot see.

Each captured session becomes a small record:

| Field | From |
|---|---|
| kind | The request that opened it — `bugfix`, `feature`, `refactor`, `investigation`, or `unknown` |
| touched paths | Tool calls that changed a file and succeeded. A file that was only read was not changed |
| `touched` edges | The symbols defined in those files |

A request that matches no rule stays `unknown`. An invented category would be
treated downstream as a fact.

### Fixes

A `bugfix` session that actually changed something and did not end on a failing
check leaves a **fix**: symptom, diagnosis, changed paths, and the command that
verified it. Prose describing a bug never becomes a fix, for the same reason
prose describing a procedure never becomes a skill — a record of work has to be
backed by work.

Verification is recorded only when a check went from failing to passing. Most
real fixes have no such check; those are kept and marked unverified rather than
dropped or overclaimed, and they carry lower confidence.

Recall matches on the *shape* of the trouble: quoted values, numbers and paths
are stripped before comparing, so `KeyError: 'slug'` finds `KeyError: 'title'`.
Without that every occurrence looks novel and memory never recognises anything.

Fixes appear on the repository's index page under "What broke before", and are
reachable from the code they repaired through a `fixes` edge.

### Tools an agent should reach for

| Tool | When |
|---|---|
| `recall_fix(symptom)` | Before debugging anything. Paste the error |
| `retrieve_code_context(query, repo, include_source)` | Before changing unfamiliar code |
| `record_work(request, outcome, changed_paths, verified_by)` | After work that mattered |

`skills/archivum-memory/SKILL.md` packages this loop for agents that support
skills. Copy it into `~/.claude/skills/` to have it loaded automatically.
