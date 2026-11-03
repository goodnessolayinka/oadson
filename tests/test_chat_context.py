"""
tests/test_chat_context.py — real, runnable tests for core/chat_context.py.

Uses stdlib unittest (not pytest) deliberately — zero extra dependency to
install, so `python3 -m unittest tests.test_chat_context -v` just works
on any machine with Python 3 and this repo checked out, including a
fresh Render deploy shell with nothing extra installed.

Each test in the DownloadResultTracking class corresponds directly to
the real production bug this project's structural pass fixed (see
ChatContext.note_skill_result's docstring) — these aren't abstract
"does the dataclass work" tests, they're a regression test FOR that
specific incident: a file generated earlier in a turn getting silently
lost because a later, unrelated skill call in the same turn overwrote
the tracking variable that pointed at it.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chat_context import ChatContext


def make_ctx(**overrides):
    defaults = dict(objective="test objective", user_id="u1", session_id="s1", user={"_id": "u1"})
    defaults.update(overrides)
    return ChatContext(**defaults)


class DictCompatibility(unittest.TestCase):
    """
    ChatContext must be a genuine drop-in replacement for the plain dict
    `ctx` used to be — every existing ctx["x"]/ctx.get("x")/ctx["x"]=y
    call site across orchestrator.py, tool_loop.py, and response_pipeline.py
    depends on this being true. These tests exist specifically because
    that compatibility is load-bearing, not incidental.
    """

    def test_bracket_read_known_field(self):
        ctx = make_ctx()
        self.assertEqual(ctx["session_id"], "s1")

    def test_bracket_write_known_field(self):
        ctx = make_ctx()
        ctx["model_used"] = "gemini-3.6-flash"
        self.assertEqual(ctx.model_used, "gemini-3.6-flash")

    def test_get_with_default(self):
        ctx = make_ctx()
        self.assertIsNone(ctx.get("model_used"))
        self.assertEqual(ctx.get("model_used", "fallback"), "fallback")

    def test_unknown_field_routes_to_extra(self):
        ctx = make_ctx()
        ctx["some_future_field"] = "value"
        self.assertEqual(ctx.get("some_future_field"), "value")
        self.assertIn("some_future_field", ctx)
        self.assertEqual(ctx.extra["some_future_field"], "value")

    def test_contains_known_field(self):
        ctx = make_ctx()
        self.assertNotIn("model_used", ctx)  # None by default
        ctx["model_used"] = "x"
        self.assertIn("model_used", ctx)

    def test_setdefault_known_field(self):
        # Real call site this exists for: core/tool_loop.py's
        # ctx.setdefault("session_state", {})["terminal_cwd"] = ...
        ctx = make_ctx()
        result = ctx.setdefault("session_state", {})
        self.assertEqual(result, {})
        result["terminal_cwd"] = "/home/user"
        self.assertEqual(ctx.session_state["terminal_cwd"], "/home/user")

    def test_setdefault_does_not_overwrite_existing(self):
        ctx = make_ctx()
        ctx["session_state"] = {"already": "set"}
        result = ctx.setdefault("session_state", {"should": "not appear"})
        self.assertEqual(result, {"already": "set"})

    def test_setdefault_unknown_field(self):
        ctx = make_ctx()
        result = ctx.setdefault("brand_new_key", [])
        self.assertEqual(result, [])
        self.assertIs(ctx.extra["brand_new_key"], result)

    def test_keys_includes_set_known_and_extra_fields(self):
        ctx = make_ctx()
        ctx["model_used"] = "x"
        ctx["custom"] = "y"
        keys = ctx.keys()
        self.assertIn("model_used", keys)
        self.assertIn("custom", keys)
        self.assertIn("session_id", keys)  # set at construction


class DownloadResultTracking(unittest.TestCase):
    """
    Direct regression tests for the real production bug: a turn like
    talk -> file_session -> file_generation used to end with
    ctx["skill_output"] holding file_session's result (no download_url),
    silently discarding file_generation's real one from earlier in the
    same turn — because skill_output was reassigned unconditionally on
    EVERY skill call. See ChatContext.note_skill_result's docstring for
    the full incident.
    """

    def test_single_skill_with_download(self):
        ctx = make_ctx()
        ctx.note_skill_result("file_generation", {
            "status": "success", "download_url": "https://x/f.html", "filename": "f.html",
        })
        self.assertEqual(ctx.best_download_result()["filename"], "f.html")

    def test_download_result_survives_later_unrelated_skill_call(self):
        """
        The exact real-world sequence from the incident: file_generation
        succeeds with a download link, then file_session runs afterward
        (no download_url) — best_download_result() must still find the
        first one, not silently return None or the wrong result.
        """
        ctx = make_ctx()
        ctx.note_skill_result("file_generation", {
            "status": "success", "download_url": "https://x/f.html", "filename": "f.html",
        })
        ctx.note_skill_result("file_session", {"status": "success", "data": "session registered"})
        result = ctx.best_download_result()
        self.assertIsNotNone(result, "download link must not be silently lost")
        self.assertEqual(result["filename"], "f.html")

    def test_no_download_result_when_nothing_produced_a_file(self):
        ctx = make_ctx()
        ctx.note_skill_result("web_search", {"status": "success", "data": "some results"})
        ctx.note_skill_result("memory_manager", {"status": "success", "data": "saved"})
        self.assertIsNone(ctx.best_download_result())

    def test_most_recent_download_wins_when_multiple_files_generated(self):
        """
        If the model genuinely generates two files in one turn,
        best_download_result should reflect the LAST download-bearing
        result (skill_output itself, since it still has a download_url)
        — not stay stuck on the first one.
        """
        ctx = make_ctx()
        ctx.note_skill_result("file_generation", {
            "status": "success", "download_url": "https://x/first.html", "filename": "first.html",
        })
        ctx.note_skill_result("file_generation", {
            "status": "success", "download_url": "https://x/second.html", "filename": "second.html",
        })
        self.assertEqual(ctx.best_download_result()["filename"], "second.html")

    def test_error_result_from_skill_does_not_count_as_download(self):
        ctx = make_ctx()
        ctx.note_skill_result("file_generation", {"status": "error", "reason": "missing content"})
        self.assertIsNone(ctx.best_download_result())

    def test_skill_name_and_output_track_most_recent_regardless_of_download(self):
        """
        note_skill_result must still update skill_name/skill_output on
        EVERY call (existing behavior, unrelated code reads "the most
        recent skill result" via these) — only download_result gets the
        special sticky treatment.
        """
        ctx = make_ctx()
        ctx.note_skill_result("file_generation", {
            "status": "success", "download_url": "https://x/f.html", "filename": "f.html",
        })
        ctx.note_skill_result("file_session", {"status": "success", "data": "registered"})
        self.assertEqual(ctx.skill_name, "file_session")
        self.assertEqual(ctx.skill_output["data"], "registered")
        # But download_result still correctly points at the earlier one:
        self.assertEqual(ctx.download_result["filename"], "f.html")


if __name__ == "__main__":
    unittest.main()
