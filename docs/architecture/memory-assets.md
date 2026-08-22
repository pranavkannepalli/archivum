# Memory Assets

Markdown stays the human editing surface and canonical knowledge stays the source of truth. Memory assets add the missing governance layer: every durable unit of memory is typed, owned, versioned, reviewable, and equippable by a named agent.

The pipeline's skeleton is deterministic and works without model APIs: extraction is rule-based, summaries are assembled from the records they cite, and graph analysis is a deterministic algorithm. An optional LLM-assisted evaluator pass (`MEMORY_LLM_EVALUATOR_ENABLED`) re-scores extracted atoms on the strategy's dimensions, assigns semantic types (profile, preference, decision, principle, fact, procedure, skill, relationship, source_summary, code_insight), and proposes additional candidates — all routed to review cards, never written directly. Any evaluator failure falls back to deterministic scoring.

Promotion is review-gated: every atom gets a review card, above-threshold atoms are written to canonical knowledge only as provisional records (`review_state: pending`), distilled assets register as drafts, and context packages exclude pending-review records.

The `person:self` scope is a deployment-wide singleton by design: Archivum models one human, and owner-role accounts across wikis curate that same personal memory. Collaborator accounts can neither read the `person:self` context scope nor promote objects into it. Hosting multiple distinct humans on one deployment is not supported; it would require namespacing the person scope per owner.

## Layers

| Layer | What it holds | Where it lives |
|---|---|---|
| L0 | Raw captured bytes | Content-addressed blobs (`blob_dir`), addressable as `source` assets |
| L1 | Evidence lineage and memory atoms | `sources` / `documents` / `chunks`, plus `memory_atom` knowledge objects |
| L2 | Scenario and skill memory | `memory_scenario` and `memory_skill` knowledge objects |
| L3 | Owner persona | The `memory:persona:self` knowledge object |

Every layer above L0 is addressable as a memory asset. An asset shares its id with the canonical knowledge object it governs, so provenance and governance never drift apart.

## Asset Model

`memory_assets` rows carry:

| Field | Meaning |
|---|---|
| `asset_type` | `wiki`, `chat`, `skill`, `codegraph`, `source`, `scenario`, `persona` |
| `layer` | `L0`–`L3` |
| `owner` | Whose memory this is — `person:self` for everything the owner owns |
| `scope` | Which vault or repository it belongs to, e.g. `wiki:default` |
| `status` | `draft`, `active`, `archived` |
| `visibility` | `private`, `shared`, `public` |
| `version` | Bumped only when content changes |
| `page_slug` | Optional editable markdown view |
| `citations` | Evidence the asset rests on |

Status and visibility are governance state, not content: changing them does not create a version, and editing content does not silently reactivate an archived asset. Every content change is snapshotted in `memory_asset_versions`.

`owner` and `scope` answer different questions and are not interchangeable. Every asset is owned by `person:self` and scoped to the wiki it lives in, so "what are my agents told about me?" is an `owner` query — `GET /api/memory/assets?owner=person:self`, which is what the profile page uses. Filtering assets on `scope=person:self` matches nothing.

## Governance Travels With the Write

A page becomes a governed asset when it is indexed, and an ingested source becomes one when its bytes are stored. Both go through the same registration used by the catalog pass (`register_page_asset` / `register_source_asset` in `memory/catalog.py`), so ingest and catalog cannot describe the same object under two ids or two kinds.

That makes cataloguing a repair path rather than a routine one, in the same way `reconcile_vault` is the repair path for indexing. It is still needed to backfill a vault written before this held, and to register code graphs. **Settings → Vault repair** runs the whole catch-up: a forced vault reindex followed by a catalog pass. Running it on an up-to-date vault is a no-op.

## Cataloguing Existing Memory

Wiki pages, ingested sources, and code graphs were memory before the registry existed. `POST /api/memory/catalog` (or the `catalog_memory_assets` MCP tool) registers them as governed assets:

| Existing memory | Asset id | Type / layer |
|---|---|---|
| Markdown page | `page:{wiki}:{slug}` — the canonical page object's own id | `wiki` / L1 |
| Ingested or captured source | `source:{source_id}` | `source` / L0 |
| Code graph for a repo scope | `codegraph:repo:{name}` | `codegraph` / L2 |

Ids are derived rather than generated, so cataloguing is idempotent: re-running produces no version churn. Pages under `memory/` and `skills/` are skipped because they already back a distilled asset, and one unit of memory must not appear under two ids. Source and code-graph assets also get a canonical object linked to `person:self` (`owns_asset`, `uses_code`), so they show up in graph traversal and audit.

## Distillation

`POST /api/memory/distill` promotes one captured conversation:

1. **Atoms (L1).** Sentence-level rules classify owner statements as `decision`, `preference`, `constraint`, `fact`, or `outcome`. Each atom keeps the exact quote and a span into the transcript chunk it came from. Assistant turns are mined for decisions only.
2. **Threshold.** Atoms at or above `MEMORY_ATOM_CONFIDENCE_THRESHOLD` are written to canonical knowledge. Weaker atoms become suggestions in the existing human review queue instead of being written silently.
3. **Recurrence.** The same statement in a second session merges into one atom, records both session ids, and gains a bounded confidence lift.
4. **Scenario (L2).** Accepted atoms aggregate into project or scenario memory, linked to each atom by a `contains_atom` relationship.
5. **Persona (L3).** Preferences, facts, and constraints seen in at least `MEMORY_PERSONA_MIN_SESSIONS` distinct sessions are promoted into the owner profile. One offhand remark is not an identity.
6. **Skill.** See below.

Every produced object is linked back to `person:self` (`remembers`, `describes_self`, `learned_skill`) and written as an editable markdown page under `memory/` or `skills/` when page views are enabled. A failed page write never blocks the canonical record.

## Skill Memory

A skill is extracted only from work that actually happened:

- at least `MEMORY_SKILL_MIN_TOOL_CALLS` successful tool calls were recorded,
- the session did not end in a recorded failure,
- a user request exists to serve as the trigger.

Steps come from the recorded tool calls in order, deduplicated by (tool, salient argument). Validation is recovered from verification-shaped steps and successful outcomes. Prose describing a procedure never becomes a skill.

Skills are registered as **draft**. Activation stays a human decision.

## Agent Loadouts

An agent profile is bound to specific assets:

- `always` bindings are handed over unconditionally,
- `on_demand` bindings are handed over when the session query matches the asset's text.

Only `active` assets in the caller's wiki are ever returned. An agent with no profile is not turned away empty-handed: it receives the vault's `active` assets, on demand, with a reason saying no profile was set. Active is the same bar a bound asset must clear — a draft is a proposal, and an agent is handed decisions rather than proposals. Without this a fresh vault answered every loadout with nothing, because no screen creates profiles.

`GET /api/memory/agents/{key}/loadout` and the `load_agent_memory` MCP tool return the bound assets with their citations, plus an explicit reason whenever the loadout is empty or uncited. This is what lets the next agent inherit the last agent's experience without loading the whole store into context.

## REST

| Route | Purpose |
|---|---|
| `GET/POST /api/memory/assets` | List and register assets; `GET` filters on `asset_type`, `layer`, `status`, `owner`, `scope`, `page_slug` |
| `GET /api/memory/assets/{id}` | Fetch one asset |
| `GET /api/memory/assets/{id}/versions` | Version history |
| `POST /api/memory/assets/{id}/status` | `draft` / `active` / `archived` |
| `POST /api/memory/assets/{id}/visibility` | `private` / `shared` / `public` |
| `GET/POST /api/memory/agents` | List and upsert agent profiles |
| `GET/POST /api/memory/agents/{key}/bindings` | Read and set bindings |
| `DELETE /api/memory/agents/{key}/bindings/{asset_id}` | Remove a binding |
| `GET /api/memory/agents/{key}/loadout` | Resolve the loadout |
| `POST /api/memory/catalog` | Register existing pages, sources, and code graphs as assets |
| `POST /api/memory/distill` | Distil a captured source |

## Settings

| Setting | Default | Effect |
|---|---|---|
| `MEMORY_ATOM_CONFIDENCE_THRESHOLD` | `0.7` | Below this, atoms go to review instead of canonical memory |
| `MEMORY_PERSONA_MIN_SESSIONS` | `2` | Sessions a statement must recur in before it reaches the persona |
| `MEMORY_SKILL_MIN_TOOL_CALLS` | `3` | Successful tool calls needed before a session counts as a procedure |
| `MEMORY_PAGE_VIEWS_ENABLED` | `true` | Write editable markdown views for distilled memory |
