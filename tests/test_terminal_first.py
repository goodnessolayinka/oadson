"""
Terminal turns try GPT OSS 20B first, then DeepSeek, then Llama 3.1 8B,
all on CoreWeave. Gemini is only reached after all three fail.
No network: the provider call is replaced with a fake.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SPEND_LEDGER_PATH", os.path.join(tempfile.mkdtemp(), "ledger.json"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from core import provider_router  # noqa: E402
from core.provider_registry import get_provider  # noqa: E402
from core.provider_router import TERMINAL_FIRST_MODELS  # noqa: E402

CONTENTS = [{"role": "user", "parts": [{"text": "list the files"}]}]
OK = {"text": None, "tool_call": {"name": "run_bash", "args": {"command": "ls"}}}


def _fake(failing: set, calls: list):
    async def fake_call(provider, model_id, messages, tools):
        calls.append(model_id)
        if model_id in failing:
            raise RuntimeError(f"{model_id} is down")
        return dict(OK)
    return fake_call


def test_the_order_is_gpt_oss_then_deepseek_then_llama_31():
    assert TERMINAL_FIRST_MODELS == [
        "openai/gpt-oss-20b",
        "deepseek-ai/DeepSeek-V4.1-Flash",
        "meta-llama/Llama-3.1-8B-Instruct",
    ]


def test_gpt_oss_answers_first_and_the_rest_are_not_called(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_router, "_call_openai_compatible", _fake(set(), calls))
    result = asyncio.run(provider_router.ProviderRouter().decide_terminal_first("sys", CONTENTS, [], needs_tools=False))
    assert calls == ["openai/gpt-oss-20b"]
    assert result["model_used"] == "coreweave/openai/gpt-oss-20b"
    assert result["protocol"] == "openai" and result["openai_messages"]


def test_if_gpt_oss_fails_deepseek_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_router, "_call_openai_compatible",
                        _fake({"openai/gpt-oss-20b"}, calls))
    result = asyncio.run(provider_router.ProviderRouter().decide_terminal_first("sys", CONTENTS, [], needs_tools=False))
    assert calls == ["openai/gpt-oss-20b", "deepseek-ai/DeepSeek-V4.1-Flash"]
    assert result["model_used"] == "coreweave/deepseek-ai/DeepSeek-V4.1-Flash"


def test_if_gpt_oss_and_deepseek_fail_llama_31_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_router, "_call_openai_compatible",
                        _fake({"openai/gpt-oss-20b", "deepseek-ai/DeepSeek-V4.1-Flash"}, calls))
    result = asyncio.run(provider_router.ProviderRouter().decide_terminal_first("sys", CONTENTS, [], needs_tools=False))
    assert calls[-1] == "meta-llama/Llama-3.1-8B-Instruct"
    assert result["model_used"] == "coreweave/meta-llama/Llama-3.1-8B-Instruct"


def test_if_all_three_fail_it_raises_so_the_caller_uses_gemini(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_router, "_call_openai_compatible",
                        _fake(set(TERMINAL_FIRST_MODELS), calls))
    with pytest.raises(RuntimeError, match="every model failed"):
        asyncio.run(provider_router.ProviderRouter().decide_terminal_first("sys", CONTENTS, [], needs_tools=False))
    assert calls == TERMINAL_FIRST_MODELS


def test_mid_turn_retry_reuses_the_current_openai_messages(monkeypatch):
    seen = []

    async def fake_call(provider, model_id, messages, tools):
        seen.append(messages)
        return dict(OK)

    monkeypatch.setattr(provider_router, "_call_openai_compatible", fake_call)
    current = [{"role": "user", "content": "list the files"},
               {"role": "assistant", "content": "ok"}]
    result = asyncio.run(provider_router.ProviderRouter().decide_terminal_first(
        "sys", [], [], needs_tools=False, openai_messages=current))
    assert seen[0] == current
    assert result["openai_messages"] == current


def test_every_terminal_first_model_is_in_the_coreweave_registry():
    ids = [m["id"] for m in get_provider("coreweave")["models"]]
    for model_id in TERMINAL_FIRST_MODELS:
        assert model_id in ids
