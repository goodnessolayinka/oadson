"""
core/uploader.py — OADSON V3 Cloudinary upload helper.

Replaces routers.upload_router.upload_to_cloudinary from V2.
Skills import from here: from core.uploader import upload_to_cloudinary
"""

import os
import cloudinary
import cloudinary.uploader
import logging

logger = logging.getLogger("oadson.uploader")


def is_cloudinary_configured() -> bool:
    """
    REINTRODUCED 2026-09-03 — Cloudinary was removed 2026-08-30 because
    the old USE_CLOUDINARY flag defaulted false and the branch never ran
    in production; this module itself was left in place (dormant) rather
    than deleted. Now that uploads are switching back to Cloudinary, this
    is the single place that decides "is Cloudinary actually usable right
    now" — callers (core/inline_storage.py's save_upload) check this
    before calling upload_to_cloudinary so a half-configured deployment
    (e.g. CLOUDINARY_NAME set but the secret forgotten) fails over to
    inline storage instead of raising mid-request.
    """
    return bool(
        os.getenv("CLOUDINARY_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )


def _init():
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )


async def upload_to_cloudinary(
    data: bytes,
    public_id: str,
    resource_type: str = "auto",
) -> dict:
    """
    Upload bytes to Cloudinary.
    Returns {"url": str, "public_id": str} on success.
    Raises RuntimeError on failure.
    """
    _init()
    try:
        result = cloudinary.uploader.upload(
            data,
            public_id=public_id,
            resource_type=resource_type,
            overwrite=True,
        )
        return {
            "url": result["secure_url"],
            "public_id": result["public_id"],
        }
    except Exception as e:
        logger.error(f"Cloudinary upload failed: {e}")
        raise RuntimeError(f"Upload failed: {e}")
