"""Every shipped model-free starter card must pass validation for its kind."""
import json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bumparr.card_validation import validate_card

SEEDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "bumparr", "config_files", "card_seeds.json")


class CardSeeds(unittest.TestCase):
    def setUp(self):
        self.data = json.load(open(SEEDS, encoding="utf-8"))

    def test_every_seed_validates(self):
        for kind, seeds in self.data.items():
            if kind.startswith("_"):
                continue
            for i, obj in enumerate(seeds):
                clean, reason = validate_card(kind, obj)
                self.assertIsNotNone(clean, "%s seed %d rejected: %s" % (kind, i, reason))

    def test_preference_tiny_games_have_no_answer(self):
        for obj in self.data.get("tiny_games", []):
            clean, _ = validate_card("tiny_games", obj)
            self.assertEqual(clean.get("answer", ""), "", "preference prompt must not assert an answer")

    def test_all_model_kinds_covered(self):
        for kind in ("psa", "corrections", "coming_up", "achievements", "tiny_games"):
            self.assertTrue(self.data.get(kind), "no starter seeds for %s" % kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
