"""
api/models.py — OADSON V3 Models Router.

ADDED 2026-09-09. Read-only listing of every model the registry knows
about, for the web/app/pwa model-picker UI (see api/chat.py's
ChatRequest.model and api/chat_stream.py's ChatStreamRequest.model —
this endpoint is what populates the list a client picks a value from).

Deliberately mirrors api/skills.py's pattern: a thin, auth-gated GET
that reads straight off an in-memory registry, no DB round-trip.

SCOPE, deliberate: this returns the STATIC registry list — whatever
provider_registry.py currently declares — not a live per-model quota
check against quota_tracker. A model that's actually out of quota or
briefly unreachable still appears here; picking it just falls through
to normal automatic routing exactly as if nothing had been selected
(see core/provider_router.py's decide_preferred docstring for that
fail-open behavior). Keeping this static is a deliberate simplicity
choice, not an oversight — a live-checked list would need its own
caching/staleness story that isn't worth building until this proves
useful in practice. Telegram/WhatsApp/CLI never call this endpoint and
never need to — automatic routing there is completely unaffected by
anything in this file.
"""

import logging
from fastapi import APIRouter, Depends

from dependency import get_current_user
from core.provider_registry import all_models_flat

logger = logging.getLogger("oadson.api.models")

router = APIRouter()


@router.get("/models")
async def list_models(user: dict = Depends(get_current_user)):
    """
    Every model currently in the registry, flattened across providers.

    Each entry: {id, provider, tier, rpm, rpd, supports_tools,
    supports_vision, ...any other per-model fields the registry carries}
    — whatever all_models_flat() already returns, unchanged. No
    filtering here: local_qwen_cli's terminal-only gate and any
    surface-specific restriction live at the actual call site
    (core/provider_router.py's decide_preferred / decide_fallback), not
    in this listing — a web client CAN see local_qwen_cli in this list,
    same as it can see every other model, but selecting it will simply
    fail over to normal routing the same way selecting an over-quota
    model would, since decide_preferred enforces that gate itself.
    """
    models = all_models_flat()
    return {"models": models, "count": len(models)}
