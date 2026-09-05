"""Extra validation tuning tests (Phase 4.3 self-answer + dangling number)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr.card_validation import looks_truncated, validate_card


class SelfAnswerTuning(unittest.TestCase):
    def test_comparison_does_not_self_answer_paris(self):
        obj = {"lines": ["Which city is often used in comparison studies?",
                         "Paris", "London"],
               "answer": "Paris"}
        clean, reason = validate_card("trivia", obj)
        self.assertIsNotNone(clean, reason)

    def test_long_whole_word_self_answer_still_rejects(self):
        obj = {"lines": ["Which city is Canberra the capital of?",
                         "Sydney", "Canberra"],
               "answer": "Canberra"}
        clean, _ = validate_card("trivia", obj)
        self.assertIsNone(clean)

    def test_answer_ending_in_punctuation_is_still_detected(self):
        clean, _ = validate_card(
            "trivia",
            {"lines": ["Which runtime is Node.js?", "Node.js", "Python"],
             "answer": "Node.js"},
        )
        self.assertIsNone(clean)


class DanglingNumberTuning(unittest.TestCase):
    def test_long_fact_ending_in_number_passes(self):
        fact = ("The observatory catalog lists twelve new moons this year and "
                "the running total of confirmed objects which is 42.")
        self.assertGreater(len(fact), 70)
        self.assertFalse(looks_truncated(fact), fact)
        clean, reason = validate_card("fun_facts", {"lines": [fact]})
        self.assertIsNotNone(clean, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
