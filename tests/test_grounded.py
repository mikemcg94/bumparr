"""Regression tests for grounded generators (M2 saturation cap, M8 baseline)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_snippet(code, timeout=90):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "t.db")
        env = dict(os.environ, DB_PATH=db_path, ASSET_ROOT=tmp,
                   PYTHONPATH=REPO)
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return r


SATURATION_CODE = textwrap.dedent("""
    from bumparr import db
    import bumparr.generators.grounded as g
    db.init_db()
    g.time.sleep = lambda s: None
    fact = {"question": "What is the capital of France?",
            "correct_answer": "Paris",
            "incorrect_answers": ["London", "Berlin", "Rome"]}
    g._get_json = lambda url: {"results": [fact]}
    added1 = g.gen_trivia(1)
    assert added1 == 1, added1
    import time as _t
    start = _t.time()
    added2 = g.gen_trivia(2)
    elapsed = _t.time() - start
    assert added2 == 0, added2
    assert elapsed < 20, elapsed
    print("OK saturation")
""")

BASELINE_CODE = textwrap.dedent("""
    from bumparr import db
    from bumparr import ingest
    db.init_db()
    ingest.register_all_baselines()
    with db.conn() as c:
        n1 = c.execute("SELECT COUNT(*) FROM playables WHERE kind='number'").fetchone()[0]
    assert n1 > 0, n1
    ingest.register_all_baselines()
    with db.conn() as c:
        n2 = c.execute("SELECT COUNT(*) FROM playables WHERE kind='number'").fetchone()[0]
    assert n1 == n2, (n1, n2)
    from bumparr.generators.grounded import gen_number
    added = gen_number(5)
    with db.conn() as c:
        n3 = c.execute("SELECT COUNT(*) FROM playables WHERE kind='number'").fetchone()[0]
    assert added == 5 and n3 == n2 + 5, (added, n2, n3)
    print("OK baseline %d==%d expansion=%d" % (n1, n2, added))
""")


class Grounded(unittest.TestCase):
    def test_trivia_saturation_returns_promptly(self):
        r = run_snippet(SATURATION_CODE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("OK saturation", r.stdout)

    def test_double_baseline_idempotent(self):
        r = run_snippet(BASELINE_CODE, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("OK baseline", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
