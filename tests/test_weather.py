"""Regression tests for weather refresh preservation (M4)."""
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


FIRST_INSERT_CODE = textwrap.dedent("""
    from bumparr import db
    import bumparr.generators.weather as w
    db.init_db()
    def fake_gj(url):
        if "geocoding" in url:
            return {"results": [{"name": "Testville", "admin1": "Testland",
                                 "latitude": 1.0, "longitude": 2.0}]}
        return {"current": {"temperature_2m": 70.0, "weather_code": 0,
                            "wind_speed_10m": 5.0, "relative_humidity_2m": 50.0}}
    w._gj = fake_gj
    msg = w.generate("Testville")
    assert "Testville" in msg, msg
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM playables WHERE kind='weather'").fetchone()[0]
    assert n == 1, n
    print("OK first")
""")

PRESERVE_CODE = textwrap.dedent("""
    import hashlib
    import json
    from bumparr import db
    import bumparr.generators.weather as w
    db.init_db()
    def fake_gj(url):
        if "geocoding" in url:
            return {"results": [{"name": "Testville", "admin1": "Testland",
                                 "latitude": 1.0, "longitude": 2.0}]}
        return {"current": {"temperature_2m": 70.0, "weather_code": 0,
                            "wind_speed_10m": 5.0, "relative_humidity_2m": 50.0}}
    w._gj = fake_gj
    w.generate("Testville")
    label = "Testville, Testland"
    pid = "card:weather:" + hashlib.md5(label.encode()).hexdigest()[:12]
    with db.conn() as c:
        c.execute("UPDATE playables SET uri=?, play_count=?, enabled=?, health=?, weight=?, last_played=?, created_at=? WHERE id=?",
                  ("custom/uri.mp4", 42, 0, "dead", 9.9, 12345.0, 11111.0, pid))
        c.commit()
    def fake_gj2(url):
        if "geocoding" in url:
            return {"results": [{"name": "Testville", "admin1": "Testland",
                                 "latitude": 1.0, "longitude": 2.0}]}
        return {"current": {"temperature_2m": 80.0, "weather_code": 1,
                            "wind_speed_10m": 10.0, "relative_humidity_2m": 60.0}}
    w._gj = fake_gj2
    w.generate("Testville")
    with db.conn() as c:
        r = c.execute("SELECT * FROM playables WHERE id=?", (pid,)).fetchone()
    assert r["uri"] == "custom/uri.mp4", dict(r)
    assert r["play_count"] == 42, dict(r)
    assert r["enabled"] == 0, dict(r)
    assert r["health"] == "dead", dict(r)
    assert abs(r["weight"] - 9.9) < 1e-6, dict(r)
    assert r["last_played"] == 12345.0, dict(r)
    assert r["created_at"] == 11111.0, dict(r)
    payload = json.loads(r["payload"])
    assert payload["temp"] == "80°", payload
    assert r["title"] == label, dict(r)
    print("OK preserve")
""")


class Weather(unittest.TestCase):
    def test_n_flag_is_not_accepted(self):
        result = subprocess.run(
            [sys.executable, "-m", "bumparr.generators.weather", "--n", "2"],
            cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_first_insert(self):
        r = run_snippet(FIRST_INSERT_CODE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("OK first", r.stdout)

    def test_refresh_preserves_operator_fields(self):
        r = run_snippet(PRESERVE_CODE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("OK preserve", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
