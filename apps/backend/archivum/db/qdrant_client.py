"""Qdrant wrapper: collection management, upsert, search, delete."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from fastembed import TextEmbedding
import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from archivum.config import Settings, get_settings
from archivum.markdown_text import lede, strip_frontmatter

logger = logging.getLogger(__name__)

# ── Module-level singletons ───────────────────────────────────────────────────

_client: AsyncQdrantClient | None = None
_embedder: TextEmbedding | None = None
_embed_dim_cache: dict[tuple[str, str, str], int] = {}


def get_embedder(settings: Settings | None = None) -> TextEmbedding:
    global _embedder
    if _embedder is None:
        s = settings or get_settings()
        _embedder = TextEmbedding(s.embed_model)
    return _embedder


def _ollama_openai_base_url(base_url: str) -> str:
    normalized = (base_url or "").rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _resolve_embed_endpoint(s: Settings) -> tuple[str, str, dict[str, str]]:
    """
    Return (base_url, api_key, extra_headers) for OpenAI-compatible embeddings.

    Providers:
      - openai_compat: use EMBED_BASE_URL / EMBED_API_KEY
      - openrouter: reuse OpenRouter base URL + key
      - ollama: use OLLAMA_BASE_URL + /v1, no key by default
    """
    provider = (s.embed_provider or "local").strip().lower()
    if provider == "openrouter":
        return (s.openrouter_base_url.rstrip("/"), s.openrouter_api_key, {})
    if provider == "ollama":
        return (_ollama_openai_base_url(s.ollama_base_url), s.ollama_api_key, {})
    # default: openai_compat
    base_url = (s.embed_base_url or "").rstrip("/")
    if not base_url:
        p = (s.embed_openai_compat_provider or "openai").strip().lower()
        derived = {
            "openai": "https://api.openai.com/v1",
            "together": "https://api.together.xyz/v1",
            "fireworks": "https://api.fireworks.ai/inference/v1",
            "groq": "https://api.groq.com/openai/v1",
            "deepinfra": "https://api.deepinfra.com/v1/openai",
        }.get(p, "https://api.openai.com/v1")
        base_url = derived
    return (base_url, s.embed_api_key, {})


def _is_azure_openai_base_url(base_url: str) -> bool:
    return "openai.azure.com" in (base_url or "").lower()


async def resolve_embed_dim(s: Settings) -> int:
    """
    Resolve the effective embedding vector dimension.

    - If settings.embed_dim > 0, use it.
    - If settings.embed_dim <= 0, probe the configured provider/model once and cache the result.
    """
    configured = int(getattr(s, "embed_dim", 0) or 0)
    if configured > 0:
        return configured

    provider = (s.embed_provider or "local").strip().lower()
    if provider == "local":
        cache_key = (provider, (s.embed_model or "").strip(), "")
    else:
        base_url, _, _ = _resolve_embed_endpoint(s)
        cache_key = (provider, (s.embed_model or "").strip(), base_url.rstrip("/"))

    cached = _embed_dim_cache.get(cache_key)
    if cached:
        return cached

    # Probe a single vector.
    if provider == "local":
        embedder = get_embedder(s)
        vec = next(embedder.embed(["dimension probe"]))
        dim = int(getattr(vec, "shape", [len(vec)])[0] if hasattr(vec, "shape") else len(vec))
    else:
        vectors = await _embed_texts_openai_compat(["dimension probe"], s)
        dim = int(len(vectors[0]) if vectors else 0)

    if dim <= 0:
        raise RuntimeError(f"Could not resolve embedding dimension (provider={provider} model={s.embed_model})")

    _embed_dim_cache[cache_key] = dim
    logger.info(
        "Resolved embedding dimension",
        extra={"provider": provider, "model": s.embed_model, "embed_dim": dim},
    )
    return dim


async def _embed_texts_openai_compat(texts: list[str], s: Settings) -> list[list[float]]:
    base_url, api_key, extra_headers = _resolve_embed_endpoint(s)
    if not base_url:
        raise RuntimeError("Embeddings base URL could not be derived/configured")

    # OpenAI compat: POST {base_url}/embeddings
    url = f"{base_url}/embeddings"

    headers: dict[str, str] = {"Accept": "application/json", **extra_headers}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    params: dict[str, Any] | None = None
    if _is_azure_openai_base_url(base_url):
        # Azure OpenAI uses api-key header and api-version query param.
        # They often require a deployment name instead of a model id; we keep
        # `embed_model` as "deployment name" for Azure users.
        if api_key:
            headers.pop("Authorization", None)
            headers["api-key"] = api_key
        params = {"api-version": s.embed_azure_api_version}

    payload = {"model": s.embed_model, "input": texts}
    logger.info(
        "Embedding via OpenAI-compatible provider",
        extra={
            "provider": s.embed_provider,
            "base_url": base_url,
            "model": s.embed_model,
            "count": len(texts),
        },
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, params=params, json=payload)
        resp.raise_for_status()
        data = resp.json()

    items = (data.get("data") if isinstance(data, dict) else None) or []
    vectors: list[list[float]] = []
    for item in items:
        emb = item.get("embedding") if isinstance(item, dict) else None
        if isinstance(emb, list):
            vectors.append([float(x) for x in emb])

    if len(vectors) != len(texts):
        raise RuntimeError(f"Embeddings API returned {len(vectors)} vectors for {len(texts)} inputs")
    return vectors


async def get_client(settings: Settings | None = None) -> AsyncQdrantClient:
    global _client
    if _client is None:
        s = settings or get_settings()
        _client = AsyncQdrantClient(url=s.qdrant_url)
    return _client


async def _semantic_query_points(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    query_vector: list[float],
    query_filter: Filter,
    limit: int,
    with_payload: bool = True,
) -> list[Any]:
    """
    Qdrant client API compatibility shim.

    `qdrant-client` async API uses `query_points()` (returning QueryResponse) in
    newer versions. Some older variants exposed `search()` / `search_points()`.
    """
    if hasattr(client, "query_points"):
        resp = await client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=with_payload,
        )
        return list(getattr(resp, "points", []) or [])

    # Fallbacks (older client variants)
    if hasattr(client, "search"):
        return await client.search(  # type: ignore[attr-defined]
            collection_name=collection_name,
            query_vector=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=with_payload,
        )

    if hasattr(client, "search_points"):
        resp = await client.search_points(  # type: ignore[attr-defined]
            collection_name=collection_name,
            vector=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=with_payload,
        )
        return list(getattr(resp, "points", []) or [])

    raise AttributeError(
        "Unsupported Qdrant async client: expected one of query_points/search/search_points"
    )


# ── Chunking ─────────────────────────────────────────────────────────────────

def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split *text* into overlapping token-approximate chunks.

    We approximate tokens as whitespace-separated words (1 word ≈ 1.3 tokens).
    """
    words = text.split()
    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk.strip():
            chunks.append(chunk.strip())
        i += step
    return chunks or [text[:2000]]  # fallback for very short docs


# ── Embedding helper ─────────────────────────────────────────────────────────

async def embed_texts(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    """
    Embed *texts* using either local fastembed or an OpenAI-compatible endpoint.
    """
    s = settings or get_settings()
    provider = (s.embed_provider or "local").strip().lower()

    if provider == "local":
        loop = asyncio.get_running_loop()
        embedder = get_embedder(s)
        logger.debug(
            "Embedding texts (local)",
            extra={"count": len(texts), "model": getattr(embedder, "model_name", None)},
        )

        def _embed() -> list[list[float]]:
            return [v.tolist() for v in embedder.embed(texts)]

        return await loop.run_in_executor(None, _embed)

    return await _embed_texts_openai_compat(texts, s)


# ── Collection init ───────────────────────────────────────────────────────────

async def init_collection(settings: Settings | None = None) -> None:
    """Create the Qdrant collection if it does not already exist."""
    s = settings or get_settings()
    client = await get_client(s)
    embed_dim = await resolve_embed_dim(s)

    try:
        exists = await client.collection_exists(s.qdrant_collection)
        if not exists:
            await client.create_collection(
                collection_name=s.qdrant_collection,
                vectors_config=VectorParams(
                    size=embed_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info("Created Qdrant collection '%s'", s.qdrant_collection)
        else:
            # If embed_dim changed (e.g. you switched embedding models), Qdrant
            # needs to have vectors_config.size match, otherwise upserts/search
            # will fail or produce incorrect results.
            try:
                desc = await client.get_collection(s.qdrant_collection)
                # qdrant-client model shapes vary by version; try a few common paths.
                existing_vectors = (
                    getattr(desc, "vectors", None)
                    or getattr(getattr(getattr(desc, "config", None), "params", None), "vectors", None)
                )

                existing_dim: int | None = None
                if isinstance(existing_vectors, dict):
                    if "size" in existing_vectors:
                        existing_dim = int(existing_vectors["size"])
                    elif "default" in existing_vectors:
                        dv = existing_vectors.get("default")
                        if isinstance(dv, dict) and "size" in dv:
                            existing_dim = int(dv["size"])
                        else:
                            existing_dim = getattr(dv, "size", None)
                            existing_dim = int(existing_dim) if existing_dim is not None else None
                    elif len(existing_vectors) == 1:
                        only = next(iter(existing_vectors.values()))
                        if isinstance(only, dict) and "size" in only:
                            existing_dim = int(only["size"])
                        else:
                            existing_dim = getattr(only, "size", None)
                            existing_dim = int(existing_dim) if existing_dim is not None else None
                else:
                    size = getattr(existing_vectors, "size", None)
                    if size is None:
                        size = getattr(getattr(existing_vectors, "default", None), "size", None)
                    existing_dim = int(size) if size is not None else None

                if existing_dim is not None and int(existing_dim) != int(embed_dim):
                    logger.warning(
                        "Qdrant collection '%s' dim mismatch: existing=%s settings=%s",
                        s.qdrant_collection,
                        existing_dim,
                        embed_dim,
                    )
                    if s.qdrant_recreate_collection_on_dim_mismatch:
                        await client.delete_collection(s.qdrant_collection)
                        await client.create_collection(
                            collection_name=s.qdrant_collection,
                            vectors_config=VectorParams(
                                size=embed_dim,
                                distance=Distance.COSINE,
                            ),
                        )
                        logger.info(
                            "Recreated Qdrant collection '%s' for embed_dim=%s",
                            s.qdrant_collection,
                            embed_dim,
                        )
                    else:
                        logger.warning(
                            "Set QDRANT_RECREATE_COLLECTION_ON_DIM_MISMATCH=true to auto-recreate."
                        )
            except Exception:
                # Keep original behavior: if we can't read the collection description,
                # just continue (operations will surface the real error).
                pass
            logger.info("Qdrant collection '%s' already exists", s.qdrant_collection)
    except Exception as exc:
        logger.warning("Could not init Qdrant collection: %s", exc)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def embeddable_text(title: str, markdown: str) -> str:
    """What should actually be embedded for a page.

    Frontmatter was being embedded along with the body, and every page opens
    with the same handful of keys — so the strongest signal in the first chunk
    was structure that all pages share, and unrelated queries still scored
    around 0.5 against everything. The title goes in front instead: it is short,
    it is what people type, and it was previously only searchable by accident.
    """
    body = strip_frontmatter(markdown).strip()
    heading = (title or "").strip()
    return f"{heading}\n\n{body}".strip() if heading else body


async def upsert_page(
    slug: str,
    title: str,
    content: str,
    wiki_id: str = "default",
    settings: Settings | None = None,
    projection: bool = False,
) -> int:
    """Chunk *content*, embed all chunks, upsert to Qdrant. Returns chunk count."""
    s = settings or get_settings()
    client = await get_client(s)
    embed_dim = await resolve_embed_dim(s)

    logger.info(
        "Qdrant upsert_page start",
        extra={"slug": slug, "wiki_id": wiki_id, "collection": s.qdrant_collection, "content_chars": len(content or "")},
    )
    # Delete stale vectors first
    await delete_page(slug, wiki_id, s)

    chunks = _chunk_text(embeddable_text(title, content))
    # Stored once, from the markdown while it still has line breaks. The chunker
    # collapses whitespace, so an excerpt recovered from a chunk cannot tell a
    # heading from a sentence.
    excerpt = lede(content)
    vectors = await embed_texts(chunks, s)
    logger.debug("Qdrant vectors ready", extra={"slug": slug, "chunks": len(chunks), "dim": len(vectors[0]) if vectors else 0})
    if vectors and len(vectors[0]) != int(embed_dim):
        raise RuntimeError(
            f"Embedding dim mismatch: got={len(vectors[0])} settings.embed_dim={embed_dim} "
            f"(provider={s.embed_provider} model={s.embed_model})"
        )

    points = [
        PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{wiki_id}/{slug}/{i}")),
            vector=vec,
            payload={
                "slug": slug,
                "title": title,
                "chunk_index": i,
                "wiki_id": wiki_id,
                "text": chunk,
                "excerpt": excerpt,
                "knowledge_projection": projection,
            },
        )
        for i, (chunk, vec) in enumerate(zip(chunks, vectors))
    ]

    await client.upsert(collection_name=s.qdrant_collection, points=points)
    logger.info("Qdrant upsert_page done", extra={"slug": slug, "wiki_id": wiki_id, "points": len(points)})
    return len(points)


async def index_page(
    slug: str,
    title: str,
    markdown: str,
    wiki_id: str = "default",
) -> int:
    """Index one natural-language knowledge object using the page chunk schema."""
    return await upsert_page(slug, title, markdown, wiki_id, projection=True)


async def clear_projection_index(
    wiki_id: str = "default", settings: Settings | None = None
) -> None:
    """Delete canonical-knowledge projection vectors for one wiki."""
    s = settings or get_settings()
    client = await get_client(s)
    from qdrant_client.models import FilterSelector

    await client.delete(
        collection_name=s.qdrant_collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(key="wiki_id", match=MatchValue(value=wiki_id)),
                    FieldCondition(key="knowledge_projection", match=MatchValue(value=True)),
                ]
            )
        ),
    )
    logger.debug("Deleted canonical projection vectors", extra={"wiki_id": wiki_id})


async def search(
    query: str,
    wiki_id: str = "default",
    limit: int = 5,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Semantic search. Returns list of {slug, title, excerpt, score}."""
    s = settings or get_settings()
    client = await get_client(s)
    await resolve_embed_dim(s)  # ensures embed settings are usable and cached

    vectors = await embed_texts([query], s)
    query_vec = vectors[0]

    results = await _semantic_query_points(
        client,
        collection_name=s.qdrant_collection,
        query_vector=query_vec,
        query_filter=Filter(
            must=[FieldCondition(key="wiki_id", match=MatchValue(value=wiki_id))]
        ),
        limit=limit,
        with_payload=True,
    )

    seen: dict[str, dict[str, Any]] = {}
    for hit in results:
        payload = hit.payload or {}
        slug = payload.get("slug", "")
        if slug not in seen or hit.score > seen[slug]["score"]:
            seen[slug] = {
                "slug": slug,
                "title": payload.get("title", ""),
                "excerpt": payload.get("excerpt") or payload.get("text", "")[:300],
                "score": hit.score,
                "chunk_index": payload.get("chunk_index", 0),
            }

    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


async def search_raw(
    query: str,
    wiki_id: str = "default",
    limit: int = 5,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Like search() but returns all individual chunk hits (for context building)."""
    s = settings or get_settings()
    client = await get_client(s)
    await resolve_embed_dim(s)  # ensures embed settings are usable and cached

    vectors = await embed_texts([query], s)
    results = await _semantic_query_points(
        client,
        collection_name=s.qdrant_collection,
        query_vector=vectors[0],
        query_filter=Filter(
            must=[FieldCondition(key="wiki_id", match=MatchValue(value=wiki_id))]
        ),
        limit=limit * 3,  # fetch more to allow deduplication upstream
        with_payload=True,
    )

    return [
        {
            "slug": (r.payload or {}).get("slug", ""),
            "title": (r.payload or {}).get("title", ""),
            "excerpt": (r.payload or {}).get("excerpt")
            or (r.payload or {}).get("text", ""),
            "score": r.score,
            "chunk_index": (r.payload or {}).get("chunk_index", 0),
        }
        for r in results
    ]


async def delete_page(
    slug: str,
    wiki_id: str = "default",
    settings: Settings | None = None,
) -> None:
    """Delete all vectors for the given slug + wiki_id."""
    s = settings or get_settings()
    client = await get_client(s)
    try:
        from qdrant_client.models import FilterSelector
        await client.delete(
            collection_name=s.qdrant_collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(key="slug", match=MatchValue(value=slug)),
                        FieldCondition(key="wiki_id", match=MatchValue(value=wiki_id)),
                    ]
                )
            ),
        )
        logger.debug("Deleted Qdrant vectors", extra={"slug": slug, "wiki_id": wiki_id})
    except Exception as exc:
        logger.warning("Could not delete vectors for %s/%s: %s", wiki_id, slug, exc)
