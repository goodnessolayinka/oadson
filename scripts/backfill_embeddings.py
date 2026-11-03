"""
scripts/backfill_embeddings.py — one-time backfill for memory embeddings.

Finds all memory docs that don't have an `embedding` field yet and embeds
them using gemini-embedding-001. Run this once after deploying the
embedding search upgrade.

Usage:
    python scripts/backfill_embeddings.py

Requires MONGO_URI and GEMINI_API_KEY in environment (or .env file).
Safe to run multiple times — skips docs that already have embeddings.
Rate-limited to 5 docs/sec to stay within Gemini free tier.
"""

import asyncio
import os
import sys
import logging
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backfill")

EMBED_MODEL = "gemini-embedding-001"
EMBED_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{EMBED_MODEL}:batchEmbedContents"
)
EMBED_DIMS = 768
BATCH_SIZE = 10
RATE_LIMIT_DELAY = 2


async def embed_batch(api_key: str, texts: list[str]) -> list[list[float] | None]:
    requests_body = [
        {
            "model": f"models/{EMBED_MODEL}",
            "content": {"parts": [{"text": t[:8000]}]},
            "outputDimensionality": EMBED_DIMS,
        }
        for t in texts
    ]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                EMBED_URL,
                params={"key": api_key},
                json={"requests": requests_body},
            )
        if res.status_code != 200:
            logger.warning(f"Embed API error {res.status_code}: {res.text[:200]}")
            return [None] * len(texts)
        data = res.json()
        result = []
        for e in data.get("embeddings", []):
            values = e.get("values")
            result.append(values if values and len(values) == EMBED_DIMS else None)
        while len(result) < len(texts):
            result.append(None)
        return result
    except Exception as exc:
        logger.warning(f"Embed batch failed: {exc}")
        return [None] * len(texts)


async def main():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    mongo_uri = os.getenv("MONGO_URI", "").strip()

    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        sys.exit(1)
    if not mongo_uri:
        logger.error("MONGO_URI not set")
        sys.exit(1)

    # Extract the direct shard host from the SRV URI.
    # Termux can only reach one shard — connect directly to it instead of
    # going through the replica set topology (which needs all shards for
    # primary election). Writes still work via the one reachable shard.
    direct_host = "ac-ko5yn8o-shard-00-00.fkay7an.mongodb.net:27017"
    # Parse credentials from the SRV URI
    import re
    creds_match = re.search(r"mongodb\+srv://([^:]+):([^@]+)@", mongo_uri)
    if not creds_match:
        logger.error("Could not parse credentials from MONGO_URI")
        sys.exit(1)
    username, password = creds_match.group(1), creds_match.group(2)

    # Parse database name from URI if present
    db_match = re.search(r"\.net/([^?]+)", mongo_uri)
    db_name = db_match.group(1) if db_match else "oadson"

    direct_uri = (
        f"mongodb://{username}:{password}@{direct_host}/{db_name}"
        f"?authSource=admin&tls=true&tlsAllowInvalidCertificates=false"
        f"&directConnection=true&readPreference=secondaryPreferred"
    )

    from pymongo import ReadPreference
    client = AsyncIOMotorClient(
        direct_uri,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000,
        socketTimeoutMS=20000,
    )
    db = client[db_name]
    collection = db.get_collection("memory", read_preference=ReadPreference.SECONDARY_PREFERRED)

    # Count docs without embeddings
    total = await collection.count_documents({"embedding": {"$exists": False}})
    logger.info(f"Found {total} docs without embeddings")

    if total == 0:
        logger.info("Nothing to backfill — all docs already have embeddings.")
        return

    processed = 0
    failed = 0
    batch = []
    batch_docs = []

    cursor = collection.find(
        {"embedding": {"$exists": False}},
        {"_id": 1, "key": 1, "content": 1}
    )

    async for doc in cursor:
        key = doc.get("key", "")
        content = str(doc.get("content", ""))
        embed_text = f"{key}: {content}"
        batch.append(embed_text)
        batch_docs.append(doc)

        if len(batch) >= BATCH_SIZE:
            vectors = await embed_batch(api_key, batch)
            for doc_, vec in zip(batch_docs, vectors):
                if vec:
                    await collection.update_one(
                        {"_id": doc_["_id"]},
                        {"$set": {"embedding": vec, "embedding_model": EMBED_MODEL}},
                    )
                    processed += 1
                else:
                    failed += 1
                    logger.warning(f"Failed to embed key='{doc_.get('key')}'")

            logger.info(f"Progress: {processed}/{total} embedded, {failed} failed")
            batch = []
            batch_docs = []
            await asyncio.sleep(RATE_LIMIT_DELAY)

    # Final partial batch
    if batch:
        vectors = await embed_batch(api_key, batch)
        for doc_, vec in zip(batch_docs, vectors):
            if vec:
                await collection.update_one(
                    {"_id": doc_["_id"]},
                    {"$set": {"embedding": vec, "embedding_model": EMBED_MODEL}},
                )
                processed += 1
            else:
                failed += 1

    logger.info(f"Done. {processed} embedded, {failed} failed out of {total} total.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
