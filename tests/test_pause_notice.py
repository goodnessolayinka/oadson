import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SPEND_LEDGER_PATH", os.path.join(tempfile.mkdtemp(), "ledger.json"))

import asyncio  # noqa: E402

from core import model_policy, spend_tracker  # noqa: E402


def _reset(total_today_usd: float):
    spend_tracker._mirror["total"].clear()
    spend_tracker._mirror["daily"].clear()
    if total_today_usd:
        day = spend_tracker._today()
        spend_tracker._mirror["total"]["anthropic"] = total_today_usd
        spend_tracker._mirror["daily"][day] = {"anthropic": total_today_usd}


def test_no_notice_when_claude_is_available():
    _reset(0.0)
    assert model_policy.pause_notice("anthropic/claude-haiku-5-5") is None


def test_daily_limit_gives_a_plain_notice_with_the_reset_time():
    _reset(spend_tracker.CAPS["anthropic"]["daily"])
    note = model_policy.pause_notice("anthropic/claude-haiku-5-5")
    assert note and "claude-haiku-5-5" in note and "midnight UTC" in note
    assert "Start your reply" in note


def test_lifetime_limit_is_described_differently():
    spend_tracker._mirror["total"].clear()
    spend_tracker._mirror["daily"].clear()
    spend_tracker._mirror["total"]["anthropic"] = spend_tracker.CAPS["anthropic"]["total"]
    note = model_policy.pause_notice("claude-haiku-5-5")
    assert note and "lifetime" in note


def test_free_and_unknown_models_never_get_a_notice():
    _reset(spend_tracker.CAPS["anthropic"]["daily"])
    assert model_policy.pause_notice("gemini-3.5-flash") is None
    assert model_policy.pause_notice("not-a-real-model") is None
    assert model_policy.pause_notice(None) is None
