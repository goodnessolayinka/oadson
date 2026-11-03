"""
tests/test_task_state.py — real, runnable tests for core/task_state.py's
is_task_shaped() and extract_task_state_for_turn().

Run: python3 -m unittest tests.test_task_state -v

The TaskShapedGate class is a direct regression suite for the real
production bug is_task_shaped() was written to fix — see that function's
own docstring in core/task_state.py for the full incident (ordinary
questions and symptom descriptions like "I have stomach ache what should
I do" were getting wrapped as unresolvable tracked "requirements" that
could never complete, producing a confusing caveat on every subsequent
turn in the session).
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.task_state import is_task_shaped, extract_task_state_for_turn


class TaskShapedGate(unittest.TestCase):
    """
    Every case in this class corresponds to something that actually
    happened in production and broke — see is_task_shaped's docstring.
    """

    def test_symptom_description_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped("I have stomach ache what should I do"))

    def test_short_correction_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped("Use news skill instead"))

    def test_commentary_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped(
            "Now you're saying the truth... Took you a 1172 insertion push"
        ))

    def test_plain_question_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped("What is the capital of France?"))
        self.assertFalse(is_task_shaped("How does this work?"))
        self.assertFalse(is_task_shaped("Why is this happening?"))

    def test_greeting_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped("hey"))
        self.assertFalse(is_task_shaped("good morning"))

    def test_empty_string_is_not_task_shaped(self):
        self.assertFalse(is_task_shaped(""))
        self.assertFalse(is_task_shaped("   "))

    def test_explicit_build_request_is_task_shaped(self):
        self.assertTrue(is_task_shaped("Build a login page with email and password fields"))

    def test_explicit_fix_request_is_task_shaped(self):
        self.assertTrue(is_task_shaped("Fix the bug in the authentication module"))

    def test_multi_step_enumerated_task_is_task_shaped(self):
        self.assertTrue(is_task_shaped(
            "Build a login page, extract the user data, store it in the DB, and return a token"
        ))

    def test_conjunctive_list_with_action_verb_is_task_shaped(self):
        self.assertTrue(is_task_shaped(
            "Create the schema, add validation, and deploy the migration"
        ))

    def test_plain_conjunctive_list_without_action_verb_is_not_task_shaped(self):
        """
        A comma/and list alone isn't enough — "I like apples, oranges,
        and pears" has list structure but no real action verb, and
        should not be tracked as a task.
        """
        self.assertFalse(is_task_shaped("I like apples, oranges, and pears"))


class ExtractTaskStateForTurn(unittest.TestCase):
    """
    Tests the full wrapper function (gate + extraction + non-blocking
    error handling), not just the gate in isolation.
    """

    def test_non_task_shaped_objective_returns_none(self):
        result = extract_task_state_for_turn("I have stomach ache what should I do", "sess1")
        self.assertIsNone(result)

    def test_task_shaped_objective_returns_real_task_state(self):
        result = extract_task_state_for_turn("Build a login page and add validation", "sess2")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result.requirements), 1)

    def test_task_state_carries_session_id(self):
        result = extract_task_state_for_turn("Fix the broken CSS on the homepage", "sess3")
        self.assertEqual(result.session_id, "sess3")

    def test_multi_requirement_extraction(self):
        result = extract_task_state_for_turn(
            "1. Add a login form\n2. Add a signup form\n3. Add password reset",
            "sess4",
        )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result.requirements), 2)

    def test_never_raises_on_pathological_input(self):
        """
        Non-blocking by design — a bug in extraction must never prevent
        the actual turn from running. This isn't testing a specific
        failure mode (none is known), it's asserting the contract that
        no input, however strange, should propagate an exception out of
        this function.
        """
        pathological_inputs = [
            "a" * 100000,  # very long
            "Build " + ("x, " * 5000),  # pathological list structure
            "\x00\x01\x02 build something \x03",  # control characters
        ]
        for text in pathological_inputs:
            try:
                extract_task_state_for_turn(text, "sess_stress")
            except Exception as e:
                self.fail(f"extract_task_state_for_turn raised on pathological input: {e}")


if __name__ == "__main__":
    unittest.main()
