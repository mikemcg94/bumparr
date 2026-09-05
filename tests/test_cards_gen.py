"""Regression tests for cards.generate per-item survival (M3)."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr.card_validation import validate_card
from bumparr.generators import cards

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIXED_CODE = textwrap.dedent("""
    import json
    from bumparr import db
    from bumparr.generators import cards
    db.init_db()
    bad = {"lines": ["Too many?", "one", "two", "three", "four", "five", "six", "seven"],
           "answer": "one"}
    good = {"lines": ["Which city is the capital of France?", "Paris", "London"],
            "answer": "Paris"}
    cards.PROMPTS["trivia"] = "make {n} test cards"
    cards._call_model = lambda prompt, **kw: json.dumps([bad, good])
    added, rejected = cards.generate("trivia", 2)
    assert added == 1, (added, rejected)
    assert rejected == 1, (added, rejected)
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM playables WHERE kind='trivia'").fetchone()[0]
    assert n == 1, n
    print("OK mixed")
""")


def run_snippet(code, timeout=90):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "t.db")
        env = dict(os.environ, DB_PATH=db_path, ASSET_ROOT=tmp,
                   PYTHONPATH=REPO)
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return r


class CardsGen(unittest.TestCase):
    def test_seven_options_rejected_no_raise(self):
        unlabelled = {"lines": ["Q?", "o1", "o2", "o3", "o4", "o5", "o6", "o7"],
                      "answer": "A"}
        clean, reason = validate_card("trivia", unlabelled)
        self.assertIsNone(clean, reason)
        labelled = {"lines": ["Q?", "A  o1", "B  o2", "C  o3", "D  o4",
                              "E  o5", "F  o6", "G  o7"],
                    "answer": "A  o1"}
        clean2, reason2 = validate_card("trivia", labelled)
        self.assertIsNone(clean2, reason2)

    def test_mixed_batch_inserts_good_one(self):
        r = run_snippet(MIXED_CODE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("OK mixed", r.stdout)

    def test_database_error_is_not_misreported_as_model_rejection(self):
        good = '[{"lines":["Still Awake","You are still here."]}]'

        @contextmanager
        def broken_conn():
            class Broken:
                def execute(self, *args, **kwargs):
                    raise sqlite3.OperationalError("database unavailable")
            yield Broken()

        with mock.patch.object(cards, "_call_model", return_value=good), \
                mock.patch.object(cards.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                cards.generate("achievements", 1)

    def test_database_error_rolls_back_earlier_items(self):
        code = textwrap.dedent("""
            import contextlib, json, sqlite3
            from bumparr import db
            from bumparr.generators import cards
            db.init_db()
            cards._call_model = lambda prompt, **kw: json.dumps([
                {"lines": ["First", "Good item"]},
                {"lines": ["Second", "Also good"]},
            ])
            original = db.conn
            @contextlib.contextmanager
            def fail_second_insert():
                with original() as connection:
                    class Proxy:
                        inserts = 0
                        def execute(self, sql, args=()):
                            if sql.lstrip().startswith("INSERT"):
                                self.inserts += 1
                                if self.inserts == 2:
                                    raise sqlite3.OperationalError("forced second insert")
                            return connection.execute(sql, args)
                        def commit(self):
                            return connection.commit()
                    yield Proxy()
            cards.db.conn = fail_second_insert
            try:
                cards.generate("achievements", 2)
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("database error did not propagate")
            with original() as connection:
                count = connection.execute("SELECT COUNT(*) FROM playables").fetchone()[0]
            assert count == 0, count
            print("OK rollback")
        """)
        result = run_snippet(code, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertIn("OK rollback", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
