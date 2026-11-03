"""
core/anthropic_native.py — Claude via the NATIVE Messages API, with prompt caching.

Why native and not the OpenAI-compatible layer: Anthropic documents that the
OpenAI layer does not support prompt caching. Caching is what makes repeated
repo context cheap: cache reads cost 10% of normal input, cache writes cost
125%. Native API lets us put cache_control breakpoints on:
  1. the system prompt (incl. the stable repo map)       — breakpoint 1
  2. the last tool definition (caches all tool schemas)  — breakpoint 2
  3. the last block of the previous message (history)    — breakpoint 3
Anthropic allows up to 4 breakpoints. Cache entries have a 5-minute TTL
(ephemeral), refreshed on every hit.

Input format is OpenAI-style messages (what the tool loop already builds);
this module converts them. Tool call IDs are reused as-is, because the loop
generates them and uses the same ID on the assistant tool_calls and on the
matching tool message, which is exactly what Anthropic requires.
"""
import json
import logging
import os

import httpx

logger = logging.getLogger("oadson.anthropic_native")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 20000
_CACHE = {"type": "ephemeral"}


class ClaudeQuota(Exception):
    """Rate limit, overload, network failure. Caller treats it as QuotaExceeded."""


def _text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text")


def _user_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            blocks.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": head[5:] or "image/png", "data": data}})
    return blocks


def to_anthropic(messages: list) -> tuple[str, list]:
    """OpenAI-style messages -> (system_text, anthropic_messages)."""
    system_parts, out = [], []
    seen_tool_ids = set()  # Anthropic rejects a tool_result with no matching tool_use (400)
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            t = _text_of(m.get("content"))
            if t:
                system_parts.append(t)
        elif role == "assistant":
            blocks = []
            t = _text_of(m.get("content"))
            if t:
                blocks.append({"type": "text", "text": t})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                seen_tool_ids.add(tc["id"])
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {}})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            if m.get("tool_call_id") not in seen_tool_ids:
                continue  # orphan result: drop instead of letting the API 400 the whole turn
            out.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                "content": _text_of(m.get("content"))}]})
        else:
            blocks = _user_blocks(m.get("content"))
            if blocks:
                out.append({"role": "user", "content": blocks})

    # Anthropic wants alternating roles in practice; merge same-role runs
    # (e.g. several tool results in a row become one user message).
    merged = []
    for msg in out:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = merged[-1]["content"] + msg["content"]
        else:
            merged.append(msg)
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return "\n\n".join(system_parts), merged


def openai_tools_to_anthropic(tools: list) -> list:
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({"name": fn["name"], "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


async def call_claude(provider: dict, model_id: str, messages: list, tools: list) -> dict:
    api_key = os.getenv(provider.get("api_key_env", ""), "")
    if not api_key:
        raise RuntimeError(f"{provider.get('api_key_env')} not set")

    system, msgs = to_anthropic(messages)
    body = {"model": model_id, "max_tokens": MAX_TOKENS, "messages": msgs}

    if system:
        body["system"] = [{"type": "text", "text": system, "cache_control": _CACHE}]
    if tools:
        at = openai_tools_to_anthropic(tools)
        at[-1]["cache_control"] = _CACHE
        body["tools"] = at
    if len(msgs) >= 2 and isinstance(msgs[-2]["content"], list) and msgs[-2]["content"]:
        msgs[-2]["content"][-1]["cache_control"] = _CACHE

    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION,
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            res = await client.post(API_URL, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ClaudeQuota(f"claude network error {type(e).__name__}") from e

    if res.status_code in (429, 500, 502, 503, 529):
        raise ClaudeQuota(f"claude {res.status_code}: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"claude error {res.status_code}: {res.text[:300]}")

    data = res.json()
    content = data.get("content", [])
    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text" and b.get("text")) or None
    tool_uses = [b for b in content if b.get("type") == "tool_use"]

    u = data.get("usage", {}) or {}
    cr = int(u.get("cache_read_input_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    usage = {"prompt_tokens": inp + cr + cw, "completion_tokens": out,
             "cache_read_tokens": cr, "cache_write_tokens": cw}
    logger.info(f"CLAUDE {model_id} in={inp} cache_read={cr} cache_write={cw} out={out} stop={data.get('stop_reason')}")

    if data.get("stop_reason") == "max_tokens" and text:
        text = text.rstrip() + "\n\n⚠️ *This reply was cut off (length limit). Send \"continue\" for the rest.*"

    raw = {"role": "assistant", "content": text or ""}
    result = {"tool_call": None, "text": text, "raw_message": raw, "usage": usage}
    if tool_uses:
        tu = tool_uses[0]
        raw["tool_calls"] = [{"id": tu["id"], "type": "function",
                              "function": {"name": tu["name"], "arguments": json.dumps(tu.get("input", {}))}}]
        result["tool_call"] = {"name": tu["name"], "args": tu.get("input", {})}
    return result
