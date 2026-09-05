"""Regression tests for card_validation, seeded with the REAL defects found in
the live pool on 2026-08-27 (see the-ledger Live Log)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr.card_validation import looks_truncated, validate_card


class TriviaValidation(unittest.TestCase):
    """The repair/reject matrix for multiple-choice trivia."""

    def test_unlabelled_options_are_repaired(self):
        """The exact shape of the broken batch: bare options, bare-letter answer."""
        # The exact shape of the broken batch: bare options, bare-letter answer.
        obj = {"lines": ["What is the capital of Australia?", "Sydney", "Melbourne", "Canberra"],
               "answer": "C"}
        clean, reason = validate_card("trivia", obj)
        self.assertIsNotNone(clean, reason)
        self.assertEqual(clean["lines"][1:], ["A  Sydney", "B  Melbourne", "C  Canberra"])
        self.assertEqual(clean["answer"], "C  Canberra")

    def test_answer_as_option_text_maps_to_letter(self):
        """An answer given as option text (not a letter) resolves to its letter."""
        obj = {"lines": ["Q?", "Sydney", "Canberra"], "answer": "Canberra"}
        clean, _ = validate_card("trivia", obj)
        self.assertEqual(clean["answer"], "B  Canberra")

    def test_already_labelled_is_kept(self):
        """Well-formed labelled input passes through unchanged."""
        obj = {"lines": ["Q?", "A  Sydney", "B  Canberra"], "answer": "B  Canberra"}
        clean, _ = validate_card("trivia", obj)
        self.assertEqual(clean["answer"], "B  Canberra")

    def test_answer_not_in_options_is_rejected(self):
        """An answer that matches no option cannot be repaired, so it is dropped."""
        # The Great Emu War card: answer letter valid but no option is the real
        # answer -> here we model the harder case where answer can't be mapped.
        obj = {"lines": ["Q?", "Sydney", "Melbourne"], "answer": "Z"}
        clean, _ = validate_card("trivia", obj)
        self.assertIsNone(clean)

    def test_self_answering_is_rejected(self):
        """A question that contains its own answer is a defect, not a riddle."""
        obj = {"lines": ["Who painted the Mona Lisa by Da Vinci?", "Da Vinci", "Raphael"],
               "answer": "Da Vinci"}
        clean, _ = validate_card("trivia", obj)
        self.assertIsNone(clean, "self-answering card should be rejected")

    def test_too_few_options_rejected(self):
        """A multiple-choice card with one option is not multiple choice."""
        obj = {"lines": ["Q?", "only one"], "answer": "A"}
        self.assertIsNone(validate_card("trivia", obj)[0])


class NumberValidation(unittest.TestCase):
    """Structural checks for number cards (truth comes from grounded data)."""

    def test_empty_placeholder_rejected(self):
        """The literal "... = ..." card that shipped."""
        # The literal "... = ..." card that shipped.
        self.assertIsNone(validate_card("number", {"number": "...", "meaning": "..."})[0])

    def test_missing_digit_rejected(self):
        """A 'number' with no digit at all is a prose card mislabeled."""
        self.assertIsNone(validate_card("number", {"number": "many", "meaning": "stars"})[0])

    def test_wellformed_number_passes_structurally(self):
        """A structurally sound number passes; source grounding supplies truth."""
        # NOTE: structural validation cannot catch a wrong-but-well-formed number;
        # the grounded source owns that guarantee. This one is structurally fine.
        clean, _ = validate_card("number", {"number": "8,848.86 m", "meaning": "Height of Everest"})
        self.assertIsNotNone(clean)


class TinyGamesValidation(unittest.TestCase):
    """Two-option games: preferences stay open, facts keep their answer."""

    def test_preference_answer_is_stripped(self):
        """'Pizza or Tacos?' has no right answer, so one must not be asserted."""
        clean, _ = validate_card("tiny_games", {"lines": ["Pizza or Tacos?", "Pizza", "Tacos"],
                                                "answer": "Tacos"})
        self.assertEqual(clean["answer"], "", "preference prompt must not assert an answer")

    def test_which_came_first_keeps_answer(self):
        """'Which came first' has a true answer and must not be treated as a preference."""
        clean, _ = validate_card("tiny_games", {"lines": ["Which came first?", "Egg", "Chicken"],
                                                "answer": "Egg"})
        self.assertEqual(clean["answer"], "Egg")


class Truncation(unittest.TestCase):
    """looks_truncated: real source-extract cut shapes fire, clean prose doesn't."""

    def test_real_truncations_detected(self):
        """The actual truncated extracts from the live pool, verbatim."""
        for s in ["...located within Hamilton Township, in Mercer County, in the U.",
                  "...registered under the Non-Governmental Organisations' Act, [Cap.",
                  "...a non-fiction book by American historian Judith C.",
                  "Alijah Huzzie is an American professional football cornerback for the"]:
            self.assertTrue(looks_truncated(s), s)

    def test_complete_sentences_pass(self):
        """No false positives: ordinary complete sentences must not be flagged."""
        for s in ["Carfury is a hamlet in west Cornwall, England, United Kingdom.",
                  "Nuphar spenneriana is an aquatic plant native to Europe.",
                  "The device can beep, chirp, or hum."]:
            self.assertFalse(looks_truncated(s), s)

    def test_fun_facts_truncation_rejected(self):
        """Truncation checking applies to factual prose kinds (fun_facts)..."""
        self.assertIsNone(validate_card("fun_facts", {"lines": ["...Judith C."]})[0])

    def test_comedic_kinds_not_truncation_checked(self):
        """...but not to the comedic kinds, where an abrupt ending is the point."""
        # A PSA may end abruptly on purpose.
        clean, _ = validate_card("psa", {"lines": ["Do not dream", "The walls hear everything"]})
        self.assertIsNotNone(clean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
