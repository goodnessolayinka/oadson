"""
core/provider_router.py — OADSON V3 Provider Router.

The actual fix for "Gemini quota exhausted mid-conversation = bot gives
one crippled apology and abandons every tool/vision capability for the
rest of the turn" (see core/orchestrator.py's old quota-fallback block,
which called ProviderManager.generate() — plain text only, no tools).

This router builds an ordered candidate chain from
core/provider_registry.py, skips anything core/quota_tracker.py says is
out of headroom, and tries each candidate through the right protocol
adapter (Gemini's native contents/functionCall shape, or the
OpenAI-compatible tools/tool_calls shape most other free-tier providers
speak) until one succeeds. Callers get back a NORMALIZED result
regardless of which provider actually answered:

    {
        "tool_call": {"name": str, "args": dict} | None,
        "text": str | None,
        "model_used": "provider/model",
        "protocol": "gemini" | "openai",
        "openai_messages": list | None,  # populated only when protocol
                                          # switched away from Gemini —
                                          # see translate_gemini_to_openai()
    }

Adding a new provider or model: add ONE entry to
core/provider_registry.py. Nothing here needs to change unless the new
provider speaks a genuinely different protocol than "gemini" or "openai"
(rare — most inference providers are OpenAI-compatible by convention).
"""

import os
import json
import time
import logging
import httpx
from typing import Optional

from core.provider_registry import PROVIDERS, get_provider
from core import quota_tracker
from core import spend_tracker, model_policy
from core import anthropic_native
from core.local_tool_schemas import LOCAL_TOOL_DECLARATIONS

# The model terminal turns try first (see decide_terminal_first). Gemini
# stays first on every other surface. Model id, not provider/id.
TERMINAL_FIRST_MODELS = [
    "openai/gpt-oss-20b",                    # first choice
    "deepseek-ai/DeepSeek-V4.1-Flash",       # if GPT OSS fails
    "meta-llama/Llama-3.1-8B-Instruct",      # if DeepSeek also fails
]

logger = logging.getLogger("oadson.provider_router")

# ADDED 2026-09-19 — a model that answers with anything other than a clean
# STOP has NOT finished its answer. Google documents these finishReason
# values; MAX_TOKENS is a hard truncation, the rest are the model refusing
# or being cut off. Previously NOTHING on the response path looked at this,
# so a reply cut off mid-sentence was persisted and shown as complete.
_INCOMPLETE_FINISH_REASONS = {
    "MAX_TOKENS", "SAFETY", "RECITATION", "BLOCKLIST",
    "PROHIBITED_CONTENT", "SPII", "LANGUAGE", "OTHER",
}

# Raised output cap for OpenAI-compatible fallbacks (Groq/NVIDIA/OpenRouter).
# Was a hardcoded 2048, which cuts a long formatted answer off mid-sentence.
OPENAI_COMPAT_MAX_TOKENS = 8192


def _max_tokens_for(provider: dict) -> int:
    """Per-provider reply cap. A provider can set its own "max_tokens" in the registry.
    The global default stays 8192 so Groq's 8000-TPM cap is not affected."""
    return int(provider.get("max_tokens", OPENAI_COMPAT_MAX_TOKENS))


class TruncatedResponse(Exception):
    """A model stopped before finishing its answer (MAX_TOKENS etc.)."""


def _is_daily_quota(text: str) -> bool:
    """Google's per-DAY exhaustion won't clear in a minute — use the long cooldown."""
    t = (text or "").lower()
    return "perday" in t.replace("_", "").replace(" ", "") or "per day" in t or "daily" in t


# ── SCHEMA TRANSLATION (Gemini <-> OpenAI tool-calling shapes) ──

def gemini_declarations_to_openai_tools(function_declarations: list) -> list:
    """
    Gemini's functionDeclarations and OpenAI's tools are both
    JSON-schema-based and structurally very close — this is a
    straightforward wrapping, not a lossy conversion.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for d in (function_declarations or [])
    ]


def translate_gemini_to_openai(system: str, contents: list, keep_images: bool = False) -> list:
    """
    Converts a Gemini-shape conversation (contents: [{role, parts}])
    into OpenAI-shape messages ([{role, content}] with tool_calls/
    tool_call_id for function turns) — so a fallback provider can
    continue the SAME conversation state Gemini had built up, instead of
    restarting from scratch with just the original objective.

    Only called ONCE, at the moment the router falls back away from
    Gemini for a given turn — not on every iteration.

    keep_images: 2026-08-30 — the registry now has real vision-capable
    OpenAI-protocol models (meta/muse-glimmer-30b on NVIDIA NIM,
    minimax/minimax-m3:free on OpenRouter — see provider_registry.py),
    but this function runs ONCE, up front, before decide_fallback() has
    picked which model the conversation will actually land on. Stripping
    images unconditionally at translation time (the old behavior) meant
    even a genuinely vision-capable fallback model downstream could
    never see them — the bytes were already gone by the time the loop
    got there.

    Default is False (strip + note, old behavior preserved) since most
    callers don't yet know whether they'll land on a vision model.
    decide_fallback() below calls this once with keep_images=True,
    producing image-preserving content blocks for every user turn that
    had one, then STRIPS them back down to the old text-only form as
    the per-model default and only re-attaches them for a model whose
    registry entry says supports_vision=True — see the
    _strip_images_from_openai_messages() helper and its call site in
    decide_fallback(). That keeps this function's output usable for both
    vision and non-vision destinations from one translation pass,
    instead of needing to re-translate per model tried.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    # Maps a function name to a FIFO queue of pending tool_call_ids
    # awaiting their response. This replaces a "pop the single most
    # recently pushed bookkeeping marker" trick that only worked for
    # one call at a time. Gemini supports PARALLEL function calling —
    # multiple functionCall parts in one "model" turn — and the old
    # version used next(...) (singular) to grab only the FIRST one,
    # silently dropping any additional parallel calls from the
    # translated conversation entirely. orchestrator.py still executed
    # and appended a response for every one of them, though, so the
    # provider ended up seeing fewer tool_calls than tool responses —
    # exactly the "not the same number of function calls and responses"
    # 400 seen in production, and very plausibly the cause of the
    # "ends in assistant" role-order error too, since the mismatched
    # structure could easily end up truncated at an odd point.
    pending_ids_by_name = {}
    call_counter = 0

    for turn in contents:
        role = turn.get("role")
        parts = turn.get("parts", [])

        # FIX 2026-08-28: functionResponse parts are now correctly sent
        # with role "user" (see tool_loop.py's role-fix comments — Gemini's
        # REST API only accepts user/model as content roles, and every
        # official example sends functionResponse under "user"). That
        # means a plain user-text turn and a tool-result turn are now the
        # SAME role and must be told apart by inspecting parts, not by
        # role alone. The old `elif role == "function"` branch below was
        # dead code from before that fix — nothing ever produced that
        # role — which meant every tool result was silently swallowed by
        # the `role == "user"` branch instead (functionResponse has no
        # "text" key, so it just produced an empty-content user message
        # and the actual result vanished). That's the real root cause of
        # "not the same number of function calls and responses" and
        # "expected last role user or tool, got assistant" seen in
        # production — the translated history was missing every tool
        # response, so it no longer matched the tool_calls it also sent.
        fr_parts = [p for p in parts if "functionResponse" in p] if role == "user" else []
        if fr_parts:
            for p in fr_parts:
                fr_part = p["functionResponse"]
                name = fr_part.get("name")
                queue = pending_ids_by_name.get(name)
                if queue:
                    tool_id = queue.pop(0)
                else:
                    call_counter += 1
                    tool_id = f"call_{call_counter}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(fr_part.get("response", {}), default=str),
                })

        elif role == "user":
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            image_parts = [p["inline_data"] for p in parts if "inline_data" in p]

            if image_parts and keep_images:
                # Vision-capable OpenAI-protocol content-block form —
                # kept alongside a plain-text fallback copy so a
                # non-vision model downstream can still be handed
                # equivalent text (see _strip_images_from_openai_messages).
                content_blocks = []
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for img in image_parts:
                    mime = img.get("mime_type", "image/png")
                    data = img.get("data", "")
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    })
                messages.append({"role": "user", "content": content_blocks})
            else:
                # Real image bytes in a Gemini-format turn can't carry
                # over to a non-vision fallback provider. Note it in text
                # rather than silently dropping it so the model doesn't
                # contradict itself claiming to have seen something it
                # didn't.
                if image_parts:
                    text = f"{text}\n[An image was attached here, but the current fallback model can't see images.]"
                messages.append({"role": "user", "content": text})

        elif role == "model":
            fc_parts = [p for p in parts if "functionCall" in p]
            if fc_parts:
                tool_calls = []
                for p in fc_parts:
                    call_counter += 1
                    tool_id = f"call_{call_counter}"
                    fc = p["functionCall"]
                    name = fc.get("name")
                    pending_ids_by_name.setdefault(name, []).append(tool_id)
                    tool_calls.append({
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(fc.get("args", {}))},
                    })
                # TRACE MODE 2026-08-20: was content=None unconditionally —
                # same discard bug as _call_gemini above, just on the
                # history-replay side. A past turn that had BOTH
                # interstitial text and a tool call (now possible since
                # the _call_gemini fix) would silently lose that text the
                # moment the conversation got replayed into a fallback
                # provider's OpenAI-format history. OpenAI's tool-call
                # message shape supports content alongside tool_calls
                # (content: <text or null>), so this is a legal value,
                # not a hack.
                replay_text = "".join(p.get("text", "") for p in parts if "text" in p) or None
                messages.append({"role": "assistant", "content": replay_text, "tool_calls": tool_calls})
            else:
                text = "".join(p.get("text", "") for p in parts if "text" in p)
                messages.append({"role": "assistant", "content": text})

        elif role == "function":
            for p in parts:
                fr_part = p.get("functionResponse")
                if not fr_part:
                    continue
                name = fr_part.get("name")
                queue = pending_ids_by_name.get(name)
                if queue:
                    tool_id = queue.pop(0)
                else:
                    # No matching pending call recorded for this name —
                    # shouldn't normally happen, but a fresh id here beats
                    # crashing or silently mismatching an unrelated call.
                    call_counter += 1
                    tool_id = f"call_{call_counter}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(fr_part.get("response", {}), default=str),
                })

    return messages


# ── LOW-LEVEL CALLERS ──

async def _call_gemini(model_id: str, system: str, contents: list, tools: list) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}
    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    # SECURITY FIX 2026-08-14: was ?key={api_key} in the URL — httpx's
    # own request logger (and any proxy/log aggregator in between) logs
    # full request URLs, which meant the live API key was landing in
    # plaintext in Render's logs on every single call. Google's current
    # docs (ai.google.dev/api) list x-goog-api-key as the standard auth
    # method — ?key= still works but is legacy and log-unsafe. A header
    # never appears in a request-line log the way a URL does.
    try:
        # TUNED 2026-09-22 — was timeout=60, no documented reasoning for
        # that specific value. Confirmed in production: a decide_gemini
        # walk took walk_total=179.54s to land on a working model, with
        # only 27s of that explained by an explicit quota cooldown skip —
        # the remaining ~150s is only explainable by one or more earlier
        # models in the walk each hanging close to this 60s ceiling
        # before failing, none of which ever even reached the explicit
        # httpx.TimeoutException handler below (that only fires once
        # THIS timeout is actually exceeded).
        #
        # CAUTION, checked before picking a value: this same session's
        # own logs also show GENUINELY SUCCESSFUL calls taking 45.38s and
        # 45.69s under load (call=45.38s, ROUTE decide_gemini OK — not a
        # failure) — an aggressive cut to e.g. 25s would have killed
        # those and made things WORSE, not better, since a killed-then-
        # retried call costs more total time than just waiting the
        # extra seconds. 40s sits above that confirmed-legitimate 45s
        # ceiling's near-neighbor while still meaningfully undercutting
        # the original 60s — real headroom for a genuinely slow-but-
        # working response, while a call approaching the OLD 60s ceiling
        # (nowhere near the confirmed-good 45s range) was still very
        # likely hung, not just slow.
        async with httpx.AsyncClient(timeout=40) as client:
            res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
    except (httpx.TimeoutException, httpx.TransportError) as _net_e:
        # ADDED 2026-09-19 — a timeout used to propagate as a bare exception
        # that nothing remembered, so the NEXT request re-tried the same
        # hung model and waited the full 60s again. Treated as retryable
        # (QuotaExceeded) so decide_gemini/decide_fallback move on, and the
        # model is cooled down so later requests skip it immediately.
        quota_tracker.mark_exhausted("gemini", model_id, reason=f"network: {type(_net_e).__name__}")
        raise QuotaExceeded(f"gemini:{model_id} — network error {type(_net_e).__name__}: {_net_e}") from _net_e

    # Any retryable server-side failure should fall through to the
    # provider fallback chain, not just quota-specific errors:
    #   429                → real rate limit
    #   500/502/503/504    → capacity spikes, overload, upstream errors —
    #                        these do NOT contain "RESOURCE_EXHAUSTED" in
    #                        the response text (that string is specific to
    #                        Google's quota-exceeded errors), so the old
    #                        check silently let plain overload 503s
    #                        through as a hard failure with no fallback.
    if res.status_code == 429 or res.status_code in (500, 502, 503, 504):
        quota_tracker.mark_exhausted(
            "gemini", model_id, reason=f"{res.status_code}: {res.text[:120]}",
            daily=(res.status_code == 429 and _is_daily_quota(res.text)),
        )
        raise QuotaExceeded(f"gemini:{model_id} — {res.status_code}: {res.text[:300]}")
    if res.status_code != 200:
        raise RuntimeError(f"Gemini error {res.status_code}: {res.text[:300]}")

    data = res.json()
    candidates = data.get("candidates", [])
    if not candidates or not candidates[0].get("content", {}).get("parts"):
        reason = candidates[0].get("finishReason", "UNKNOWN") if candidates else "NO_CANDIDATES"
        raise RuntimeError(f"Gemini returned no content (reason: {reason})")

    # ADDED 2026-09-19 — log WHY the model stopped, on every call. Before
    # this there was no way to tell from the logs whether a cut-off reply
    # was MAX_TOKENS, a safety stop, or a dropped connection.
    _finish = candidates[0].get("finishReason", "UNKNOWN")
    logger.info(f"GEMINI finish model={model_id} finishReason={_finish}")

    parts = candidates[0]["content"]["parts"]
    fc_part = next((p for p in parts if "functionCall" in p), None)
    # TRACE MODE 2026-08-20: previously hardcoded "text": None whenever a
    # functionCall part was present, discarding any text Gemini generated
    # ALONGSIDE that tool call in the same response. Gemini's function-
    # calling responses can legitimately carry both a text part and a
    # functionCall part in one turn (the model saying something while
    # also deciding to act) — that text was silently thrown away here,
    # before the orchestrator ever got a chance to see it. This is the
    # root fix everything else in Trace mode depends on: capture it now,
    # let callers decide what to do with it. Never breaks anything for
    # callers that don't check "text" when tool_call is set — they
    # behaved exactly as before (checked tool_call first, ignored text).
    accompanying_text = "".join(p.get("text", "") for p in parts if "text" in p).strip() or None
    if fc_part:
        fc = fc_part["functionCall"]
        # DEBUG LOGGING 2026-08-20 — root of the Trace Mode pipeline.
        # Logs the RAW part types Gemini actually returned for this call,
        # so a "trace never shows up" report can be narrowed to "Gemini
        # simply never sends text alongside a functionCall for this
        # prompt/model" vs. a bug somewhere downstream (orchestrator
        # gating, event emission, frontend). part_types will show e.g.
        # ['functionCall'] with no 'text' entry if Gemini genuinely
        # didn't produce any — that's a prompting/model behavior
        # question, not a code bug, and this line is how to tell the
        # difference from the Render logs alone.
        logger.info(
            f"TRACE _call_gemini model={model_id}: fc={fc.get('name')} "
            f"part_types={[list(p.keys()) for p in parts]} "
            f"accompanying_text_len={len(accompanying_text) if accompanying_text else 0}"
        )
        return {
            "tool_call": {"name": fc.get("name"), "args": fc.get("args", {}) or {}},
            "text": accompanying_text,
            "raw_part": fc_part,
            # WORDING CONTINUITY 2026-08-20 — orchestrator.py's echo-back
            # step (contents.append after a tool call) previously only
            # ever echoed raw_part (the functionCall alone) into the
            # conversation history Gemini sees on its NEXT decision.
            # accompanying_text lived only in our own step-tracking
            # (ctx["steps"]), never in `contents` itself — so the model
            # had no memory of its own talk sentence when it went on to
            # write the real final answer after the tool result came
            # back. Two genuinely disconnected generations, is what that
            # produces: a talk fragment the user saw once, and a
            # self-contained final answer that re-explains from scratch
            # because the model literally never saw what it said a
            # moment earlier. raw_parts is the FULL original parts array
            # (text part + functionCall part, in Gemini's own order) so
            # the echo can include both — see its use in orchestrator.py.
            "raw_parts": parts,
        }
    text = "".join(p.get("text", "") for p in parts)
    return {"tool_call": None, "text": text, "raw_part": None, "raw_parts": parts}


async def _stream_gemini(model_id: str, system: str, contents: list) -> "AsyncGenerator[str, None]":
    """
    Token-by-token streaming for the final answer turn only.
    Uses streamGenerateContent — returns newline-delimited JSON chunks.
    Only called when there are no tools in the payload (final answer, not
    a tool-calling decision step) so we never stream a function call.

    Yields text chunks as they arrive. Raises QuotaExceeded on 429/5xx
    so the caller can fall back to non-streaming if needed.
    """
    import json as _json
    api_key = os.getenv("GEMINI_API_KEY", "")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:streamGenerateContent?alt=sse"
    )
    # SECURITY FIX 2026-08-14: same as _call_gemini above — key moved
    # from the URL query string to the x-goog-api-key header so it
    # never appears in httpx's request-line logs.
    # ADDED 2026-09-19 — track how the stream ENDED. Before this, the loop
    # below simply ran out of lines and returned, so a stream that stopped
    # early (MAX_TOKENS, a safety stop, or a connection dropped mid-answer)
    # was indistinguishable from one that finished cleanly: the partial
    # text was returned as the complete reply, persisted, and shown to the
    # user cut off mid-sentence with no error anywhere in the logs.
    finish_reason = None
    got_any_text = False
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=payload, headers={"x-goog-api-key": api_key}) as res:
                if res.status_code == 429 or res.status_code in (500, 502, 503, 504):
                    body = (await res.aread()).decode("utf-8", "replace")[:200]
                    quota_tracker.mark_exhausted(
                        "gemini", model_id, reason=f"stream {res.status_code}: {body[:120]}",
                        daily=(res.status_code == 429 and _is_daily_quota(body)),
                    )
                    raise QuotaExceeded(f"gemini:{model_id} (stream) {res.status_code}")
                if res.status_code != 200:
                    raise RuntimeError(f"Gemini stream error {res.status_code}")
                async for line in res.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = _json.loads(raw)
                    except Exception:
                        continue
                    candidates = chunk.get("candidates", [])
                    if not candidates:
                        continue
                    fr = candidates[0].get("finishReason")
                    if fr:
                        finish_reason = fr
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
                        text = part.get("text", "")
                        if text:
                            got_any_text = True
                            yield text
    except (httpx.TimeoutException, httpx.TransportError) as net_e:
        # Connection died mid-stream. If nothing was emitted yet this is a
        # plain failure the caller can retry; if text WAS emitted, the
        # caller must know the answer is incomplete (see TruncatedResponse).
        quota_tracker.mark_exhausted("gemini", model_id, reason=f"stream network: {type(net_e).__name__}")
        if got_any_text:
            raise TruncatedResponse(
                f"gemini:{model_id} stream dropped mid-answer ({type(net_e).__name__})"
            ) from net_e
        raise QuotaExceeded(f"gemini:{model_id} stream network error {type(net_e).__name__}") from net_e

    logger.info(f"GEMINI stream finish model={model_id} finishReason={finish_reason} emitted_text={got_any_text}")

    if finish_reason in _INCOMPLETE_FINISH_REASONS:
        raise TruncatedResponse(f"gemini:{model_id} stream ended with finishReason={finish_reason}")
    if got_any_text and finish_reason is None:
        # Stream closed without ever sending a finishReason: the server (or
        # a proxy) cut the connection. A clean stream always ends with one.
        raise TruncatedResponse(f"gemini:{model_id} stream closed without a finishReason")


async def _call_openai_compatible_once(provider: dict, model_id: str, messages: list, tools: list,
                                     api_key_override: str | None = None,
                                     spend_name: str | None = None) -> dict:
    # FIX 2026-09-06 — was unconditional: any provider with no
    # api_key_env set, or with the env var unset, raised here before ever
    # attempting the call. That's correct for every provider added so
    # far (all genuinely require a key), but the new local_qwen_cli entry
    # (see provider_registry.py) has NO auth at all — it's the user's own
    # llama-server, unauthenticated by default. An empty api_key_env
    # ("") is now read as "this provider deliberately has no auth" and
    # skips the key requirement instead of failing; a provider whose
    # api_key_env is a real name but whose env var isn't SET still fails
    # exactly as before — this only changes behavior for the explicit
    # opt-out case.
    api_key_env = provider.get("api_key_env", "")
    if api_key_override is not None:
        api_key = api_key_override
    else:
        api_key = os.getenv(api_key_env, "") if api_key_env else None
        if api_key_env and not api_key:
            raise RuntimeError(f"{api_key_env} not set")

    # SPEND GATE — paid providers are refused BEFORE the request if the cap
    # (lifetime or daily) would be breached. Raised as QuotaExceeded so the
    # walk simply moves on to the next (free) provider.
    # spend_name is the ledger this key draws from. Each key has its own account credit,
    # so each key gets its own cap (see _call_openai_compatible).
    spend_key = spend_name or provider["name"]
    if provider.get("paid") and not spend_tracker.can_spend(spend_key, est_usd=model_policy.PER_TURN_RESERVE_USD):
        raise QuotaExceeded(f"{spend_key}: spend cap reached, skipping paid model")

    # NATIVE CLAUDE — protocol "anthropic" goes to the Messages API with
    # prompt caching. Same return shape as the OpenAI path below, so every
    # caller (decide_fallback, decide_preferred, continue_openai) is unchanged.
    if provider.get("protocol") == "anthropic":
        try:
            result = await anthropic_native.call_claude(provider, model_id, messages, tools)
        except anthropic_native.ClaudeQuota as e:
            raise QuotaExceeded(f"{provider['name']}:{model_id} — {e}") from e
        if provider.get("paid"):
            try:
                await spend_tracker.record(provider["name"], model_id, result.get("usage") or {})
            except Exception as _se:
                logger.warning(f"spend record failed for {provider['name']}: {_se}")
        return result

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(provider.get("extra_headers", {}))

    # RAISED 2026-09-19: was a hardcoded 2048, which cut a long formatted
    # answer off mid-sentence on every fallback provider (Groq / NVIDIA /
    # OpenRouter) — and nothing noticed, see the finish_reason check below.
    if provider.get("trim_tool_results"):
        messages = model_policy.trim_tool_results(messages)
    body = {"model": model_id, "messages": messages, "max_tokens": _max_tokens_for(provider), "temperature": 0.7}
    if tools and provider.get("supports_tools"):
        body["tools"] = tools

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(provider["base_url"], headers=headers, json=body)
    except (httpx.TimeoutException, httpx.TransportError) as _net_e:
        quota_tracker.mark_exhausted(provider["name"], model_id, reason=f"network: {type(_net_e).__name__}")
        raise QuotaExceeded(f"{provider['name']}:{model_id} — network error {type(_net_e).__name__}") from _net_e

    if res.status_code == 429 or res.status_code in (500, 502, 503, 504):
        quota_tracker.mark_exhausted(
            provider["name"], model_id, reason=f"{res.status_code}: {res.text[:120]}",
            daily=(res.status_code == 429 and _is_daily_quota(res.text)),
        )
        raise QuotaExceeded(f"{provider['name']}:{model_id} — {res.status_code}: {res.text[:300]}")
    if res.status_code == 410:
        # See ProviderUnavailable's own docstring — 410 means the whole
        # endpoint is gone, not just this model.
        raise ProviderUnavailable(f"{provider['name']} error 410: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"{provider['name']} error {res.status_code}: {res.text[:300]}")

    data = res.json()
    if provider.get("paid"):
        try:
            await spend_tracker.record(spend_key, model_id, data.get("usage") or {})
        except Exception as _se:
            logger.warning(f"spend record failed for {provider['name']}: {_se}")
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected {provider['name']} response shape: {data}") from e

    # ADDED 2026-09-19 — was never read. finish_reason == "length" means the
    # model hit max_tokens and the reply is cut off mid-sentence.
    _fr = (data["choices"][0] or {}).get("finish_reason")
    logger.info(f"OPENAI-COMPAT finish provider={provider['name']} model={model_id} finish_reason={_fr}")

    tool_calls = message.get("tool_calls")
    if tool_calls:
        tc = tool_calls[0]["function"]
        try:
            args = json.loads(tc.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        # WORDING CONTINUITY 2026-08-20 — was hardcoded "text": None,
        # unconditionally discarding message.content whenever a tool_call
        # was present. OpenAI-compatible chat completions (this is the
        # fallback provider path — Groq/etc, used once Gemini's quota is
        # hit mid-turn) can legitimately return BOTH message.content and
        # message.tool_calls in one response, same as Gemini can return a
        # text part alongside a functionCall part. Matches _call_gemini's
        # accompanying_text handling (see that function's own comment) —
        # this was the same bug, just on the fallback protocol instead.
        return {"tool_call": {"name": tc.get("name"), "args": args}, "text": message.get("content") or None, "raw_message": message}

    _text = message.get("content", "") or ""
    if _fr == "length" and _text.strip():
        # Truncated by the token cap. Keep what we got (better than nothing)
        # but say so, instead of presenting a sentence that stops mid-word
        # as a finished answer.
        logger.warning(f"OPENAI-COMPAT TRUNCATED provider={provider['name']} model={model_id} chars={len(_text)}")
        _text = _text.rstrip() + "\n\n⚠️ *This reply was cut off (length limit). Send \"continue\" for the rest.*"
    return {"tool_call": None, "text": _text, "raw_message": message}


async def _call_openai_compatible(provider: dict, model_id: str, messages: list, tools: list) -> dict:
    """
    Entry point for every OpenAI-compatible call. A provider with
    "api_key_envs" (a list of env var names) gets tried key by key: if one
    key fails (quota, auth, server error, network), the next key is tried
    with the same model. Providers with only "api_key_env" are unchanged.
    """
    names = provider.get("api_key_envs")
    if not names:
        return await _call_openai_compatible_once(provider, model_id, messages, tools)

    last_error = None
    for index, name in enumerate(names):
        key = os.getenv(name, "")
        if not key:
            continue
        # The first key keeps the provider's own ledger name; each backup gets its own
        # (coreweave_2, ...), so a capped first account does not block the backup.
        spend_name = provider["name"] if index == 0 else f"{provider['name']}_{index + 1}"
        try:
            return await _call_openai_compatible_once(provider, model_id, messages, tools,
                                                      api_key_override=key, spend_name=spend_name)
        except Exception as e:
            logger.warning(f"{provider['name']} key {name} failed, trying the next key: {e}")
            last_error = e
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"none of {', '.join(names)} is set")


def _strip_images_from_openai_messages(messages: list) -> list:
    """
    Companion to translate_gemini_to_openai(keep_images=True) — takes
    messages that may contain vision content-block lists (from a user
    turn that had an image) and returns an equivalent text-only copy for
    handing to a model whose registry entry says supports_vision=False.
    Non-destructive: returns new dicts/lists, doesn't mutate the input,
    since the same translated `openai_messages` gets tried against
    several models in one decide_fallback() walk and a vision-capable
    one tried later in the loop still needs the original image blocks
    intact.
    """
    out = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            has_image = any(b.get("type") == "image_url" for b in content)
            if has_image:
                text = f"{text}\n[An image was attached here, but the current fallback model can't see images.]"
            out.append({**msg, "content": text})
        else:
            out.append(msg)
    return out


def _surface_allowed(provider: dict, surface: str) -> bool:
    """
    Generic per-surface gate. A provider with no \"surfaces\" key is
    available on every surface. A provider WITH the key is only available
    when the request's surface is in that list. surface=None (any caller
    that hasn't passed it through) matches nothing, so it fails closed.
    """
    allowed = provider.get("surfaces")
    if allowed is None:
        return True
    return surface is not None and surface in allowed


class QuotaExceeded(Exception):
    pass


class ProviderUnavailable(Exception):
    """
    ADDED 2026-09-04. Raised for an HTTP 410 (Gone) response — unlike a
    429 (this specific request is over quota) or a 5xx (transient), 410
    means the ENDPOINT ITSELF is gone, which by definition applies to
    every model behind it, not just the one requested. Confirmed in
    production logs: GitHub Models returned 410
    "github_models_retirement_brownout" for gpt-4o, then
    Llama-3.3-70B-Instruct, then gpt-4o-mini — three back-to-back
    guaranteed-identical failures, each logged as a separate ERROR with
    the full JSON error body, before finally falling through to
    nvidia_nim. Distinguished from RuntimeError so decide_fallback can
    log it once (not once per doomed model) and skip straight to the
    NEXT PROVIDER instead of working through the rest of this one's
    models one by one, each repeating the same outage.
    """
    pass


# ── ROUTER ──

class ProviderRouter:
    """
    router.decide(...) tries Gemini first (best quality, only one with
    real vision), then falls through provider_registry.py's other
    entries in order, skipping anything quota_tracker says is out of
    headroom, until one actually answers.
    """

    def _parse_local_qwen_tool_call(self, text: str) -> Optional[dict]:
        """
        ADDED 2026-09-07 — local_qwen_cli was fine-tuned on dataset.jsonl
        examples that emit tool calls as raw JSON-in-text, e.g.:
            {"tool_call": {"name": "git_status", "args": {}}}
        This is the PRE-2026-08-23 style OADSON's main Gemini/OpenAI
        paths moved away from (see tool_loop.py's NATIVE FUNCTION-CALLING
        comment) — but it's exactly what this specific model was trained
        to produce, confirmed directly against 227 matching examples in
        the training set. Rather than retraining the model to speak
        native function-calling (a much bigger undertaking), this parses
        its actual trained output format back into the same
        {"name": ..., "args": {...}} shape tool_loop.py already expects
        from every other provider.

        Looks for a JSON object anywhere in the text (the model may emit
        pure JSON, or JSON with surrounding prose per the training
        examples). Returns the parsed {"name": ..., "args": dict} on a
        structurally valid match, or None if no tool_call is present or
        parsing fails for any reason — never raises, so a malformed or
        absent tool call just falls through to plain-text handling.
        """
        if not text:
            return None
        # Find the first '{' that plausibly starts a tool_call object —
        # avoids false-positives on unrelated braces in prose by
        # requiring "tool_call" to actually appear in the candidate span.
        if '"tool_call"' not in text:
            return None
        start = text.find("{")
        if start == -1:
            return None
        # Walk brace depth to find the matching close — handles nested
        # args objects (e.g. create_file's {"content": "..."}) correctly,
        # unlike a naive first-'{'-to-last-'}' slice which can overshoot
        # past trailing prose containing its own unrelated braces.
        depth = 0
        end = None
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return None
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
        tool_call = parsed.get("tool_call")
        if not isinstance(tool_call, dict) or "name" not in tool_call:
            return None
        return {"name": tool_call["name"], "args": tool_call.get("args", {})}

    def _build_local_qwen_tool_instruction(self) -> str:
        """
        ADDED 2026-09-08 — local_qwen_cli was trained (per dataset.jsonl,
        227 examples) to emit tool calls as bare JSON in its completion
        text: {"tool_call": {"name": "...", "args": {...}}}. But the
        system prompt this function receives is OADSON's main prompt,
        written for Gemini's NATIVE function-calling (see
        local_tool_schemas.py's 2026-08-23 migration note) — it never
        tells the model to use that JSON-in-text shape, so without this,
        Qwen (which has no API-level tools schema forcing its output
        format, since _call_openai_compatible is called with tools=None)
        falls back to describing what it would run in prose/markdown
        instead of actually emitting a parseable tool_call. Confirmed
        live: asked to run a tool, it replied with a markdown ```bash
        run_bash "ls -la" ``` block — exactly the "narrates instead of
        calls" failure mode this instruction exists to prevent.

        Tool names/args are pulled from LOCAL_TOOL_DECLARATIONS (the
        same authoritative source Gemini's native schemas use) rather
        than hardcoded here, so this stays in sync automatically if
        local tools are ever added, renamed, or have args change.
        """
        lines = [
            "You can call ONE tool per turn by replying with ONLY this exact JSON shape, nothing else — no markdown, no code fences, no explanation before or after it:",
            '{"tool_call": {"name": "<tool_name>", "args": {...}}}',
            "",
            "Available tools:",
        ]
        for decl in LOCAL_TOOL_DECLARATIONS:
            props = decl.get("parameters", {}).get("properties", {})
            arg_names = ", ".join(props.keys()) if props else "(no args)"
            lines.append(f"- {decl['name']}({arg_names}): {decl['description']}")
        lines.append("")
        lines.append("If no tool is needed, just reply normally in plain text.")
        return "\n".join(lines)

    async def try_local_qwen_cli(self, system: str, contents: list) -> Optional[dict]:
        # REMOVED 2026-10-21 — local_qwen_cli is no longer in the registry.
        return None
        """
        ADDED 2026-09-07 — dedicated terminal-first entry point for the
        self-hosted local_qwen_cli model, called from tool_loop.py BEFORE
        decide_gemini on the terminal surface (see that call site's
        comment for the full rationale). Distinct from decide_fallback,
        which only reaches local_qwen_cli deep in a Gemini-exhaustion
        walk that rarely triggers in practice on a free-tier key that
        hasn't run dry yet.

        UPDATED 2026-09-07: originally text-only (no tool-calling), on
        the assumption the model couldn't call tools at all. Confirmed
        against dataset.jsonl that it WAS trained on tool-calling — just
        in a JSON-in-text format (see _parse_local_qwen_tool_call's
        docstring), not native function-calling. Every response is now
        run through that parser; a detected tool_call is returned in the
        exact same shape decide_gemini/continue_openai already produce,
        so tool_loop.py's execution logic (which reads
        decision["tool_call"]["name"]/["args"]) needs no changes at all
        to handle a local_qwen_cli-originated tool call.

        Returns {"tool_call": {...} | None, "text": str,
        "model_used": str} on success, or None on ANY failure — tunnel
        down, server not running, timeout, non-200, malformed body.
        Callers treat a returned None as "proceed to Gemini as normal";
        this never raises.
        """
        provider = get_provider("local_qwen_cli")
        if provider is None:
            return None
        model_id = provider["models"][0]["id"]
        # Prepend the JSON-tool-call instruction ONLY for this call — the
        # caller's own `system` (OADSON's main, Gemini-oriented prompt)
        # is left untouched for every other use elsewhere in the turn.
        qwen_system = self._build_local_qwen_tool_instruction() + "\n\n" + system
        messages = translate_gemini_to_openai(qwen_system, contents, keep_images=False)
        # REINFORCEMENT 2026-09-08: the full instruction living only at
        # the top of `system` was confirmed live to lose out to
        # conversation history once a session has a few turns in it —
        # the model reverted to narrating a tool call as markdown on a
        # fresh request, and only produced real JSON after the user
        # explicitly said "use tool call like you were trained" a
        # message later. A short, terse reminder placed at the END of
        # the last user message (not the system prompt) sits closer to
        # what the model actually attends to right before responding.
        #
        # Appended to the EXISTING last message rather than as a new
        # separate one — dataset.jsonl confirms this model was only
        # trained on single-turn instruction/output pairs, so it's never
        # seen back-to-back user-role messages either. Modifying the
        # real last message keeps the turn structure (one user message,
        # then the expected output) as close to its actual training
        # shape as this multi-turn calling context allows.
        if messages and messages[-1]["role"] == "user":
            reminder = '\n\n(Reminder: if a tool is needed, respond with ONLY {"tool_call": {"name": "...", "args": {...}}} — no markdown, no explanation.)'
            if isinstance(messages[-1]["content"], str):
                messages[-1]["content"] += reminder
            # If content is a list (vision-style content blocks) rather
            # than a plain string, leave it alone — local_qwen_cli has
            # no vision support (keep_images=False above guarantees
            # text-only content here in practice), so this branch is a
            # defensive no-op, not an expected path.
        try:
            result = await _call_openai_compatible(provider, model_id, messages, tools=None)
            raw_text = result.get("text", "") or ""
            tool_call = self._parse_local_qwen_tool_call(raw_text)
            return {
                "tool_call": tool_call,
                # When the reply IS a tool call, don't also surface the
                # raw JSON as chat text — matches decide_gemini/
                # _call_openai_compatible's own convention of only
                # setting "text" to genuine accompanying prose, not the
                # call itself (see _call_openai_compatible's WORDING
                # CONTINUITY comment for the precedent this follows).
                "text": None if tool_call else raw_text,
                "model_used": f"local_qwen_cli/{model_id}",
            }
        except Exception as e:
            logger.warning(f"local_qwen_cli (terminal-first) unavailable: {e}")
            return None

    async def decide_terminal_first(
        self,
        system: str,
        contents_gemini_format: list,
        function_declarations: list,
        needs_tools: bool,
        openai_messages: list = None,
    ) -> dict:
        """
        ADDED 2026-11-02 — terminal turns try GPT OSS 20B on CoreWeave first.
        Gemini stays first everywhere else.

        Tries exactly ONE model and raises on any failure, so the caller in
        tool_loop.py falls through to decide_gemini. Same return shape as
        decide_preferred: model_used, protocol "openai", and openai_messages,
        so the tool loop can continue on this model with continue_openai.

        The model is looked up by provider AND id, because the same id
        ("openai/gpt-oss-20b") also exists under groq, which is earlier in
        the registry.
        """
        provider = get_provider("coreweave")
        if provider is None:
            raise RuntimeError("terminal-first: the coreweave provider is not in the registry")

        if openai_messages is None:
            openai_messages = translate_gemini_to_openai(system, contents_gemini_format, keep_images=True)
        # Every terminal-first model is text-only, so strip images once.
        text_messages = _strip_images_from_openai_messages(openai_messages)
        openai_tools = gemini_declarations_to_openai_tools(function_declarations) if needs_tools else None

        last_error = None
        for model_id in TERMINAL_FIRST_MODELS:
            model = next((m for m in provider["models"] if m["id"] == model_id), None)
            if model is None:
                last_error = RuntimeError(f"{model_id} is not in the coreweave registry")
                continue
            if needs_tools and not model.get("supports_tools"):
                continue
            if not quota_tracker.can_use(provider["name"], model["id"], rpm=model.get("rpm"), rpd=model.get("rpd")):
                continue
            try:
                result = await _call_openai_compatible(provider, model["id"], text_messages, openai_tools)
            except Exception as e:
                # Any failure (quota, dead endpoint, bad response) moves on to the next model.
                logger.warning(f"terminal-first {model_id} failed, trying next: {e}")
                last_error = e
                continue
            quota_tracker.record_use(provider["name"], model["id"])
            result["model_used"] = f"{provider['name']}/{model['id']}"
            result["protocol"] = "openai"
            result["openai_messages"] = text_messages
            return result

        raise RuntimeError(f"terminal-first: every model failed, last error: {last_error}")

    async def decide_preferred(
        self,
        preferred_model: str,
        system: str,
        contents_gemini_format: list,
        function_declarations: list,
        needs_tools: bool,
        surface: str = None,
    ) -> dict:
        """
        ADDED 2026-09-09 — user-selectable model, web/app/pwa surfaces
        only (see api/chat.py's ChatRequest.model / api/chat_stream.py's
        ChatStreamRequest.model docstring for the surface gate — this
        function itself has no surface restriction beyond the
        local_qwen_cli check below, since that gate belongs at the one
        real registry-walk call site same as decide_fallback's).

        Tries exactly ONE model — whichever preferred_model names — and
        ONLY that one. Does not walk the registry, does not try sibling
        models on the same provider. On ANY failure (quota, dead model,
        wrong shape, provider not found) raises so the caller's existing
        try/except falls through to the normal decide_gemini →
        decide_fallback chain completely unchanged — this function is a
        thin "try the user's pick first" wrapper in front of that chain,
        never a replacement for it. That's the whole point: selecting a
        model is an optimistic fast-path, not a guarantee, and reliability
        never regresses for a surface that doesn't use this at all
        (telegram/whatsapp/CLI never call this function).

        Returns the same shape decide_gemini/decide_fallback already
        produce (model_used + protocol, plus either the gemini result
        fields or openai_messages) so tool_loop.py's existing dispatch —
        `if active_protocol == "gemini": ... else: continue_openai(...)`
        — needs no new branch to consume whatever this returns; the
        caller just sets active_protocol from decision["protocol"] same
        as it already does after a decide_fallback switch.
        """
        owner = None
        model = None
        for provider in PROVIDERS:
            for m in provider["models"]:
                if m["id"] == preferred_model:
                    owner, model = provider, m
                    break
            if owner:
                break

        if owner is None:
            raise RuntimeError(f"decide_preferred: '{preferred_model}' not found in current PROVIDERS registry")

        # Same terminal-only gate decide_fallback enforces at its one
        # real call site (provider_registry.py's local_qwen_cli entry
        # comment) — a picked model doesn't get to bypass this just
        # because it was explicitly requested.
        if not _surface_allowed(owner, surface):
            raise RuntimeError(f"decide_preferred: {owner['name']} is not enabled for surface={surface}")

        if needs_tools and not (model.get("supports_tools", owner.get("supports_tools"))):
            raise RuntimeError(f"decide_preferred: '{preferred_model}' does not support tools and this turn needs them")

        if not quota_tracker.can_use(owner["name"], model["id"], rpm=model.get("rpm"), rpd=model.get("rpd")):
            raise QuotaExceeded(f"decide_preferred: '{preferred_model}' proactively over quota")

        if owner["protocol"] == "gemini":
            result = await _call_gemini(model["id"], system, contents_gemini_format, function_declarations if needs_tools else None)
            quota_tracker.record_use(owner["name"], model["id"])
            result["model_used"] = model["id"]
            result["protocol"] = "gemini"
            return result

        # openai-protocol provider — same translate step decide_fallback
        # does on its first switch (keep_images=True; vision-capable
        # models get the real images, others get them stripped below).
        openai_messages = translate_gemini_to_openai(system, contents_gemini_format, keep_images=True)
        model_supports_vision = model.get("supports_vision", owner.get("supports_vision", False))
        if not model_supports_vision:
            openai_messages = _strip_images_from_openai_messages(openai_messages)
        openai_tools = gemini_declarations_to_openai_tools(function_declarations) if needs_tools else None

        result = await _call_openai_compatible(owner, model["id"], openai_messages, openai_tools)
        quota_tracker.record_use(owner["name"], model["id"])
        result["model_used"] = f"{owner['name']}/{model['id']}"
        result["protocol"] = "openai"
        result["openai_messages"] = openai_messages
        return result

    async def decide_gemini(self, system: str, contents: list, function_declarations: list) -> dict:
        """
        Try Gemini specifically. Was hardcoded to PROVIDERS[0]["models"][0]
        only (gemini-3.5-flash-lite) — gemini-3.1-flash-lite sits right
        there in the registry with its own separate rpm/rpd headroom and
        never got tried at all, meaning the moment the first model ran
        out this jumped straight past an entire free Gemini model to
        Groq/NVIDIA/Mistral. Now walks every Gemini model in registry
        order, same proactive-quota-check pattern as decide_fallback,
        before letting QuotaExceeded propagate up to the real fallback.
        """
        last_error = None
        _walk_t0 = time.monotonic()
        _skipped: list = []
        for model in PROVIDERS[0]["models"]:
            model_id = model["id"]
            if not quota_tracker.can_use("gemini", model_id, rpm=model.get("rpm"), rpd=model.get("rpd")):
                last_error = f"gemini:{model_id} (proactive)"
                _cd = quota_tracker.cooldown_remaining("gemini", model_id)
                _skipped.append(f"{model_id}({'cooldown ' + str(int(_cd)) + 's' if _cd else 'rpm/rpd'})")
                continue
            _t = time.monotonic()
            try:
                result = await _call_gemini(model_id, system, contents, function_declarations)
                quota_tracker.record_use("gemini", model_id)
                result["model_used"] = model_id
                result["protocol"] = "gemini"
                logger.info(
                    f"ROUTE decide_gemini OK model={model_id} call={time.monotonic()-_t:.2f}s "
                    f"walk_total={time.monotonic()-_walk_t0:.2f}s skipped=[{', '.join(_skipped)}]"
                )
                return result
            except QuotaExceeded as e:
                last_error = str(e)
                logger.warning(f"ROUTE decide_gemini {model_id} QUOTA after {time.monotonic()-_t:.2f}s — next")
                continue
            except Exception as e:
                # FIX 2026-08-08: this used to only catch QuotaExceeded —
                # any OTHER failure on a single model (wrong/retired model
                # ID returning 404, a malformed-request 400, whatever)
                # propagated straight out of this whole function instead
                # of just skipping that one model. Concretely: a retired
                # experimental ID sitting further down this list (Google
                # deprecates *-exp models aggressively) would abort the
                # ENTIRE gemini attempt the moment quota exhaustion on the
                # earlier, valid models forced the loop to reach it — which
                # then aborted the real fallback chain too, since the
                # caller's non-QuotaExceeded branch gives up entirely
                # instead of trying Groq/NVIDIA/Mistral/OpenRouter. One
                # dead model ID was silently taking down the whole
                # multi-provider system on quota-exhaustion days. Now a
                # hard failure on any single model just moves on to the
                # next one, same as a quota failure does.
                logger.warning(f"gemini:{model_id} failed (non-quota) after {time.monotonic()-_t:.2f}s: {e} — trying next model")
                last_error = f"gemini:{model_id}: {e}"
                continue
        logger.error(
            f"ROUTE decide_gemini EXHAUSTED after {time.monotonic()-_walk_t0:.2f}s "
            f"skipped=[{', '.join(_skipped)}] last_error={last_error}"
        )
        raise QuotaExceeded(f"all gemini models exhausted or failed: {last_error}")

    async def decide_fallback(self, system: str, contents_gemini_format: list, function_declarations: list, needs_tools: bool, openai_messages: list = None, surface: str = None) -> dict:
        """
        Called when Gemini itself is exhausted this turn, OR when the
        current fallback model also runs dry mid-turn and the chain
        needs re-walking from the top.

        openai_messages: pass the CURRENT up-to-date OpenAI-format
        conversation here on a re-walk (i.e. when already past the first
        switch away from Gemini) instead of leaving it None. Every tool
        call/response made after the first fallback switch is appended
        ONLY to openai_messages (see orchestrator.py's active_protocol
        branching) — contents_gemini_format stops being updated the
        moment that first switch happens. Re-translating from
        contents_gemini_format on a second/third/etc. walk was silently
        discarding all of that progress and re-deciding from a stale,
        incomplete snapshot of the conversation — which is exactly the
        kind of thing that can produce a structurally invalid message
        list (e.g. Mistral's "expected last role user or tool, got
        assistant" 400s seen in production) once new turns get appended
        on top of a truncated history that doesn't actually match what
        happened. Only translate from contents_gemini_format when this
        is genuinely the FIRST fallback of the turn (openai_messages is
        still None at that point, since nothing's diverged yet).
        """
        if openai_messages is None:
            openai_messages = translate_gemini_to_openai(system, contents_gemini_format, keep_images=True)
        openai_tools = gemini_declarations_to_openai_tools(function_declarations) if needs_tools else None
        text_only_messages = None  # built lazily, at most once, only if a non-vision model is actually tried

        last_error = None
        for provider in PROVIDERS[1:]:  # skip gemini, already tried
            if needs_tools and not provider.get("supports_tools"):
                continue
            # GATE 2026-09-06 — local_qwen_cli is a tunnel-exposed,
            # unauthenticated endpoint (see provider_registry.py's entry
            # comment). Restricting it to surface="terminal" only, so
            # telegram/whatsapp/web requests never route to it. This is
            # the one real call site PROVIDERS gets walked from for the
            # fallback chain, so the check belongs here rather than in
            # the registry, which has no per-channel concept. `surface`
            # defaults to None (not "terminal") so any caller that hasn't
            # been updated to pass it through fails closed — skips this
            # provider — rather than accidentally exposing it.
            if not _surface_allowed(provider, surface):
                continue
            for model in provider["models"]:
                if needs_tools and not model.get("supports_tools"):
                    continue
                if not quota_tracker.can_use(provider["name"], model["id"], rpm=model.get("rpm"), rpd=model.get("rpd")):
                    continue

                # ADDED 2026-08-30: pick image-preserving messages for a
                # vision-capable model, text-only for everything else.
                # Registry now has real vision entries in this chain
                # (meta/muse-glimmer-30b, minimax/minimax-m3:free — see
                # provider_registry.py) alongside non-vision ones tried
                # in the same walk, so which copy to send has to be
                # decided per model, not once for the whole function.
                model_supports_vision = model.get("supports_vision", provider.get("supports_vision", False))
                if model_supports_vision:
                    messages_for_this_model = openai_messages
                else:
                    if text_only_messages is None:
                        text_only_messages = _strip_images_from_openai_messages(openai_messages)
                    messages_for_this_model = text_only_messages

                # Proactive size check — a provider whose token-per-minute
                # cap is smaller than the current conversation is a
                # guaranteed 413, every single time, no matter how many
                # times it's retried (confirmed in production: Groq's
                # on_demand tier caps at 8000 TPM, and a conversation with
                # several tool-call round trips easily clears 20K+ tokens).
                # Same proactive-skip philosophy as quota_tracker.can_use()
                # above — don't attempt what's already known to fail.
                tpm_limit = model.get("tpm")
                if tpm_limit:
                    estimated_tokens = len(json.dumps(messages_for_this_model)) // 4
                    if estimated_tokens > tpm_limit:
                        logger.warning(f"Skipping {provider['name']}/{model['id']} — conversation is ~{estimated_tokens} tokens, over its {tpm_limit} TPM cap")
                        continue
                _ft = time.monotonic()
                try:
                    result = await _call_openai_compatible(provider, model["id"], messages_for_this_model, openai_tools)
                    quota_tracker.record_use(provider["name"], model["id"])
                    result["model_used"] = f"{provider['name']}/{model['id']}"
                    result["protocol"] = "openai"
                    result["openai_messages"] = messages_for_this_model
                    logger.info(f"ROUTE decide_fallback OK {provider['name']}/{model['id']} call={time.monotonic()-_ft:.2f}s")
                    return result
                except QuotaExceeded:
                    logger.warning(f"Quota hit — {provider['name']}/{model['id']}, trying next")
                    continue
                except ProviderUnavailable as e:
                    # ADDED 2026-09-04 — see class docstring. Log ONCE
                    # per provider (not once per model, which is what
                    # this replaced — GitHub Models' retirement brownout
                    # was logging 3 near-identical ERROR lines with a
                    # full JSON body each, once per tool-capable model
                    # tried, on EVERY fallback walk) at WARNING (this is
                    # an expected, handled condition — the walk
                    # continues to the next provider right below, it's
                    # not an unhandled failure), then stop trying this
                    # provider's remaining models entirely — they're all
                    # behind the same gone endpoint and would fail
                    # identically.
                    logger.warning(f"{provider['name']} unavailable (410 — endpoint gone, not just this model): {e}")
                    last_error = e
                    break
                except Exception as e:
                    logger.error(f"{provider['name']}/{model['id']} failed: {e}")
                    last_error = e
                    continue

        raise RuntimeError(f"All fallback providers exhausted. Last error: {last_error}")

    async def continue_openai(self, provider_name_model: str, openai_messages: list, tools: list) -> dict:
        """Continue subsequent loop iterations on the SAME fallback model once already switched, rather than re-walking the whole chain every iteration."""
        # DEFENSIVE 2026-09-07: was a bare `.split("/", 1)` unpack — a
        # malformed provider_name_model (missing the "/" entirely, e.g.
        # if active_model ever ends up holding a bare Gemini model ID
        # like "gemini-3.7-flash" while active_protocol says "openai",
        # a state mismatch between the two that shouldn't happen but
        # wasn't being defended against) raised a bare ValueError with
        # no indication of what the actual bad value was — confirmed in
        # production as "not enough values to unpack (expected 2, got 1)"
        # with zero context on which string caused it. Now raises a
        # specific, readable RuntimeError naming the exact value instead.
        if "/" not in provider_name_model:
            raise RuntimeError(
                f"continue_openai: expected 'provider/model' but got model_used="
                f"{provider_name_model!r} (no '/' present) — active_protocol/active_model "
                f"state mismatch, see tool_loop.py's decision handling"
            )
        provider_name, model_id = provider_name_model.split("/", 1)
        # BUGFIX 2026-09-02: both next(...) calls below previously had no
        # default, so a provider/model no longer present in PROVIDERS
        # (e.g. a session persisting a stale `model_used` string from
        # before a registry change — exactly what happened when
        # OpenRouter's static per-model entries were replaced with the
        # single openrouter/free router model) raised a bare
        # StopIteration with NO message at all. str(StopIteration()) is
        # genuinely "" by default, which is exactly why a real production
        # log showed "All providers exhausted or failed: " with nothing
        # after the colon — the actual cause was silently swallowed by
        # an uninformative built-in exception instead of surfacing what
        # was actually missing. Now raises a real, readable error naming
        # the exact provider/model that couldn't be found, so this class
        # of failure is diagnosable from the log instead of a dead end.
        provider = next((p for p in PROVIDERS if p["name"] == provider_name), None)
        if provider is None:
            raise RuntimeError(f"continue_openai: provider '{provider_name}' not found in current PROVIDERS registry (from model_used='{provider_name_model}') — registry may have changed since this session's model was selected")
        model_cfg = next((m for m in provider["models"] if m["id"] == model_id), None)
        if model_cfg is None:
            raise RuntimeError(f"continue_openai: model '{model_id}' not found under provider '{provider_name}' in current PROVIDERS registry (from model_used='{provider_name_model}') — registry may have changed since this session's model was selected")
        if not quota_tracker.can_use(provider_name, model_id, rpm=model_cfg.get("rpm"), rpd=model_cfg.get("rpd")):
            # This specific model just ran out mid-turn — re-walk the
            # chain once from scratch rather than failing the whole turn.
            raise QuotaExceeded(f"{provider_name_model} (mid-turn)")
        result = await _call_openai_compatible(provider, model_id, openai_messages, tools)
        quota_tracker.record_use(provider_name, model_id)
        result["model_used"] = provider_name_model
        result["protocol"] = "openai"
        return result

    async def stream_final_answer(
        self,
        model_id: str,
        system: str,
        contents: list,
        on_token,  # async callable(token: str) — called per chunk
        on_status=None,  # optional async callable(status: str, reason: str) —
                          # see OADSON V3 Intelligent Response State EDD. Only
                          # fired for the streaming->non-streaming fallback
                          # below, the one recovery path in this file that was
                          # previously invisible to any caller (caught and
                          # handled entirely inside this try/except, with
                          # nothing surfaced outward but the final text).
                          # None by default so every existing caller of this
                          # method is unaffected.
        on_reset=None,   # optional async callable(reason: str) — ADDED
                          # 2026-09-19. Called BEFORE any replacement answer
                          # is emitted, but ONLY if the failed stream had
                          # already sent tokens to the client. Tells the
                          # client to discard the partial text it rendered.
                          # None by default so every existing caller is
                          # unaffected.
    ) -> str:
        """
        Stream the final answer token-by-token using streamGenerateContent.
        Only call this when there are no tools in the payload — i.e. the
        last turn where the model produces prose, not a function call.

        Calls on_token(chunk) for each text chunk as it arrives.
        Returns the full assembled text for persistence.

        On failure (429, timeout, dropped connection, MAX_TOKENS / safety
        stop) the answer is regenerated non-streaming, trying other Gemini
        models in registry order — the model that just failed is already on
        cooldown (see quota_tracker.mark_exhausted), so it is skipped rather
        than re-called.

        BUGFIX 2026-09-19 — two real defects in the old version:
          1. It only ever re-called the SAME model that had just failed, so
             a model that hit its limit mid-answer failed again, identically.
          2. If the stream had already emitted 80% of the answer before
             failing, the fallback then emitted the WHOLE answer again on
             top of it (on_token(text)) — the client showed the partial
             text followed by a complete copy. on_reset() now clears the
             client's partial text first.
        """
        full_text: list = []
        emitted = False
        failure: Exception | None = None

        try:
            async for chunk in _stream_gemini(model_id, system, contents):
                full_text.append(chunk)
                emitted = True
                await on_token(chunk)
            return "".join(full_text)
        except Exception as e:
            failure = e
            logger.warning(
                f"Streaming failed on {model_id} after emitting "
                f"{sum(len(c) for c in full_text)} chars ({type(e).__name__}: {e}) — regenerating"
            )

        if on_status:
            try:
                await on_status("working_on_it", "streaming_fallback")
            except Exception:
                pass  # status signal is best-effort — never let it break the actual fallback

        if emitted and on_reset:
            try:
                await on_reset(f"{type(failure).__name__}: {failure}"[:200])
            except Exception as reset_e:
                logger.warning(f"on_reset callback failed (non-fatal): {reset_e}")

        # Regenerate non-streaming. Walk Gemini models in registry order,
        # skipping any on cooldown, so we don't just hit the same dead model.
        last_error: Exception | None = failure
        for model in PROVIDERS[0]["models"]:
            mid = model["id"]
            if not quota_tracker.can_use("gemini", mid, rpm=model.get("rpm"), rpd=model.get("rpd")):
                continue
            try:
                result = await _call_gemini(mid, system, contents, tools=[])
                quota_tracker.record_use("gemini", mid)
                finish = (result.get("raw_parts") or [{}])
                text = result.get("text") or ""
                if not text.strip():
                    continue
                logger.info(f"Streaming fallback answered via {mid} ({len(text)} chars)")
                await on_token(text)  # single chunk — the client was reset above if needed
                return text
            except QuotaExceeded as qe:
                last_error = qe
                continue
            except Exception as e:
                logger.warning(f"Streaming fallback model {mid} failed: {e}")
                last_error = e
                continue

        # Every Gemini model failed. If we had partial text, returning it is
        # better than returning nothing — but it MUST be flagged incomplete
        # rather than passed off as a finished answer.
        partial = "".join(full_text)
        if partial.strip():
            logger.error(f"All fallback models failed; returning INCOMPLETE partial ({len(partial)} chars): {last_error}")
            note = "\n\n⚠️ *This reply was cut off before it finished. Send \"continue\" to get the rest.*"
            # What the client is showing right now depends on whether we told
            # it to reset earlier. If on_reset ran, its screen was WIPED, so
            # the whole partial must be re-sent (with the note). If it did
            # not run (no on_reset hook, so nothing was cleared), it still
            # has the partial and only needs the note appended.
            if emitted and on_reset:
                await on_token(partial + note)
            else:
                await on_token(note)
            return partial + note
        raise RuntimeError(f"stream_final_answer: every Gemini model failed. Last error: {last_error}")
