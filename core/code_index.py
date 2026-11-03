"""
core/code_index.py — Phase 10, Group 3: Codebase Indexing + Semantic Search.

Chunks OADSON's own source files, embeds each chunk via Gemini's real
embedding model (gemini-embedding-001 — an embedContent-only model, not
usable through generateContent, which is why it never showed up as a
"thinking" or "decision" model candidate), and stores vectors in Mongo.
Search is brute-force cosine similarity in Python — no Atlas Vector
Search dependency, since this is one person's codebase (a few hundred
files at most), not a scale problem.

Quota-conscious by design, not as an afterthought:
  - batchEmbedContents (one call per ~20 chunks) instead of one call per
    chunk — a 500-chunk project is ~25 calls to embed, not 500.
  - File-level content hashing — re-indexing only re-embeds files that
    actually changed since last time, not the whole project every run.
  - Hard cap on chunks per run (MAX_CHUNKS_PER_RUN) so a huge/unintended
    root path can't silently burn your whole day's quota in one call.
  - outputDimensionality=768 (vs. the model's native 3072) — smaller
    vectors, same search quality for this use case, less Mongo storage.
"""

import os
import hashlib
import logging
from datetime import datetime

import httpx

logger = logging.getLogger("oadson.code_index")

EMBED_MODEL = "gemini-embedding-001"
EMBED_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:batchEmbedContents"
OUTPUT_DIMS = 768

CHUNK_SIZE_LINES = 60
CHUNK_OVERLAP_LINES = 10
BATCH_SIZE = 20  # chunks per batchEmbedContents call
MAX_CHUNKS_PER_RUN = 500  # safety cap — see module docstring

SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".html", ".css"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "dist", "build", ".next", "outputs"}


def _iter_source_files(root: str) -> list:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SOURCE_EXTENSIONS:
                files.append(os.path.join(dirpath, fn))
    return files


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


def _chunk_file(path: str, root: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return []

    if not lines:
        return []

    rel_path = os.path.relpath(path, root)
    chunks = []
    start = 0
    step = CHUNK_SIZE_LINES - CHUNK_OVERLAP_LINES
    while start < len(lines):
        end = min(start + CHUNK_SIZE_LINES, len(lines))
        text = "".join(lines[start:end]).strip()
        if text:
            chunks.append({
                "file_path": rel_path,
                "start_line": start + 1,
                "end_line": end,
                "text": text,
            })
        if end >= len(lines):
            break
        start += step
    return chunks


async def _embed_batch(api_key: str, texts: list) -> list:
    """Returns a list of embedding vectors (list[float]), same order as texts. None entries on failure."""
    requests_body = [
        {
            "model": f"models/{EMBED_MODEL}",
            "content": {"parts": [{"text": t[:8000]}]},  # keep individual chunks well under any input limit
            "outputDimensionality": OUTPUT_DIMS,
        }
        for t in texts
    ]
    try:
        # SECURITY FIX 2026-08-14: params={"key": api_key} still renders
        # as ?key=... on the wire — moved to x-goog-api-key header,
        # which was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(EMBED_URL, headers={"x-goog-api-key": api_key}, json={"requests": requests_body})
        if res.status_code != 200:
            logger.warning(f"Embedding batch failed: {res.status_code} {res.text[:200]}")
            return [None] * len(texts)
        data = res.json()
        embeddings = data.get("embeddings", [])
        return [e.get("values") for e in embeddings] if embeddings else [None] * len(texts)
    except Exception as e:
        logger.warning(f"Embedding batch request failed: {e}")
        return [None] * len(texts)


def _cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def index_project(db, user_id: str, project: str, root: str, api_key: str) -> dict:
    """
    Indexes (or re-indexes changed files in) a project directory.
    Returns a summary dict — never raises; errors come back as
    {"status": "error", "reason": ...} for the calling skill to surface.
    """
    if not os.path.isdir(root):
        return {"status": "error", "reason": f"'{root}' is not a directory OADSON can read."}
    if not api_key:
        return {"status": "error", "reason": "GEMINI_API_KEY not configured — can't generate embeddings."}

    collection = db["code_index"]
    files = _iter_source_files(root)

    files_indexed = 0
    files_skipped_unchanged = 0
    chunks_embedded = 0
    chunks_failed = 0
    pending_chunks = []  # list of (chunk_dict,) waiting to be embedded in a batch

    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue

        content_hash = _file_hash(content)
        rel_path = os.path.relpath(path, root)

        existing = await collection.find_one(
            {"user_id": user_id, "project": project, "file_path": rel_path, "type": "file_meta"},
        )
        if existing and existing.get("content_hash") == content_hash:
            files_skipped_unchanged += 1
            continue

        # File changed (or new) — drop its old chunks, we'll re-add fresh ones.
        await collection.delete_many({"user_id": user_id, "project": project, "file_path": rel_path, "type": "chunk"})

        chunks = _chunk_file(path, root)
        for c in chunks:
            pending_chunks.append(c)

        await collection.update_one(
            {"user_id": user_id, "project": project, "file_path": rel_path, "type": "file_meta"},
            {"$set": {"content_hash": content_hash, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        files_indexed += 1

        if len(pending_chunks) >= MAX_CHUNKS_PER_RUN:
            break

    if len(pending_chunks) > MAX_CHUNKS_PER_RUN:
        pending_chunks = pending_chunks[:MAX_CHUNKS_PER_RUN]

    # Embed in batches to keep API call count low.
    for i in range(0, len(pending_chunks), BATCH_SIZE):
        batch = pending_chunks[i:i + BATCH_SIZE]
        vectors = await _embed_batch(api_key, [c["text"] for c in batch])

        for chunk, vector in zip(batch, vectors):
            if vector is None:
                chunks_failed += 1
                continue
            await collection.update_one(
                {
                    "user_id": user_id, "project": project, "type": "chunk",
                    "file_path": chunk["file_path"], "start_line": chunk["start_line"],
                },
                {"$set": {
                    "end_line": chunk["end_line"],
                    "text": chunk["text"],
                    "embedding": vector,
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
            chunks_embedded += 1

    return {
        "status": "success",
        "files_indexed": files_indexed,
        "files_skipped_unchanged": files_skipped_unchanged,
        "chunks_embedded": chunks_embedded,
        "chunks_failed": chunks_failed,
        "total_files_seen": len(files),
        "note": (
            f"Hit MAX_CHUNKS_PER_RUN ({MAX_CHUNKS_PER_RUN}) — run again to pick up "
            "the rest, or index a subdirectory." if len(pending_chunks) >= MAX_CHUNKS_PER_RUN else None
        ),
    }


async def search_code(db, user_id: str, project: str, query: str, api_key: str, limit: int = 5) -> dict:
    if not api_key:
        return {"status": "error", "reason": "GEMINI_API_KEY not configured."}

    vectors = await _embed_batch(api_key, [query])
    query_vector = vectors[0] if vectors else None
    if query_vector is None:
        return {"status": "error", "reason": "Could not embed the search query."}

    collection = db["code_index"]
    cursor = collection.find(
        {"user_id": user_id, "project": project, "type": "chunk"},
        {"file_path": 1, "start_line": 1, "end_line": 1, "text": 1, "embedding": 1},
    )

    scored = []
    async for doc in cursor:
        score = _cosine_similarity(query_vector, doc.get("embedding") or [])
        scored.append((score, doc))

    if not scored:
        return {
            "status": "info",
            "message": f"No code indexed yet for project '{project}'. Run the 'index' action first.",
            "results": [],
        }

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    return {
        "status": "success",
        "message": f"Top {len(top)} match(es) for '{query}' in '{project}'.",
        "results": [
            {
                "file_path": doc["file_path"],
                "lines": f"{doc['start_line']}-{doc['end_line']}",
                "score": round(score, 3),
                "preview": doc["text"][:300],
            }
            for score, doc in top
        ],
    }


async def index_status(db, user_id: str, project: str) -> dict:
    collection = db["code_index"]
    file_count = await collection.count_documents({"user_id": user_id, "project": project, "type": "file_meta"})
    chunk_count = await collection.count_documents({"user_id": user_id, "project": project, "type": "chunk"})
    return {
        "status": "success",
        "project": project,
        "files_indexed": file_count,
        "chunks_indexed": chunk_count,
    }
