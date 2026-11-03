"""
core/provider_registry.py — OADSON V3 Provider Registry.

Declarative list of every model OADSON can call, across every provider.
Adding a new provider or model is meant to be ONE new entry here — no
changes needed anywhere else. core/provider_router.py reads this list to
build fallback chains; core/quota_tracker.py reads the rate-limit numbers
to proactively avoid a model that's close to its limit instead of only
reacting to a 429 after the fact.

Rate limits below are sourced from each provider's published free-tier
docs (verified via web search / live testing when added, not guessed) —
but these numbers change without much notice on every provider's side.
Treat them as reasonable defaults, not guarantees. Worth a periodic
recheck against each provider's docs.

PROTOCOL determines which adapter core/provider_router.py uses:
  "gemini" — Gemini's own contents/functionDeclarations/functionCall shape.
  "openai" — the OpenAI-compatible chat/completions shape (tools/
             tool_calls) that Groq, NVIDIA NIM, OpenRouter, and most
             other free-tier inference providers all speak.

  "surfaces" (optional) — list of surfaces this provider may be used on
             (e.g. ["terminal"]). Absent = every surface. Enforced in
             provider_router.py via _surface_allowed(); fails closed on
             an unknown/None surface.

Full history of what's been tried, removed, and why — every dead model,
every payment wall, every deprecation — lives in core/PROVIDER_HISTORY.md,
not here. Check that file before re-adding anything that looks familiar;
it exists specifically so the same dead end doesn't get re-discovered
from scratch. When you remove or demote something below, move its
reasoning there instead of leaving a paragraph in this file.
"""

PROVIDERS = [
    {
        "name": "gemini",
        "protocol": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/",
        "supports_tools": True,
        "supports_vision": True,
        "models": [
            # ── PRIMARY ──────────────────────────────────────────────
            {"id": "gemini-3.8-flash", "tier": "primary", "rpm": 15, "rpd": 1500, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-3.7-flash", "tier": "primary", "rpm": 15, "rpd": 1500, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-3.6-flash", "tier": "primary", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-3.5-flash", "tier": "primary", "rpm": 10, "rpd": 250, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-3.5-flash-lite", "tier": "primary", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},

            # ── SIMPLE / FAST ────────────────────────────────────────
            {"id": "gemini-3.1-flash-lite", "tier": "simple", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-3.1-flash-lite-preview", "tier": "simple", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},

            # ── FALLBACK ─────────────────────────────────────────────
            {"id": "gemini-3-flash-preview", "tier": "fallback", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-flash-lite-latest", "tier": "fallback", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},
            {"id": "gemini-flash-latest", "tier": "fallback", "rpm": 15, "rpd": 1000, "supports_tools": True, "supports_vision": True},

            # ── OPEN-WEIGHT REASONING ────────────────────────────────
            # UPDATED 2026-09-09: supports_tools flipped True on both —
            # Google confirms both Gemma 4 models support native
            # function-calling at the API level (same Gemini 3
            # architecture as the Flash models above), so there was no
            # real reason for the fallback router to skip them on every
            # tool-using turn, which is most turns. No longer a walled-off
            # "experimental" tier — these participate in the normal
            # registry-order walk and normal fallback like everything
            # else. 31B is the dense/quality variant (slower per-token,
            # ~3-4 tok/s in independent benchmarks); 26B is the MoE/speed
            # variant. Worth a real test via /engineer/test-models with
            # an actual tool-calling prompt (not a trivial one) before
            # leaning on either in production traffic.
            {"id": "gemma-4-31b-it", "tier": "reasoning", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": True},
            {"id": "gemma-4-26b-a4b-it", "tier": "reasoning", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": True},
        ],
    },
    {
        # DeepSeek V4.1 Flash — OpenAI-compatible. TERMINAL-ONLY by design:
        # its cache-hit input price ($0.003-$0.044/M) is the reason it's
        # worth using at all, and terminal sessions reuse long stable
        # prefixes that hit that cache. Web/app/telegram/whatsapp turns
        # don't have the same prefix reuse, so there's no reason to route
        # them here. Enforced via "surfaces" below.
        #
        # Model ID: "deepseek-flash" (per api-docs.deepseek.com pricing
        # page, 2026). Legacy "deepseek-chat"/"deepseek-reasoner"/
        # "deepseek-v4-flash" are retired or aliased — do not use them.
        # Cache hits are automatic on byte-identical prefixes, so keep
        # system prompt + earlier history stable across turns.
        # Pricing per 1M tokens: input $0.15 off-peak / $0.30 peak (cache
        # miss), output $0.60 / $1.20, peak = 01:00-04:00 and 06:00-10:00
        # UTC weekdays. Full history: core/PROVIDER_HISTORY.md.
        "name": "deepseek",
        "protocol": "openai",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/chat/completions",
        "supports_tools": True,
        "supports_vision": False,
        "surfaces": ["terminal"],
        "paid": True,
        "models": [
            {"id": "deepseek-flash", "tier": "primary", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
        ],
    },
    {
        "name": "groq",
        "protocol": "openai",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "supports_tools": True,
        "supports_vision": False,
        "models": [
            # Re-added 2026-09-05 for testing only — Groq's shared
            # ~6-8K org-wide TPM ceiling previously 413'd this on real
            # tool-loop turns. "simple" tier keeps blast radius small if
            # it happens again. See PROVIDER_HISTORY.md before touching.
            {"id": "openai/gpt-oss-20b", "tier": "simple", "rpm": 30, "rpd": 1000, "tpm": 8000, "supports_tools": True, "supports_vision": False},
        ],
    },
    {
        "name": "nvidia_nim",
        "protocol": "openai",
        "api_key_env": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "supports_tools": True,
        "supports_vision": True,
        "models": [
            {"id": "meta/muse-glimmer-30b", "tier": "complex", "rpm": 40, "rpd": None, "supports_tools": True, "supports_vision": True},
        ],
    },
    {
        "name": "openrouter",
        "protocol": "openai",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "supports_tools": True,
        "supports_vision": True,
        "extra_headers": {"HTTP-Referer": "http://localhost:3000", "X-Title": "OADSON V3"},
        "models": [
            # openrouter/free is OpenRouter's own router — resolves to
            # whichever free model is actually alive AND capable at
            # request time, rather than a pinned :free slug that churns
            # out from under this file (see PROVIDER_HISTORY.md — five
            # pinned slugs died within a single day before this).
            {"id": "openrouter/free", "tier": "complex", "rpm": 20, "rpd": 200, "supports_tools": True, "supports_vision": True},
        ],
    },
    {
        # CoreWeave serverless inference (OpenAI-compatible), funded by the
        # owner's $50 academic credit. These are the PAID FALLBACK models:
        # decide_fallback walks list order, so they run after every free
        # provider has failed or hit quota, and before Claude (which stays
        # last). Routine turns never reach them while free models are up.
        #
        # Model IDs are the exact API strings from
        # docs.coreweave.com/products/inference/serverless/models. They
        # contain "/", which is safe: continue_openai splits on the FIRST
        # "/" only, so "coreweave/zai-org/GLM-5.3-Flash" -> provider
        # "coreweave", model "zai-org/GLM-5.3-Flash".
        #
        # Prices and the spend cap live in core/spend_tracker.py.
        # Not verified on this account yet: run /engineer/test-models
        # first. supports_tools is an assumption until a tool-call test
        # passes on this endpoint.
        "name": "coreweave",
        "protocol": "openai",
        "api_key_env": "COREWEAVE_API_KEY",
        "base_url": "https://api.inference.wandb.ai/v1/chat/completions",
        "supports_tools": True,
        "supports_vision": False,
        "paid": True,
        # Raised reply cap for CoreWeave only. Groq keeps the 8192 default,
        # since its 8000-TPM limit would be hit by a larger reservation.
        "max_tokens": 20000,
        "models": [
            {"id": "deepseek-ai/DeepSeek-V4.1-Flash", "tier": "fallback", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
            {"id": "zai-org/GLM-5.3-Flash", "tier": "fallback", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
            # Cheapest tool-capable model on the account. Same id as groq's
            # entry above; decide_preferred picks the first match, which is
            # fine since both are the same model.
            {"id": "openai/gpt-oss-20b", "tier": "terminal", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
            {"id": "Qwen/Qwen3.6-35B-A3B", "tier": "normal", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
            {"id": "meta-llama/Llama-3.3-70B-Instruct", "tier": "normal", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
            # Last of the terminal-first chain (after GPT OSS, then DeepSeek).
            {"id": "meta-llama/Llama-3.1-8B-Instruct", "tier": "terminal", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": False},
        ],
    },
    {
        # Claude Haiku 5.5 via Anthropic's OpenAI-compatible endpoint.
        # Placed LAST on purpose: decide_fallback walks in list order, so
        # Claude is only tried after every free provider has failed/quota'd.
        # Routine chat stays on Gemini; core/model_policy.py picks Claude for
        # heavy turns on web/app/pwa/telegram/whatsapp. Never terminal
        # (DeepSeek owns that surface, for cache reasons).
        # Native Messages API: prompt caching on (system + repo map, tool
        # schemas, history). Cache reads cost 10% of input. Pricing per 1M:
        # input $0.10, output $0.50, cache write $0.125, cache read $0.01.
        "name": "anthropic",
        "protocol": "anthropic",   # native Messages API, prompt caching (core/anthropic_native.py)
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1/messages",
        "supports_tools": True,
        "supports_vision": True,
        "paid": True,
        "surfaces": ["web", "app", "pwa", "telegram", "whatsapp"],
        "models": [
            {"id": "claude-haiku-5-5", "tier": "complex", "rpm": None, "rpd": None, "supports_tools": True, "supports_vision": True},
        ],
    },
]


def get_provider(name: str) -> dict:
    return next((p for p in PROVIDERS if p["name"] == name), None)


def all_models_flat() -> list:
    out = []
    for provider in PROVIDERS:
        for model in provider["models"]:
            out.append({**model, "provider": provider["name"]})
    return out
