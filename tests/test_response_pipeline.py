"""
tests/test_response_pipeline.py — real, runnable tests for
core/response_pipeline.py's ResponsePipeline._trim_steps_for_persistence.

Run: python3 -m unittest tests.test_response_pipeline -v

Only this one method is tested here (not _review/_validate/_persist_memory/
_finalize) because it's the only one of the five that's genuinely pure —
no self.orchestrator.bus/db/provider dependency, just a list-in/list-out
transform. Constructing ResponsePipeline(orchestrator=None) is safe
specifically for this method since it never touches self.orchestrator.
Testing the other four honestly requires mocking Mongo/the event bus/the
Gemini client, which is exactly the kind of integration surface this
project's post-deploy checklist (not a unit test) is meant to cover —
see this project's overall architecture notes.

This is a direct regression test for a real fixed bug: a skill's actual
result data (e.g. a download link) used to only be visible live during
streaming and get lost entirely on reload, because only a short status
summary was persisted, not the real result payload.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.response_pipeline import ResponsePipeline


def make_pipeline():
    # Safe here specifically because _trim_steps_for_persistence never
    # touches self.orchestrator — see module docstring above.
    return ResponsePipeline(orchestrator=None)


class TrimStepsForPersistence(unittest.TestCase):

    def test_download_url_and_filename_survive_trimming(self):
        """
        Direct regression test: a file_generation result with a real
        download_url used to be reduced to just a status line on
        persistence — download_url/filename must survive into the
        trimmed entry.
        """
        pipeline = make_pipeline()
        steps = [{
            "type": "skill",
            "skill": "file_generation",
            "message": "file_generation completed successfully.",
            "time": "2026-08-21T00:00:00Z",
            "args": {"file_type": "html", "filename": "test.html"},
            "result": {
                "status": "success",
                "download_url": "https://example.com/files/test.html",
                "filename": "test.html",
                "message": "File ready",
            },
        }]
        trimmed = pipeline._trim_steps_for_persistence(steps)
        self.assertEqual(len(trimmed), 1)
        self.assertEqual(trimmed[0]["download_url"], "https://example.com/files/test.html")
        self.assertEqual(trimmed[0]["filename"], "test.html")

    def test_result_data_is_persisted_not_just_status(self):
        """
        A skill returning real data (e.g. {"response": "..."} from a
        joke-fetching skill) should have that data visible on reload,
        not just its bare status/message.
        """
        pipeline = make_pipeline()
        steps = [{
            "type": "skill",
            "skill": "dad_joke_fetcher",
            "message": "done",
            "time": "2026-08-21T00:00:00Z",
            "args": {},
            "result": {"status": "success", "response": "Why did the chicken..."},
        }]
        trimmed = pipeline._trim_steps_for_persistence(steps)
        self.assertIn("result_data", trimmed[0])
        self.assertIn("Why did the chicken", trimmed[0]["result_data"])

    def test_talk_steps_get_generous_char_cap_not_the_tight_one(self):
        """
        Trace Mode's "talk" steps carry real prose the user is meant to
        read back later, not a truncatable status line — must use
        MAX_RESULT_DATA_CHARS (1500), not MAX_FIELD_CHARS (300).
        """
        pipeline = make_pipeline()
        long_talk = "This is a sentence. " * 30  # ~600 chars, over 300, under 1500
        steps = [{"type": "talk", "message": long_talk, "time": "2026-08-21T00:00:00Z"}]
        trimmed = pipeline._trim_steps_for_persistence(steps)
        self.assertEqual(trimmed[0]["message"], long_talk[:ResponsePipeline.MAX_RESULT_DATA_CHARS])
        self.assertGreater(len(trimmed[0]["message"]), ResponsePipeline.MAX_FIELD_CHARS)

    def test_only_keeps_most_recent_max_persisted_steps(self):
        pipeline = make_pipeline()
        steps = [
            {"type": "skill", "skill": f"skill_{i}", "message": f"step {i}", "time": "t", "args": {}, "result": {"status": "success"}}
            for i in range(50)
        ]
        trimmed = pipeline._trim_steps_for_persistence(steps)
        self.assertEqual(len(trimmed), ResponsePipeline.MAX_PERSISTED_STEPS)
        # Should keep the LAST 30, not the first 30
        self.assertEqual(trimmed[-1]["skill"], "skill_49")
        self.assertEqual(trimmed[0]["skill"], "skill_20")

    def test_non_skill_step_has_no_skill_specific_fields(self):
        pipeline = make_pipeline()
        steps = [{"type": "review", "message": "review passed", "time": "t"}]
        trimmed = pipeline._trim_steps_for_persistence(steps)
        self.assertNotIn("skill", trimmed[0])
        self.assertNotIn("download_url", trimmed[0])

    def test_empty_steps_list(self):
        pipeline = make_pipeline()
        self.assertEqual(pipeline._trim_steps_for_persistence([]), [])


if __name__ == "__main__":
    unittest.main()
