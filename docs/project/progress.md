# Archivum Project Progress

_Last updated: 2026-08-18_

Archivum keeps markdown editable for humans while maintaining rebuildable semantic and graph indexes for search, citations, and agent context. Canonical knowledge is owner-centered at `person:self`; retrieval and MCP context preserve citations, confidence, and extraction method.

## Status Vocabulary

- Verified: backed by a current test, command, or manual smoke result.
- Partial: implementation exists, but release behavior still needs proof or has known gaps.
- Started: early implementation exists.
- Unknown: code may exist, but current behavior has not been checked.
- Not built: no implementation found.

## Release Readiness

| Gate | Status | Evidence / owner |
|---|---|---|
| Open-source cleanup | Verified | Private/generated project clutter was removed before this docs pass. |
| Apache 2.0 licensing | Verified | `LICENSE`, npm package metadata, and backend package metadata are Apache-2.0. |
| README product positioning | Verified | README now describes Archivum as a self-hosted, server-hosted Obsidian-style second brain. |
| Docs pruning | Verified | Stale PRD, stale operator handoff, and duplicate root progress doc were removed on 2026-07-13. |
| Agent docs | Verified | `AGENTS.md`, `CLAUDE.md`, and `docs/agent-guide.md` point agents at current docs and verification commands. |
| Docker Compose clean boot | Verified | 2026-08-12 retry: `docker compose up -d --build` built backend, frontend, and MCP images, recreated the app containers, and left backend, frontend, MCP, Caddy, Qdrant, and Ollama running. Probes returned HTTPS frontend 200, protected `/api/pages` 401 without auth, direct frontend 200, and MCP SSE 200 with the endpoint event using the configured bearer key. |
| Ingest to wiki to query loop | Verified | 2026-08-12 current-code tests verify file/URL ingest writes source-backed canonical records, `/api/rebuild-indexes` resyncs canonical markdown pages before projection rebuild, retrieval refuses uncited/insufficient evidence, and MCP query shares the REST cited-answer path. |
| Backend pytest suite | Verified | 2026-08-12: `cd apps/backend && uv run --group dev pytest ../../tests -q`: 495 passed, with two upstream `websockets`/`uvicorn` deprecation warnings. |
| Frontend tests/build | Verified | 2026-08-12: `npm test --workspace apps/frontend`: 59 passed; `npm run build --workspace apps/frontend`: passed with existing large chunk warning. |
| CLI tests | Verified | 2026-08-12: `npm test --workspace packages/archivum-cli`: 18 passed. |

## Product Surface

| Area | Status | Evidence |
|---|---|---|
| Markdown wiki pages | Verified | REST auth/list, ingest-created page persistence, search, and cited query were smoke-tested on 2026-07-12. |
| Vault navigation | Partial | Folder/page APIs and file tree UI exist. Browser click-through still needs manual smoke. |
| Wikilinks and backlinks | Partial | CodeMirror wikilink extension exists; 2026-08-12 current-code smoke preserved edited wikilinks and canonical context contained the `references` edge. Follow-up regression coverage verifies legacy graph sync slugifies display-text wikilinks before backlink indexing. Browser editor click-through still needs manual smoke. |
| Ingest files and URLs | Partial | Backend parser coverage now includes broad text/docs/Office/data/code/email/subtitle/image/media support plus ZIP archive members, RTF/XML/logs, timestamped audio/video transcripts when optional dependencies are installed, and downloadable document URLs routed through file parsers. Canonical ingest records source/page/entity/relationship evidence with `EXTRACTED` provenance. Browser release smoke has covered representative markdown file ingest; URL and full format matrix still need product-level smoke. |
| Search | Partial | Qdrant semantic search returned freshly ingested marker text. Hybrid behavior has backend coverage across keyword/vector/graph fusion, scoped context, and insufficient-evidence handling; product-level search smoke still needs browser confirmation. |
| Query with citations | Verified | Query SSE returned citations including the source page and answered with the marker; canonical context carries citations, confidence, and extraction method. REST and MCP query paths now share cited context preparation and return insufficient evidence instead of fabricating citations. |
| Graph | Partial | 2026-08-12 local current-code context smoke opened a bounded package seeded at `person:self` with 4 nodes and `authored_thought`/`owns_project` edges. Legacy graph API and browser graph view still need rebuilt Docker/browser smoke. |
| Sharing | Partial | 2026-08-20: grants replace ad-hoc share links. Principals, per-resource and per-folder grants with inheritance, viewer/commenter roles, agent-write review holds, claim sessions, and recipient-only routing are covered by 82 backend tests including the invariant that a recipient token is refused by every owner router. Legacy `share_links` migrate on boot. Browser smoke still pending. |
| Export/public wiki | Partial | Public pages, HTML export, and PDF export code exist. Needs manual release smoke. |
| MCP server | Verified | Docker MCP SSE endpoint returned the session endpoint event; backend stdio smoke is covered by pytest. MCP SSE now enforces configured bearer auth and Docker publishes MCP on localhost by default. |
| Memory assets | Verified | Typed, owned, versioned assets with status/visibility governance, version history, and canonical projection sharing the asset id. Backend, REST, MCP, and frontend covered by tests on 2026-08-12. |
| Memory catalog | Verified | `POST /api/memory/catalog` registers existing markdown pages, ingested/captured sources, and per-repo code graphs as typed assets. Ids are derived, so re-running produces no version churn. |
| Session distillation | Verified | Captured conversations distil into cited L1 atoms, L2 scenario memory, and an L3 persona. Sub-threshold atoms are routed to the existing review queue rather than written. Deterministic: no LLM call on this path. |
| Skill memory | Verified | Skills are extracted only from sessions with real successful tool calls and no recorded failure; steps come from the recorded calls, and skills register as `draft` pending human activation. |
| Agent loadouts | Verified | Agent profiles, `always`/`on_demand` bindings, and loadout resolution that returns only active bound assets with citations plus an explicit reason when empty. |
| Graph audit | Verified | Communities (greedy modularity), shortest path (BFS), surprising links, and a plain-language provenance report over canonical knowledge; REST, MCP, and a Tools UI tab. Deterministic: no LLM call. |
| Life OS workflows | Started | Daily/projects/tasks routes and UI exist. They are not the main public positioning. |

## Verification Log

Add new entries with the exact command and result.

| Date | Check | Result |
|---|---|---|
| 2026-07-12 | Frontend tests | `npm test --workspace apps/frontend`: 29 passed. |
| 2026-07-12 | Frontend build | `npm run build --workspace apps/frontend`: passed with existing large chunk warning. |
| 2026-07-12 | Docker clean boot | `docker compose down && docker compose up -d --build`: passed after frontend/backend startup ordering fix. |
| 2026-07-12 | Runtime endpoints | `https://localhost` returned 200, protected REST returned expected 401 before login, authenticated `/api/pages` returned 200, and MCP `/sse` emitted endpoint event. |
| 2026-07-12 | Ingest/search/query | Uploaded `source.md`; ingest history completed with 1 page created; search and query found the marker with source citation. |
| 2026-07-12 | Backend pytest | Clean container copy ran `uv run --group dev pytest /tmp/workspace/tests -q`: 258 passed. |
| 2026-07-13 | Docs update | README, docs index, architecture docs, progress, and agent docs were updated; stale PRD/operator/root progress docs were removed. |
| 2026-08-11 | Frontend tests | `npm test --workspace apps/frontend`: 33 passed. |
| 2026-08-11 | Frontend build | `npm run build --workspace apps/frontend`: passed with existing large chunk warning. |
| 2026-08-11 | CLI tests | `npm test --workspace packages/archivum-cli`: 17 passed. |
| 2026-08-11 | Backend pytest | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 358 passed. |
| 2026-08-11 | Docker boot and endpoints | `docker compose up -d --build`: passed; `https://localhost` returned 200; unauthenticated `/api/pages` returned 401; authenticated `/api/pages` returned 200; MCP `http://localhost:8001/sse` emitted endpoint event. |
| 2026-08-11 | Ingest/search/query | Uploaded `archivum-smoke-source.md`; ingest history completed with 1 page created; search found `ARCHIVUM_SMOKE_MARKER_20260811`; `/api/query` emitted citations and streamed tokens through hosted Ollama-compatible config. |
| 2026-08-11 | Page backlinks | Created fresh source/target pages; `/api/pages/{target}/backlinks` returned the source page. |
| 2026-08-11 | Recovery backup/validate | `archivum recovery backup --dir=.context/recovery-smoke-20260811-1615` created config and precious-volume archives; `archivum recovery validate .context/recovery-smoke-20260811-1615` passed; stack returned healthy with web 200, protected API 401, and MCP SSE endpoint event. |
| 2026-08-12 | Backend pytest | `cd apps/backend && uv run --group dev pytest ../../tests -q`: first run failed during collection with import mismatch for duplicate `test_models.py` and `test_repository.py`; after adding `tests/knowledge/__init__.py` and `tests/store/__init__.py`, rerun passed with 469 passed in 7.89s. |
| 2026-08-12 | Frontend tests | `npm test --workspace apps/frontend`: 11 test files passed, 54 tests passed. |
| 2026-08-12 | Frontend build | `npm run build --workspace apps/frontend`: passed; Vite reported the existing warning that some chunks exceed 500 kB after minification. |
| 2026-08-12 | CLI tests | `npm test --workspace packages/archivum-cli`: 18 passed, 0 failed. |
| 2026-08-12 | Docker build/start | Retry passed: `docker compose up -d --build` built backend, frontend, and MCP images, recreated the app containers, and left backend, frontend, MCP, Caddy, Qdrant, and Ollama running. |
| 2026-08-12 | Rebuilt Docker endpoints | `https://localhost/` returned 200; unauthenticated `https://localhost/api/pages` returned 401; `http://127.0.0.1:8473/` returned 200; `http://localhost:8001/sse` with the configured MCP bearer key returned 200 and emitted `event: endpoint` before curl timed out at 5 seconds. |
| 2026-08-12 | Pre-existing Docker endpoints | Existing running stack probe: `https://localhost/` returned 200; unauthenticated `https://localhost/api/pages` returned 401; login returned 200; authenticated `https://localhost/api/pages` returned 200; `http://localhost:8001/sse` with MCP bearer key emitted `event: endpoint` before curl timed out at 5 seconds. |
| 2026-08-12 | Local current-code product smoke | Local backend from current workspace on `127.0.0.1:18080` with isolated `/tmp/archivum-task12-local` data: login 200; target page create 201; owner project page create 201; page edit 200; page fetch 200 and markdown edit marker/wikilinks preserved; markdown file ingest 200 accepted; search 200 found `ARCHIVUM_TASK12_INGEST_MARKER_20260811212638`; `/api/context-package` 200 returned 4 bounded nodes including `person:self`, the owner project, linked target, and ingested page with `references`, `owns_project`, and `authored_thought` edges; `seed_ids:["person:self"]` context returned `person:self` plus owner-centered edges; `/api/retrieve` 200 returned 5 hits with citations and `insufficient_evidence:false`; `/api/query` SSE emitted citations and an answer containing `ARCHIVUM_TASK12_PAGE_MARKER_20260811212638`. Legacy `/api/pages/{target}/backlinks` returned 200 with `[]` for the display-text wikilink during this smoke, even though canonical context contained the reference edge; this was fixed by the follow-up display-text wikilink regression below. |
| 2026-08-12 | Display-text wikilink backlink regression | `cd apps/backend && uv run --group dev pytest ../../tests/test_pages_backlinks.py ../../tests/api/test_system.py -q`: 21 passed. Legacy page sync and rebuild-index paths now slugify `[[Target Page|Target]]` before adding `REFERENCES` edges, matching canonical markdown projection behavior. |
| 2026-08-12 | Suggestion lifecycle | Product REST routes and frontend clients now list, create, accept, and reject scoped suggestions through explicit user action. `cd apps/backend && uv run --group dev pytest ../../tests/api/test_suggestions.py ../../tests/knowledge/test_suggestions.py -q`: 11 passed. |
| 2026-08-12 | Remaining parity backend suite | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 495 passed, with two upstream `websockets`/`uvicorn` deprecation warnings. |
| 2026-08-12 | Remaining parity frontend tests | `npm test --workspace apps/frontend`: 11 test files passed, 59 tests passed. |
| 2026-08-12 | Remaining parity frontend build | `npm run build --workspace apps/frontend`: passed; Vite reported the existing warning that some chunks exceed 500 kB after minification. |
| 2026-08-12 | Remaining parity CLI tests | `npm test --workspace packages/archivum-cli`: 18 passed, 0 failed. |
| 2026-08-12 | Code graph namespacing and cleanup | `cd apps/backend && uv run --group dev pytest ../../tests/archgraph ../../tests/knowledge/test_repository.py -q`: 70 passed. Code node IDs are now namespaced by repository scope and relative path; incremental ingest cleans canonical records for changed/deleted/renamed files, preserves lexical rows for unchanged files, and preserves untouched caller-owned inferred edges when callee files change. |
| 2026-08-12 | Query/MCP insufficient evidence | `cd apps/backend && uv run --group dev pytest ../../tests/api/test_query.py ../../tests/retrieval/test_hybrid.py ../../tests/mcp_tests/test_server.py -q`: 22 passed. MCP query uses the shared REST cited synthesis path; REST query no longer falls back to out-of-scope hits or fabricates `[1]`. |

| 2026-08-12 | Memory asset parity backend suite | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 616 passed (up from 495), with the two pre-existing upstream `websockets`/`uvicorn` deprecation warnings. New coverage: `tests/memory/` (60), `tests/knowledge/test_graph_audit.py` (22), `tests/api/test_memory_api.py` (12), `tests/api/test_graph_audit_api.py` (7), `tests/mcp_tests/test_memory_tools.py` (8). |
| 2026-08-12 | Memory asset parity frontend tests | `npm test --workspace apps/frontend`: 14 test files passed, 78 tests passed (up from 59), adding `src/memory-api.test.ts` and `src/memory-pages.test.tsx`. |
| 2026-08-12 | Memory asset parity frontend build | `npm run build --workspace apps/frontend`: passed; Vite reported the existing warning that some chunks exceed 500 kB after minification. |
| 2026-08-12 | Memory asset parity CLI tests | `npm test --workspace packages/archivum-cli`: 18 passed, 0 failed. |
| 2026-08-12 | Docs staleness scan | `rg -n "scripts/[b]ootstrap\|[N]eo4j\|[y]ou@youremail\|[L]ast updated: 2026-06\|[f]eature complete" -g "*.md" -g "!node_modules/**" -g "!apps/backend/.venv/**"`: no matches. |
| 2026-08-14 | Clean-memory strategy review fixes backend | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 653 passed. Covers the `person:self` context scope, the review gate on durable promotion (all atoms get review cards, canonical writes are provisional, distilled assets start as drafts, activation/acceptance stamps `approved_by`/`reviewed_at`), targeted merge/replace/retire with scope/visibility overrides, per-scope token/item budgets and pending-review exclusion in context packages, the automatic retention sweep, and the hybrid LLM evaluator with strategy-aligned semantic types. |
| 2026-08-14 | Clean-memory strategy review fixes frontend | `npm test --workspace apps/frontend`: 16 files, 89 tests passed. `npm run build --workspace apps/frontend`: passed. Review cards now support edit-then-accept, scope picker, visibility picker, and merge/replace/retire target selection. |
| 2026-08-14 | Clean-memory strategy review fixes CLI | `npm test --workspace packages/archivum-cli`: 18 passed, 0 failed. |
| 2026-08-14 | Conflict detection and librarian feedback | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 660 passed. `npm test --workspace apps/frontend`: 89 passed; build passed. Distillation now flags token-overlap duplicates and polarity-flip conflicts against existing canonical atoms on review cards; merge/replace/retire resolve implicit targets by filtering to registered assets; distill reports include `conflicts_flagged`/`sentences_scanned` and Home shows a librarian-style capture summary. |
| 2026-08-18 | Expanded ingest parser matrix | `cd apps/backend && uv run --group dev pytest ../../tests/ingest/test_parsers.py ../../tests/store/test_normalize.py -q`: 58 passed. Added backend coverage for RTF/XML text extraction, ZIP archives with unsupported-member reporting, timestamped Whisper transcript output, graceful missing-ffmpeg video errors, downloadable PDF URLs routed through the file parser, and normalized MIME mapping for expanded ingest types. |
| 2026-08-18 | Frontend ingest picker matrix | `npm test --workspace apps/frontend -- IngestPanel.test.tsx`: 1 passed. The file picker now advertises and accepts the broad backend ingest matrix instead of only markdown/text/PDF/HTML/DOCX. |
| 2026-08-18 | Settings-installed audio/video support | `cd apps/backend && uv run --group dev pytest ../../tests -q`: 678 passed, with two upstream `websockets`/`uvicorn` deprecation warnings. `npm test --workspace apps/frontend`: 17 files, 92 tests passed. `npm run build --workspace apps/frontend`: passed with the existing large chunk warning. Audio/video dependencies are now installable from Settings through an owner-only API action; the UI no longer displays manual install commands. |

## Known Gaps

- Distillation runs on demand through `POST /api/memory/distill` or the `distill_source` MCP tool. There is no background worker that distils captured sessions automatically. (A background retention sweep does now expire stale review candidates automatically.)
- The Tools → Memory and Tools → Audit surfaces have backend and server-render coverage but still need browser click-through smoke.
- Cataloguing existing pages, sources, and code graphs into the asset registry is an explicit action (`POST /api/memory/catalog`, the `catalog_memory_assets` MCP tool, or the Tools → Memory button). Nothing registers them automatically on write.

## What To Build Next

1. Smoke page CRUD, autosave, backlinks, and vault drawer in the browser.
2. Smoke the Settings LLM provider form in the browser.
3. Smoke URL ingest and a representative file-format matrix in the browser/release stack.
4. Smoke graph UI, share links, public wiki, HTML export, and PDF export.
