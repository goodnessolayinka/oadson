import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.failure_explainer import describe_call, explain_failure  # noqa: E402


def test_sandbox_block_points_to_the_right_path():
    msg = explain_failure({"status": "error", "reason": "Blocked: path '../x' is outside the sandbox."})
    assert "repos/owner__repo" in msg


def test_timeouts_rate_limits_and_credentials_each_say_the_next_step():
    assert "time limit" in explain_failure({"status": "timeout", "reason": ""})
    assert "Wait" in explain_failure({"status": "error", "reason": "429 quota exceeded"})
    assert "owner needs" in explain_failure({"status": "error", "reason": "GITHUB_TOKEN not set"})


def test_unknown_failures_still_get_a_sentence():
    msg = explain_failure({"status": "error", "reason": "something odd"})
    assert msg and "Read the reason" in msg
    assert explain_failure(None)


def test_call_summaries_are_plain():
    assert describe_call("terminal", {"command": "ls repos/"}) == "Run command: ls repos/"
    assert describe_call("github", {"action": "push_workspace", "owner": "o", "repo": "r"}) \
        == "Commit and push workspace changes to o/r"
    assert describe_call("github", {"action": "clone_to_workspace", "owner": "o", "repo": "r"}) \
        == "Clone o/r into the workspace"
