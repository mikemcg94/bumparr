"""Every shipped model-free starter card must pass validation for its kind."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bumparr.card_validation import validate_card

SEEDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "bumparr", "config_files", "card_seeds.json")


class CardSeeds(unittest.TestCase):
    """The shipped model-free starter cards are the no-model install's entire
    card floor, so a broken seed means a broken install, not a flaky test."""

    def setUp(self):
        """Load the shipped seed file under test."""
        with open(SEEDS, encoding="utf-8") as f:
            self.data = json.load(f)

    def test_every_seed_validates(self):
        """Every seed, in every kind, must survive validate_card."""
        for kind, seeds in self.data.items():
            if kind.startswith("_"):
                continue
            for i, obj in enumerate(seeds):
                clean, reason = validate_card(kind, obj)
                self.assertIsNotNone(clean, "%s seed %d rejected: %s" % (kind, i, reason))

    def test_preference_tiny_games_have_no_answer(self):
        """Either/or preference seeds must air as open prompts (no answer)."""
        for obj in self.data.get("tiny_games", []):
            clean, _ = validate_card("tiny_games", obj)
            self.assertEqual(clean.get("answer", ""), "", "preference prompt must not assert an answer")

    def test_all_model_kinds_covered(self):
        """Every model-generated kind must have a starter set, or a no-model
        install would be empty in that kind (the original v0.1.0 bug)."""
        for kind in ("psa", "corrections", "coming_up", "achievements", "tiny_games"):
            self.assertTrue(self.data.get(kind), "no starter seeds for %s" % kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
