# Provider Registry — Change History

This is the incident log for `core/provider_registry.py`. Every entry
below was originally an inline comment in that file, explaining why a
model was added, removed, demoted, or re-tested. Moved here so the
registry itself stays a lean, current-state config file — this file is
where you look when you want to know "didn't we try that already?"

Convention: each entry keeps its original date and provider grouping.
Newest first within each provider isn't enforced — kept in the order
they originally appeared (roughly chronological per provider).

---

## gemini

**2026-11-02 — RETIRED `gemini-2.5-flash` from live code paths**
Google's API now 404s it for new users ("no longer available to new
users"). Removed from the biometrics verification default, the
memory-vault summarization call, and the provider fallback chain —
replaced with `gemini-3.8-flash` (GA 2026-09-02) or the file's
existing 3.5-flash-lite calls. Biometrics also force-upgrades any
GEMINI_THINKING_MODEL value still pointing at a retired 2.5 ID.

**2026-09-08 — ADDED `gemini-3.8-flash`**
GA as of 2026-09-02 — Google's newest Flash-tier model, same
$0.75/$3.75 per-1M introductory pricing as 3.7 Flash (holds through
2026-12-31), same 1M context window. Placed first so it's tried before
the older Flash versions.

**2026-08-08 — REMOVED `gemini-2.0-pro-exp`, `gemini-2.5-pro-exp-03-25`, `gemini-2.0-flash-thinking-exp`**
Checked live against Google's current model catalog and deprecation
notices — all three are retired (2.0-pro-exp and 2.0-flash-thinking-exp
are Dec-2024-era snapshots gone alongside the rest of the 2.0 line's
June 2026 retirement; 2.5-pro-exp-03-25 is the March 2025 preview
snapshot, listed as already removed in Google's own Jan 2026
deprecation sweep). This wasn't just dead weight — a request to a
retired ID 404s, `decide_gemini()` used to treat that as fatal instead
of skipping to the next model, and `orchestrator.py` used to treat
*that* as fatal instead of trying the non-Gemini fallback chain. One
stale model ID here was silently disabling Groq/NVIDIA/Mistral/
OpenRouter fallback entirely on any day real quota exhaustion forced
the loop far enough to reach it. Both of those have their own real
fixes now (this isn't the only line of defense), but there's no reason
to keep 3 guaranteed-404 entries in the list regardless.

---

## groq

**2026-08-30 — REMOVED `openai/gpt-oss-120b`, `openai/gpt-oss-20b`**
Both were 413'ing ("Request too large ... Limit 8000 [TPM], Requested
25822") on completely ordinary tool-loop turns, not edge cases. Every
free-tier model on Groq shares one org-wide ~6-8K TPM ceiling
regardless of which model you pick (confirmed against Groq's current
published free-tier docs) — this isn't a per-model problem fixable by
swapping IDs, it's a structural ceiling on the whole provider's free
tier. Groq also has no free vision model at all.

**2026-09-05 — REMOVED `llama-3.1-8b-instant`**
Confirmed dead via live production test (404 model_not_found) AND via
Groq's own deprecation page: shut down for free/developer tier on
2026-08-16, per an email Groq sent 2026-06-17. Not a guess.

The honest problem: Groq's own recommended replacement for this exact
model is `openai/gpt-oss-20b` — the same model already removed above
(2026-08-30) for 413'ing on ordinary tool-loop turns due to the shared
~6-8K TPM org-wide ceiling. Groq's current free production catalog is
down to `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, and
`groq/compound`, per Groq's own "Groq is not a model" retrospective on
the Aug 2026 cleanup. Not re-adding `gpt-oss-20b` blindly — re-testing
it now that `llama-3.1-8b-instant` is gone means it may get real
traffic instead of being skipped, which could resurface the same 413.
Leaving Groq with no models rather than guessing wrong twice.

**2026-09-05 — RE-ADDED `openai/gpt-oss-20b` (for testing only)**
This is the exact planned test from the note above, not a re-guess.
Kept at `"simple"` tier deliberately: if the 413/TPM ceiling
resurfaces, it only affects short/simple-tier turns instead of
becoming a primary route that breaks normal tool-loop traffic. A
trivial "reply OK" prompt passing in `/engineer/test-models` does NOT
prove the TPM ceiling is fixed, since that's a small request — the
real test is whether ordinary production-sized tool-loop turns still
413. **Currently live** with `rpm: 30, rpd: 1000, tpm: 8000` — watch
the next test run closely.

---

## nvidia_nim

**2026-08-30 — REMOVED `meta/llama-3.1-70b-instruct`**
Confirmed dead, not a rate-limit or transient failure. NVIDIA NIM
returned a hard 410 Gone: "The model 'meta/llama-3.1-70b-instruct' has
reached its end of life on 2026-08-26T09:00:00Z and is no longer
available." Same category of bug as the stale Gemini IDs above — a
guaranteed-dead entry silently eating a retry slot on every fallback
attempt before the loop could reach a model that actually works.

**2026-08-30 — ADDED `meta/muse-glimmer-30b`**
Verified live on NVIDIA's current model catalog (build.nvidia.com),
Free Endpoint available, same OpenAI-compatible endpoint/base_url as
everything else in this provider block. First NVIDIA NIM entry with
real vision support: "multimodal reasoning model accepting text and
images, with native tool-calling and separate reasoning output." 131K
context. This is also why the provider's `supports_vision` flag
flipped to True — every model in this block used to be text-only.
**Currently live** with `rpm: 40, rpd: None`.

Considered but NOT added: `minimaxai/minimax-m3` (also vision+tools on
NVIDIA NIM) — its own model page carried an explicit notice: "This API
will be deprecated on 09/08/2026." Adding a model scheduled to die in
under two weeks would just recreate the exact bug this change was
fixing. See `openrouter/free` below for a same-model pick
(`minimax/minimax-m3:free`) that wasn't under that NVIDIA-specific
sunset (also since removed — see openrouter section).

**2026-09-05 — REMOVED `mistralai/mistral-nemotron`**
Not a clean dead-model case like the 410s above, but a real
reliability pattern across two separate `/engineer/test-models` runs
same week: first a 60-second full timeout (no error body at all), then
a 500 "Internal Server Error... Inference connection error while
making inference request" on the very next run. Two different failure
modes on the SAME model, both server-side on NVIDIA's end, not a
quota/429 (which would be expected and fine). Removing rather than
demoting tier — an unreliable fallback model is worse than no fallback
model, since a request routed into it burns the timeout window before
continuing down the chain. Re-add only after several consecutive clean
passes, not a single lucky run.

**2026-09-05, same day — REMOVED (attempted add, never actually worked)**
Confirmed via live `/engineer/test-models` run — "nvidia_nim error
404: 404 page not found" (plain text, not JSON — a routing-level 404,
not a model-level one). The model was real and actively maintained on
NIM's container infrastructure (confirmed first-party via NGC build
logs), but that doesn't mean it's reachable on the standard free
chat/completions route without separate per-model-family access — NIM
gates some models behind a "Try API" registration step. Not re-adding
on a guess a second time same day — register for the specific model
family on build.nvidia.com first, THEN confirm the exact reachable
endpoint/ID before adding it back.

---

## mistral (standalone, never had its own provider block survive)

**2026-09-05 — REMOVED `mistral/mistral-small-latest`**
429 rate-limited on every single `/engineer/test-models` run it
appeared in (2 for 2), not an intermittent blip. Likely cause: `rpm`
was set to 2 — an extremely narrow window, easy to exhaust even from
spaced-out real traffic, not just back-to-back test runs. Whether the
underlying cause is the ceiling being set wrong or Mistral's actual
free tier being this tight, the observed behavior is the same either
way: consistently unusable. A model that fails 100% of the times it's
been checked is production evidence, not noise to wait out. Re-add
only with a verified real `rpm` value AND a clean pass in
`/engineer/test-models`.

---

## openrouter

**2026-08-30 — REPLACED all pinned `:free` model IDs with `openrouter/free`**
Every pinned model ID that used to live here —
`minimax/minimax-m3:free`, `z-ai/glm-5.2:free` (both added *the same
day*, earlier), plus three older pins (`meta-llama/llama-3.3-70b-
instruct:free`, `qwen/qwen-2.5-72b-instruct:free`, `mistralai/mistral-
small-3.1-24b-instruct:free`) — was confirmed gone. Checked directly
against OpenRouter's live `https://openrouter.ai/api/v1/models`
response (not a blog post, not a cached doc page): none of those five
IDs appeared anywhere in the current catalog. Two of them didn't even
survive from earlier the same day to later that day.

Not a one-off miss — OpenRouter's own docs describe their free tier as
churning by design: models rotate in as promos/prerelease checkpoints
and rotate out on no fixed schedule. Pinning a specific `:free` slug
is choosing to re-hit this exact bug on a recurring basis, no matter
how carefully the ID is verified at write time.

Replaced with `openrouter/free` — OpenRouter's own router model for
exactly this problem. Per OpenRouter's docs: "a router that selects
free models at random from the models available on OpenRouter [and]
smartly filters for models that support features needed for your
request such as image understanding, tool calling, structured outputs
and more." Resolves to whichever free model is actually alive AND
capable at request time — can't go stale the way a pinned slug just
did twice in one day. **Currently live** with
`rpm: 20, rpd: 200, tier: complex`.

---

## Dropped entirely (never made it past same-day testing)

**2026-09-05 — cerebras**
Both models confirmed dead via live `/engineer/test-models` run:
`gpt-oss-120b` returned 402 Payment Required (free tier doesn't cover
it despite docs suggesting otherwise), `llama3.1-8b` returned 404
model_not_found (renamed or pulled from the catalog). Second time this
provider's catalog changed out from under a fresh addition within 24
hours — combined with directly conflicting third-party sources on
what's actually free right now, not trustworthy enough to keep
guessing at. Not re-adding without a manual test against a real,
current API key first.

**2026-09-05, same day as added — deepseek**
User confirmed directly on the DeepSeek dashboard: balance shows
$0.00. The "5-10M free tokens, no card required" claim from multiple
third-party sources did not hold for this account — either the grant
never landed, needed a separate claim step undocumented in what was
checked, or the offer itself has changed. `base_url` and model IDs
(`deepseek-chat`, `deepseek-reasoner`) were independently correct —
this isn't a wrong-ID bug, it's a real account-balance dead end
confirmed at the source. Not re-adding without seeing an actual
non-zero balance on the dashboard first.

**2026-09-05, same day as added — sambanova**
Confirmed via live test, unambiguously: "PAYMENT_METHOD_REQUIRED",
`balance_units: 0`. Resolves a source conflict noted when this block
was added (some sources claimed no-card, one claimed card-required) —
card-required was correct, at least for this account. Real dead end
confirmed at the source, same category as deepseek above. Not
re-adding without a payment method actually attached.

**2026-09-05, same day as added — zhipu**
SMS verification failed repeatedly on bigmodel.cn signup with a
Nigerian (+234) number ("获取验证码失败，请重试" — "failed to get
verification code, please retry"). The model/base_url were never
actually confirmed working because signup itself couldn't complete.
Not a code bug, not a wrong model ID — a real access barrier for this
account's phone number, plausibly geographic. Not worth further time
chasing without a Chinese number or an alternate verification path.
Re-add only if signup actually completes by some other route.

**2026-11-01 — coreweave (DeepSeek V4.1 Flash, GLM 5.3 Flash)**
Re-added as PAID FALLBACK on a different route from the direct DeepSeek
and Z.AI APIs above. CoreWeave serverless inference is funded by a $50
academic credit the owner confirmed on the dashboard, which is not the
same account that hit the $0.00 balance. Exact model strings come from
docs.coreweave.com/products/inference/serverless/models. Endpoint is
api.inference.wandb.ai/v1/chat/completions, and auth is a Bearer token.
The base URL and tool-call support are not yet verified on this account.
Run /engineer/test-models before relying on it. If the first call 401s,
check that COREWEAVE_API_KEY is an inference-capable token from
console.coreweave.com/tokens.

---

**2026-11-02 - biometrics moved onto the normal provider path**

api/biometrics.py used to make its own direct Gemini HTTP call with a
hardcoded model string, which is why it 404ed twice on retired models
(gemini-2.5-flash, then its hardcoded replacement). It now enters at
GEMINI_MODELS["simple"] (gemini-3.5-flash-lite) and walks GEMINI_FALLBACK
on quota errors exactly like generate(), reusing _call_gemini. Its
multimodal payload is still built locally because generate() only
supports one image part. The walk stops at the openrouter sentinel -
OpenRouter free models cannot see images or hear audio, so verification
returns an honest failure there instead of a fake verdict. biometrics no
longer reads GEMINI_THINKING_MODEL.

## How to use this file going forward

When you remove or demote a model from `provider_registry.py`, move
its inline comment here (under the right provider heading) instead of
leaving it in the registry. Keep the registry itself down to: what's
live, its tier/limits, and *short* one-line "why this tier" notes only.
Anything with a paragraph of forensic detail belongs here.
