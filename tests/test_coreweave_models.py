"""
CoreWeave fallback models and the engineer's one-model test.

No network: provider calls are replaced with fakes.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SPEND_LEDGER_PATH", os.path.join(tempfile.mkdtemp(), "ledger.json"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from core import model_policy, model_tester, spend_tracker  # noqa: E402
from core.provider_registry import PROVIDERS, get_provider  # noqa: E402


def _reset_spend():
    spend_tracker._mirror["total"].clear()
    spend_tracker._mirror["daily"].clear()


def test_coreweave_sits_between_free_providers_and_claude():
    names = [p["name"] for p in PROVIDERS]
    assert names.index("coreweave") < names.index("anthropic")
    assert names[-1] == "anthropic"  # Claude stays last


def test_coreweave_models_are_the_exact_api_ids():
    p = get_provider("coreweave")
    assert p["paid"] is True
    assert p["protocol"] == "openai"
    assert p["api_key_env"] == "COREWEAVE_API_KEY"
    ids = [m["id"] for m in p["models"]]
    assert ids == [
        "deepseek-ai/DeepSeek-V4.1-Flash",
        "zai-org/GLM-5.3-Flash",
        "openai/gpt-oss-20b",
        "Qwen/Qwen3.6-35B-A3B",
        "meta-llama/Llama-3.3-70B-Instruct",
        "meta-llama/Llama-3.1-8B-Instruct",
    ]


def test_llama_31_is_priced_at_the_listed_rate():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert round(spend_tracker.cost_usd("coreweave", "meta-llama/Llama-3.1-8B-Instruct", usage), 4) == 0.44


def test_new_coreweave_models_are_priced_at_the_listed_rates():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert round(spend_tracker.cost_usd("coreweave", "openai/gpt-oss-20b", usage), 4) == 0.16
    assert round(spend_tracker.cost_usd("coreweave", "Qwen/Qwen3.6-35B-A3B", usage), 4) == 1.50
    assert round(spend_tracker.cost_usd("coreweave", "meta-llama/Llama-3.3-70B-Instruct", usage), 4) == 1.42


def test_coreweave_prices_match_the_listed_rates():
    # $0.20 in + $0.65 out per 1M for DeepSeek V4.1 Flash
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert round(spend_tracker.cost_usd("coreweave", "deepseek-ai/DeepSeek-V4.1-Flash", usage), 4) == 0.85
    # $0.15 in + $0.50 out per 1M for GLM 5.3 Flash
    assert round(spend_tracker.cost_usd("coreweave", "zai-org/GLM-5.3-Flash", usage), 4) == 0.65


def test_unknown_coreweave_model_uses_the_conservative_fallback_price():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert spend_tracker.cost_usd("coreweave", "some/new-model", usage) == spend_tracker._FALLBACK_PRICE[0]


def test_coreweave_has_its_own_spend_caps():
    caps = spend_tracker.CAPS["coreweave"]
    assert caps["total"] <= 50.0  # never more than the credit
    assert caps["daily"] > 0


def test_pause_notice_matches_ids_that_contain_a_slash():
    _reset_spend()
    spend_tracker._mirror["total"]["coreweave"] = spend_tracker.CAPS["coreweave"]["total"]
    note = model_policy.pause_notice("coreweave/zai-org/GLM-5.3-Flash")
    assert note and "zai-org/GLM-5.3-Flash" in note and "lifetime" in note
    note_bare = model_policy.pause_notice("zai-org/GLM-5.3-Flash")
    assert note_bare and "GLM-5.3-Flash" in note_bare
    _reset_spend()


def test_engineer_test_runs_only_the_selected_model(monkeypatch):
    calls = []

    async def fake_call(provider, model_id, messages, tools):
        calls.append(f"{provider['name']}/{model_id}")
        return {"text": "OK", "tool_call": None}

    monkeypatch.setattr(model_tester, "_call_openai_compatible", fake_call)
    result = asyncio.run(model_tester.test_models())
    assert calls == ["coreweave/deepseek-ai/DeepSeek-V4.1-Flash"]
    assert result["tested"] == 1 and result["passed"] == 1


def test_engineer_test_can_still_name_another_model(monkeypatch):
    calls = []

    async def fake_call(provider, model_id, messages, tools):
        calls.append(f"{provider['name']}/{model_id}")
        return {"text": "OK", "tool_call": None}

    monkeypatch.setattr(model_tester, "_call_openai_compatible", fake_call)
    result = asyncio.run(model_tester.test_models(["coreweave/zai-org/GLM-5.3-Flash"]))
    assert calls == ["coreweave/zai-org/GLM-5.3-Flash"]
    assert result["tested"] == 1


def test_engineer_test_reports_a_failure_without_crashing(monkeypatch):
    async def boom(provider, model_id, messages, tools):
        raise RuntimeError("COREWEAVE_API_KEY not set")

    monkeypatch.setattr(model_tester, "_call_openai_compatible", boom)
    result = asyncio.run(model_tester.test_models())
    assert result["status"] == "fail" and result["failed"] == 1
    assert "COREWEAVE_API_KEY" in result["results"][0]["error"]


def test_coreweave_reply_cap_is_20k_and_groq_keeps_the_default():
    from core.provider_router import OPENAI_COMPAT_MAX_TOKENS, _max_tokens_for
    assert _max_tokens_for(get_provider("coreweave")) == 20000
    assert _max_tokens_for(get_provider("groq")) == OPENAI_COMPAT_MAX_TOKENS == 8192


def test_second_coreweave_key_is_used_when_the_first_fails(monkeypatch):
    from core import provider_router
    monkeypatch.setenv("COREWEAVE_API_KEY", "key-one")
    monkeypatch.setenv("COREWEAVE_API_KEY_2", "key-two")
    used = []

    async def fake_once(provider, model_id, messages, tools, api_key_override=None, spend_name=None):
        used.append(api_key_override)
        if api_key_override == "key-one":
            raise provider_router.QuotaExceeded("key one is out of credit")
        return {"text": "OK", "tool_call": None}

    monkeypatch.setattr(provider_router, "_call_openai_compatible_once", fake_once)
    result = asyncio.run(provider_router._call_openai_compatible(
        get_provider("coreweave"), "openai/gpt-oss-20b", [], None))
    assert used == ["key-one", "key-two"] and result["text"] == "OK"


def test_no_coreweave_key_set_is_an_error(monkeypatch):
    from core import provider_router
    monkeypatch.delenv("COREWEAVE_API_KEY", raising=False)
    monkeypatch.delenv("COREWEAVE_API_KEY_2", raising=False)
    with pytest.raises(RuntimeError, match="none of"):
        asyncio.run(provider_router._call_openai_compatible(
            get_provider("coreweave"), "openai/gpt-oss-20b", [], None))


def test_capped_first_key_is_skipped_and_the_backup_key_answers(monkeypatch):
    """Real spend gate, real ledger, only the HTTP call is faked."""
    import httpx
    from core import provider_router
    monkeypatch.setenv("COREWEAVE_API_KEY", "key-one")
    monkeypatch.setenv("COREWEAVE_API_KEY_2", "key-two")
    _reset_spend()
    spend_tracker._mirror["daily"][spend_tracker._today()] = {"coreweave": spend_tracker.CAPS["coreweave"]["daily"]}
    posted_with = []

    class _Res:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            posted_with.append(headers.get("Authorization"))
            return _Res()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    asyncio.run(provider_router._call_openai_compatible(
        get_provider("coreweave"), "openai/gpt-oss-20b", [{"role": "user", "content": "hi"}], None))
    # key one was refused by its own cap before any request left; key two made the one call
    assert posted_with == ["Bearer key-two"]
    total_2, today_2 = spend_tracker.spent("coreweave_2")
    assert today_2 > 0  # the call was recorded on the backup key's own ledger
    assert spend_tracker.spent("coreweave")[1] == spend_tracker.CAPS["coreweave"]["daily"]  # untouched
    _reset_spend()


def test_the_spend_gate_refuses_a_key_over_its_own_cap(monkeypatch):
    from core import provider_router
    spend_tracker._mirror["total"].clear()
    spend_tracker._mirror["daily"].clear()
    spend_tracker._mirror["daily"][spend_tracker._today()] = {"coreweave": spend_tracker.CAPS["coreweave"]["daily"]}
    with pytest.raises(provider_router.QuotaExceeded, match="coreweave: spend cap reached"):
        asyncio.run(provider_router._call_openai_compatible_once(
            get_provider("coreweave"), "openai/gpt-oss-20b", [], None,
            api_key_override="key-one", spend_name="coreweave"))
    spend_tracker._mirror["daily"].clear()
