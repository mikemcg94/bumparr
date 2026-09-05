import unittest

from bumparr.content_filter import is_grim


class GrimBoundaries(unittest.TestCase):
    def test_substring_false_positives_are_not_grim(self):
        for text in ("a screenshot", "an executive", "a happy couple",
                     "afraid of spiders", "an eggshell", "a grape",
                     "a local patriot"):
            self.assertFalse(is_grim(text), text)

    def test_intended_forms_are_grim(self):
        for text in ("the prisoner was executed", "a bombing raid", "the war ended"):
            self.assertTrue(is_grim(text), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
