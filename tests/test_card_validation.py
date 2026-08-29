"""Regression tests for card_validation, seeded with the REAL defects found in
the live pool on 2026-08-27 (see the-ledger Live Log)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr.card_validation import validate_card, verify_fact, looks_truncated


class TriviaValidation(unittest.TestCase):
    def test_unlabelled_options_are_repaired(self):
        # The exact shape of the broken batch: bare options, bare-letter answer.
        obj = {"lines": ["What is the capital of Australia?", "Sydney", "Melbourne", "Canberra"],
               "answer": "C"}
        clean, reason = validate_card("trivia", obj)
        self.assertIsNotNone(clean, reason)
        self.assertEqual(clean["lines"][1:], ["A  Sydney", "B  Melbourne", "C  Canberra"])
        self.assertEqual(clean["answer"], "C  Canberra")

    def test_answer_as_option_text_maps_to_letter(self):
        obj = {"lines": ["Q?", "Sydney", "Canberra"], "answer": "Canberra"}
        clean, _ = validate_card("trivia", obj)
        self.assertEqual(clean["answer"], "B  Canberra")

    def test_already_labelled_is_kept(self):
        obj = {"lines": ["Q?", "A  Sydney", "B  Canberra"], "answer": "B  Canberra"}
        clean, _ = validate_card("trivia", obj)
        self.assertEqual(clean["answer"], "B  Canberra")

    def test_answer_not_in_options_is_rejected(self):
        # The Great Emu War card: answer letter valid but no option is the real
        # answer -> here we model the harder case where answer can't be mapped.
        obj = {"lines": ["Q?", "Sydney", "Melbourne"], "answer": "Z"}
        clean, reason = validate_card("trivia", obj)
        self.assertIsNone(clean)

    def test_self_answering_is_rejected(self):
        obj = {"lines": ["Who painted the Mona Lisa by Da Vinci?", "Da Vinci", "Raphael"],
               "answer": "Da Vinci"}
        clean, reason = validate_card("trivia", obj)
        self.assertIsNone(clean, "self-answering card should be rejected")

    def test_too_few_options_rejected(self):
        obj = {"lines": ["Q?", "only one"], "answer": "A"}
        self.assertIsNone(validate_card("trivia", obj)[0])


class NumberValidation(unittest.TestCase):
    def test_empty_placeholder_rejected(self):
        # The literal "... = ..." card that shipped.
        self.assertIsNone(validate_card("number", {"number": "...", "meaning": "..."})[0])

    def test_missing_digit_rejected(self):
        self.assertIsNone(validate_card("number", {"number": "many", "meaning": "stars"})[0])

    def test_wellformed_number_passes_structurally(self):
        # NOTE: structural validation cannot catch a wrong-but-well-formed number;
        # that is verify_fact's job. This one is structurally fine.
        clean, _ = validate_card("number", {"number": "8,848.86 m", "meaning": "Height of Everest"})
        self.assertIsNotNone(clean)


class TinyGamesValidation(unittest.TestCase):
    def test_preference_answer_is_stripped(self):
        clean, _ = validate_card("tiny_games", {"lines": ["Pizza or Tacos?", "Pizza", "Tacos"],
                                                "answer": "Tacos"})
        self.assertEqual(clean["answer"], "", "preference prompt must not assert an answer")

    def test_which_came_first_keeps_answer(self):
        clean, _ = validate_card("tiny_games", {"lines": ["Which came first?", "Egg", "Chicken"],
                                                "answer": "Egg"})
        self.assertEqual(clean["answer"], "Egg")


class Truncation(unittest.TestCase):
    def test_real_truncations_detected(self):
        for s in ["...community of Blaenrheidol, Ceredigion, Wales, which is 69.",
                  "...located within Hamilton Township, in Mercer County, in the U.",
                  "...registered under the Non-Governmental Organisations' Act, [Cap.",
                  "...a non-fiction book by American historian Judith C.",
                  "Alijah Huzzie is an American professional football cornerback for the"]:
            self.assertTrue(looks_truncated(s), s)

    def test_complete_sentences_pass(self):
        for s in ["Carfury is a hamlet in west Cornwall, England, United Kingdom.",
                  "Nuphar spenneriana is an aquatic plant native to Europe.",
                  "The device can beep, chirp, or hum."]:
            self.assertFalse(looks_truncated(s), s)

    def test_fun_facts_truncation_rejected(self):
        self.assertIsNone(validate_card("fun_facts", {"lines": ["...Judith C."]})[0])

    def test_comedic_kinds_not_truncation_checked(self):
        # A PSA may end abruptly on purpose.
        clean, _ = validate_card("psa", {"lines": ["Do not dream", "The walls hear everything"]})
        self.assertIsNotNone(clean)


class VerifyFact(unittest.TestCase):
    def test_number_verify_rejects_absurd(self):
        # Stub model returns NO, as the 9B did for "speed of light = 78 mph".
        ok, _ = verify_fact("number", {"number": "78.1 mph", "meaning": "speed of light in water"},
                            lambda p: "NO")
        self.assertFalse(ok)

    def test_number_verify_accepts_true(self):
        ok, _ = verify_fact("number", {"number": "8,848 m", "meaning": "Everest"}, lambda p: "YES")
        self.assertTrue(ok)

    def test_verify_fails_closed_on_garbage(self):
        ok, _ = verify_fact("number", {"number": "1", "meaning": "x"}, lambda p: "maybe?")
        self.assertFalse(ok, "unclear response must fail closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
