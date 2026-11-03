"""
core/model_tester.py — OADSON V3 Model Connectivity Tester.

Added 2026-08-08. Purpose: a deliberate, on-demand way to exercise ANY
number of configured models directly, instead of only finding out a
model works (or a model ID is dead — see provider_registry.py's
removal note for gemini-2.0-pro-exp and friends) when real quota
exhaustion happens to force the fallback chain that far down the list.

A light usage pattern means the primary model rarely runs dry, so
everything below it in provider_registry.py can sit completely
unexercised for weeks — which is exactly how 3 retired experimental
Gemini IDs sat unnoticed until a genuinely busy day hit them. This is
the tool for finding that out on purpose, on your own schedule,
instead of live in a chat.

Each model gets one minimal, cheap call: a fixed trivial prompt, no
tools, and (deliberately) does NOT go through quota_tracker's
proactive skip — the whole point is to test the model itself, not to
respect whether it looks "busy" from prior usage.
"""

import asyncio
import logging
import os
import time

from core.provider_registry import PROVIDERS, all_models_flat
from core.provider_router import _call_gemini, _call_openai_compatible

logger = logging.getLogger("oadson.model_tester")

TEST_PROMPT = "Reply with exactly one word: OK"

# The ONE model the engineer test runs when no selection is given. Set to
# DeepSeek V4.1 Flash on CoreWeave (the model the owner picked, 2026-11-01).
# Override with ENGINEER_TEST_MODEL, using the 'provider/id' form, e.g.
# ENGINEER_TEST_MODEL=coreweave/zai-org/GLM-5.3-Flash
DEFAULT_TEST_MODEL = os.getenv("ENGINEER_TEST_MODEL", "coreweave/deepseek-ai/DeepSeek-V4.1-Flash")


async def _test_one(provider: dict, model: dict) -> dict:
    model_id = model["id"]
    label = f"{provider['name']}/{model_id}" if provider["name"] != "gemini" else model_id
    start = time.monotonic()
    try:
        if provider["protocol"] == "gemini":
            result = await _call_gemini(
                model_id, "You are a test harness.",
                [{"role": "user", "parts": [{"text": TEST_PROMPT}]}], None,
            )
        else:
            result = await _call_openai_compatible(
                provider, model_id,
                [{"role": "user", "content": TEST_PROMPT}], None,
            )
        elapsed_ms = round((time.monotonic() - start) * 1000)
        text = (result.get("text") or "").strip()
        return {
            "model": label,
            "status": "pass",
            "latency_ms": elapsed_ms,
            "response_snippet": text[:80],
        }
    except Exception as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return {
            "model": label,
            "status": "fail",
            "latency_ms": elapsed_ms,
            "error": str(e)[:300],
        }


async def test_models(selection: list | None = None, concurrency: int = 4) -> dict:
    """
    selection: list of model IDs to test (matches against 'id', or
    'provider/id' for non-Gemini). None = test ONLY DEFAULT_TEST_MODEL,
    the one model the owner selected. Nothing else runs unless it is named.
    concurrency caps how many run in parallel — kept modest by default
    since this is deliberately hitting real provider APIs and burning
    real quota, not something to blast at max parallelism.
    """
    if not selection:
        selection = [DEFAULT_TEST_MODEL]

    targets = []
    for provider in PROVIDERS:
        for model in provider["models"]:
            label = model["id"] if provider["name"] == "gemini" else f"{provider['name']}/{model['id']}"
            named = bool(selection) and (model["id"] in selection or label in selection)
            # PAID MODELS ARE OPT-IN. A bare /engineer/test-models run used to
            # call every registry model, including Claude and DeepSeek, spending
            # real credits. Paid models now run only when named explicitly.
            if provider.get("paid") and not named:
                continue
            if selection and not named:
                continue
            targets.append((provider, model))

    if not targets:
        return {"status": "error", "reason": "No models matched the given selection.", "results": []}

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(provider, model):
        async with semaphore:
            return await _test_one(provider, model)

    results = await asyncio.gather(*[_bounded(p, m) for p, m in targets])
    passed = sum(1 for r in results if r["status"] == "pass")

    return {
        "status": "pass" if passed == len(results) else ("partial" if passed > 0 else "fail"),
        "tested": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": sorted(results, key=lambda r: (r["status"] != "fail", r["model"])),
    }
