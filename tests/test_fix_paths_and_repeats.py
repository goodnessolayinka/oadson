import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from skills.terminal import skill as term  # noqa: E402
from core.tool_loop import repeat_failure_state  # noqa: E402


def test_stray_quotes_are_stripped_from_paths():
    assert term.clean_path_arg("../repo/app/x.py'") == "../repo/app/x.py"
    assert term.clean_path_arg('"repos/a__b/readme.md"') == "repos/a__b/readme.md"
    assert term.clean_path_arg("  plain.txt ") == "plain.txt"


def test_sibling_repo_paths_get_the_right_hint():
    assert "repos/owner__repo" in term.repo_path_hint("../example-app__backend_v2/app.py")
    assert term.repo_path_hint("/tmp/x") == ""


def test_repeat_streak_counts_identical_failures_only():
    key, streak = repeat_failure_state(None, 0, "terminal", {"c": "ls"}, True)
    assert streak == 1
    key, streak = repeat_failure_state(key, streak, "terminal", {"c": "ls"}, True)
    assert streak == 2
    key, streak = repeat_failure_state(key, streak, "terminal", {"c": "pwd"}, True)
    assert streak == 1                      # different call: streak restarts
    key, streak = repeat_failure_state(key, streak, "terminal", {"c": "pwd"}, False)
    assert (key, streak) == (None, 0)       # success clears it


def test_pwa_has_the_same_step_limit_as_web():
    src = open(os.path.join(ROOT, "core", "orchestrator.py")).read()
    m = {k: int(v) for k, v in re.findall(r'"(terminal|web|app|pwa)":\s+(\d+)', src)}
    assert m["pwa"] == m["web"] == m["app"] == 40
