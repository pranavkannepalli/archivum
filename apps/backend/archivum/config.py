"""Application configuration via pydantic-settings (reads from .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Anthropic ──────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Security ───────────────────────────────────────────────────────────
    jwt_secret: str = "changeme-replace-in-production"
    # Plaintext on first boot; bcrypt hash stored in DB afterwards.
    owner_password: str = "changeme"
    owner_username: str = "admin"

    # ── Token lifetimes ────────────────────────────────────────────────────
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # ── Data paths ─────────────────────────────────────────────────────────
    wiki_dir: Path = Path("/data/wiki")
    raw_dir: Path = Path("/data/raw")
    blob_dir: Path = Path("/data/blobs")
    db_path: Path = Path("/data/archivum.db")
    kuzu_path: Path = Path("/data/kuzu")
    # Parsed-AST cache for indexed repositories. Kept beside the other data
    # rather than inside each repository, so indexing never writes into a
    # working tree you are trying to keep clean.
    code_cache_dir: Path = Path("/data/code-cache")

    # ── Qdrant ─────────────────────────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "archivum"
    qdrant_recreate_collection_on_dim_mismatch: bool = False

    # ── Embeddings ─────────────────────────────────────────────────────────
    # Embeddings can be either local (fastembed) or any OpenAI-compatible provider.
    # Supported: local|openai_compat|openrouter|ollama
    embed_provider: str = "local"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Vector size in Qdrant. Set to 0 to auto-detect from the embedding provider/model.
    embed_dim: int = 0

    # OpenAI-compatible embeddings provider selection (used when embed_provider=openai_compat).
    # We derive a base URL from this for common providers.
    # Supported: openai|together|fireworks|groq|deepinfra|custom|azure
    embed_openai_compat_provider: str = "openai"
    # Optional override (mostly for custom/azure). If empty, we derive from provider.
    embed_base_url: str = ""
    embed_api_key: str = ""
    # Azure OpenAI embeddings support (when embed_provider=openai_compat and base URL is an Azure endpoint).
    embed_azure_api_version: str = "2024-02-15-preview"

    # Ollama base URL (when embed_provider=ollama or llm_*_provider=ollama)
    # Ollama's OpenAI-compat endpoints are typically at `${OLLAMA_BASE_URL}/v1`.
    ollama_base_url: str = "http://localhost:11434"
    # Optional bearer token for hosted Ollama/OpenAI-compatible proxies.
    ollama_api_key: str = ""

    # ── LLM ────────────────────────────────────────────────────────────────
    # LLM provider + model selection is split by pipeline stage so you can,
    # for example, run paid extraction but free/cheaper synthesis (or vice versa).
    #
    # Extraction (entities + relationships):
    # Supported: anthropic|openrouter|openai_compat|ollama
    llm_extraction_provider: str = "anthropic"
    llm_model: str = "claude-haiku-4-5-20251001"

    # Query synthesis (answers with citations):
    # Supported: anthropic|openrouter|openai_compat|ollama|codex_cli|claude_cli
    llm_synthesis_provider: str = "anthropic"
    llm_synthesis_model: str = "claude-sonnet-4-6"

    # OpenRouter (OpenAI-compatible endpoint)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Generic OpenAI-compatible endpoint (covers OpenAI, Together, Fireworks, etc.)
    # Set LLM_*_PROVIDER=openai_compat and provide these values.
    openai_compat_api_key: str = ""
    # Prefer provider-based selection; only set base_url for custom/azure.
    # Supported: openai|together|fireworks|groq|deepinfra|custom|azure
    openai_compat_provider: str = "openai"
    openai_compat_base_url: str = ""
    openai_compat_azure_api_version: str = "2024-02-15-preview"

    # ── Multi-tenancy (future) ─────────────────────────────────────────────
    wiki_id: str = "default"

    # ── Public publishing ─────────────────────────────────────────────────
    # When true, exposes read-only page list/detail APIs for the default wiki.
    public_wiki_enabled: bool = False

    # ── Background workers ────────────────────────────────────────────────
    page_write_worker_enabled: bool = False
    # Periodic retention sweep: expires stale review candidates automatically.
    retention_sweep_enabled: bool = True

    # The vault is meant to be edited by hand, so the app watches it rather than
    # assuming every change came through the API.
    vault_watch_enabled: bool = True
    vault_watch_interval_seconds: int = 5
    # Reconcile at startup catches whatever changed while the app was down.
    vault_reconcile_on_start: bool = True

    # Capture enqueues; this worker does the slow, possibly LLM-backed half.
    distill_worker_enabled: bool = True
    distill_worker_interval_seconds: int = 10
    # Where agents write session transcripts. Empty means session capture is
    # off: the backend usually runs in a container, so this has to be mounted in
    # and named rather than guessed at.
    #
    # Read as a comma-separated list rather than JSON. Compose passes an empty
    # string when the variable is unset, and pydantic's default decoding for a
    # list field treats that as malformed JSON and refuses to start the process
    # at all — a formatting detail should not be able to take the vault down.
    transcript_dirs: Annotated[list[Path], NoDecode] = []

    @field_validator("transcript_dirs", mode="before")
    @classmethod
    def _split_transcript_dirs(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            import json

            try:
                return json.loads(text)
            except ValueError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    transcript_watch_enabled: bool = True
    transcript_watch_interval_seconds: int = 20

    # Cluster summaries: the half of GraphRAG that makes global questions
    # answerable. Hourly by default — themes change over days, and this is the
    # one place real model time is spent.
    summary_worker_enabled: bool = True
    summary_worker_interval_seconds: int = 3600

    code_repo_worker_enabled: bool = True
    code_repo_worker_interval_seconds: int = 15
    retention_sweep_interval_seconds: int = 3600

    # ── Memory distillation ───────────────────────────────────────────────
    # Distillation has a deterministic skeleton that works without model
    # APIs. Atoms at or above the threshold are written to canonical memory
    # as provisional records; every atom still gets a review card.
    memory_atom_confidence_threshold: float = 0.7
    # Optional LLM-assisted evaluator pass (hybrid scoring, semantic typing,
    # additional candidate proposals). Uses llm_extraction_provider; any
    # failure falls back to deterministic scoring.
    memory_llm_evaluator_enabled: bool = False
    # How many distinct sessions a statement must recur in before it is
    # promoted into the L3 owner persona.
    memory_persona_min_sessions: int = 2
    # Minimum successful tool calls before a session counts as a procedure.
    memory_skill_min_tool_calls: int = 3
    # Write editable markdown views for distilled memory assets.
    memory_page_views_enabled: bool = True

    # ── CORS ───────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ── Rate limiting (cap API costs) ─────────────────────────────────────
    # Defaults are conservative; tune via env vars.
    rate_limit_login_requests: int = 10
    rate_limit_login_window_seconds: int = 600  # 10 minutes
    rate_limit_api_requests: int = 120
    rate_limit_api_window_seconds: int = 60

    # ── MCP ────────────────────────────────────────────────────────────────
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001
    mcp_api_key: str = ""

    # How long a writer waits for another writer before giving up. SQLite
    # serialises writes, and this vault always has background workers going, so
    # contention is normal rather than exceptional. The driver's 5s default was
    # short enough that a forced reindex — which holds a connection across Kuzu
    # and Qdrant projection — lost the race and surfaced as a 500.
    sqlite_busy_timeout_seconds: float = 30.0

    # Dense search always returns its top-k, so without a floor a query that
    # matches nothing still comes back with a full page of confident-looking
    # results. Tuned against the default local bge-small model, where genuine
    # matches score ~0.6-0.8 and noise sits at ~0.50.
    search_min_similarity: float = 0.58
    mcp_public_url: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
