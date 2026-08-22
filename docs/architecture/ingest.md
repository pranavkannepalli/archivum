# Ingest Pipeline

Archivum ingests files and URLs into editable canonical markdown pages, then projects their content and provenance into canonical knowledge rows and rebuildable search and graph indexes.

Markdown pages are the human editing surface. Canonical knowledge rows preserve the owner profile, page-authored content, projects, thoughts, extracted entities, relationships, citations, confidence, and extraction method. Qdrant, Kuzu, FTS, and code lexical indexes are rebuildable projections.

## Flow

1. Parse the source into clean text and metadata.
2. Store the raw bytes as immutable L0 evidence and register the source as a governed memory asset.
3. Send text to the configured extraction LLM.
4. Receive wiki pages, entities, and relationships as structured JSON.
5. Write editable markdown pages to the wiki directory.
6. Project page-authored content and extracted knowledge into canonical rows, citing the chunks of the stored source they came from.
7. Update operational metadata and FTS in SQLite.
8. Project semantic vectors into Qdrant, graph nodes and edges into Kuzu, and code entities into the rebuildable code lexical index.

Step 2 is what joins the wiki pipeline to the evidence store. Both used to exist and never met: `/api/ingest` minted its own provenance id while `/api/sources/ingest` stored the bytes, so a page derived from a dropped file cited an id no store had ever heard of. Now there is one source id per ingest, the bytes behind it are kept, and every derived record cites a chunk that exists — which is what makes `GET /api/sources/{id}/derived` able to answer for a source's own output.

Ingest keeps the name the user brought in as the origin, not the upload's temp path, so re-uploading identical bytes deduplicates instead of forking the evidence.

Primary files:

| Concern | Path |
|---|---|
| Parser dispatch | `apps/backend/archivum/ingest/parsers.py` |
| Extraction prompt/client | `apps/backend/archivum/ingest/agent.py` |
| Pipeline orchestration | `apps/backend/archivum/ingest/pipeline.py` |
| Evidence store (L0/L1) | `apps/backend/archivum/store/ingest.py` |
| Shared asset registration | `apps/backend/archivum/memory/catalog.py` |
| REST ingest routes | `apps/backend/archivum/api/ingest.py` |
| Frontend ingest panel | `apps/frontend/src/components/IngestPanel.tsx` |

## Supported Sources

Backend parser support:

| Category | Formats |
|---|---|
| Text | `.md`, `.txt`, `.rst`, `.text`, `.log`, `.rtf`, `.xml`, `.rss`, `.atom` |
| Documents | `.pdf`, `.html`, `.htm`, `.epub` |
| Office | `.docx`, `.pptx`, `.xlsx`, `.xls` |
| Data | `.csv`, `.json`, `.jsonl` |
| Archives | `.zip` archives containing any supported file type |
| Code/config | `.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.go`, `.rs`, `.sh`, `.bash`, `.zsh`, `.rb`, `.java`, `.c`, `.cpp`, `.h`, `.hpp`, `.kt`, `.swift`, `.php`, `.sql`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.jsonc`, `.css`, `.scss`, `.sass`, `.less`, `.mjs`, `.cjs`, `.vue`, `.svelte`, `.mdx` |
| Subtitles | `.srt`, `.vtt` |
| Email | `.eml`, `.mbox` |
| Images | `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif` |
| Audio | `.mp3`, `.m4a`, `.wav`, `.ogg`, `.flac`, `.aac`, `.aiff`, `.opus`, `.wma` |
| Video | `.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, `.m4v`, `.mpg`, `.mpeg`, `.3gp` |
| URLs | HTML, JSON, plain text, markdown, supported downloadable documents, and text fallback |

ZIP ingest parses supported members into one normalized document and reports unsupported members in the extracted text/metadata. Downloadable document URLs are saved temporarily and routed through the same file parser as uploads, so PDF/EPUB/Office/ZIP/RTF/XML links preserve parser-specific output instead of falling back to opaque response text.

## Optional Media Dependencies

- Image parsing uses Anthropic vision and requires `ANTHROPIC_API_KEY`.
- Audio parsing requires the `audio` optional dependency and emits timestamped transcript segments when Whisper returns segment metadata.
- Video parsing requires the `audio` optional dependency and system `ffmpeg`; missing or failed ffmpeg extraction is reported as an unsupported media parse instead of a generic ingest crash.
- Published Docker images omit Whisper, Torch, and ffmpeg.

## Graph Edges

Ingest creates these graph relationships:

| Edge | Source |
|---|---|
| `Page -[:REFERENCES]-> Page` | `[[wikilink]]` syntax when the target page exists |
| `Page -[:MENTIONS]-> Entity` | Case-insensitive entity-name match in page content |
| `Entity -[:RELATED_TO]-> Entity` | Extraction LLM `relationships[]` output |

If a wikilink target is created later, run the index refresh endpoint/CLI to add the legacy page-based `REFERENCES` edge projection. The current endpoint/CLI upserts page vectors, page nodes, and reference edges; it does not remove stale page projections or references, update entity/mention/relationship projections, or rebuild canonical knowledge projections, FTS, or the code lexical index.
