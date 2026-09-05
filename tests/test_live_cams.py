"""M5: live-cam YAML validation, enabled/health preservation, removal parks.

load_cams() touches the registry, and config.DB_PATH is read at import time,
so every scenario runs in a child process with DB_PATH/ASSET_ROOT pointed at
a temp dir. One child run exercises the full lifecycle; each test method
asserts one slice of its reported snapshots.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVER = '''
import json
import sys
from pathlib import Path

from bumparr import db
import bumparr.live_cams as live_cams

yaml_full, yaml_removed, yaml_empty, yaml_bad = sys.argv[1:5]
live_cams.CONFIG = Path(yaml_full)
db.init_db()
out = {}
out["n1"] = live_cams.load_cams()

def rows():
    with db.conn() as c:
        return [
            {k: r[k] for k in ("id", "uri", "weight", "enabled", "health")}
            for r in c.execute(
                "SELECT id, uri, weight, enabled, health FROM playables ORDER BY uri")
        ]

out["rows1"] = rows()
with db.conn() as c:
    c.execute("UPDATE playables SET enabled=0, health='dead' WHERE uri=?",
              ("http://example.com/a.m3u8",))
    c.commit()
out["n2"] = live_cams.load_cams()
out["rows2"] = rows()
live_cams.CONFIG = Path(yaml_removed)
out["n3"] = live_cams.load_cams()
out["rows3"] = rows()
live_cams.CONFIG = Path(yaml_empty)
out["n4"] = live_cams.load_cams()
out["rows4"] = rows()
live_cams.CONFIG = Path(yaml_bad)
out["n5"] = live_cams.load_cams()
out["rows5"] = rows()
print(json.dumps(out))
'''

YAML_FULL = '''\
cams:
  - id: cam-a
    title: Cam A
    url: http://example.com/a.m3u8
    weight: 2.0
  - id: cam-b
    title: Bad Weight
    url: http://example.com/b.m3u8
    weight: heavy
  - id: cam-nan
    url: http://example.com/nan.m3u8
    weight: .nan
  - id: cam-inf
    url: http://example.com/inf.m3u8
    weight: .inf
  - title: Bad URL
    url: 12345
  - just a string entry
  - title: No URL
'''

YAML_REMOVED = '''\
cams:
  - id: cam-a
    title: Cam A
    url: http://example.com/a-new.m3u8
'''

URL_A = "http://example.com/a.m3u8"
URL_B = "http://example.com/b.m3u8"
URL_A_NEW = "http://example.com/a-new.m3u8"
URL_NAN = "http://example.com/nan.m3u8"
URL_INF = "http://example.com/inf.m3u8"


class LiveCams(unittest.TestCase):
    """Bad entries never block good cams; operator intent survives reloads."""

    @classmethod
    def setUpClass(cls):
        """Run the lifecycle driver once in a child with a temp DB."""
        cls.tmp = tempfile.TemporaryDirectory()
        full = os.path.join(cls.tmp.name, "cams_full.yaml")
        removed = os.path.join(cls.tmp.name, "cams_removed.yaml")
        empty = os.path.join(cls.tmp.name, "cams_empty.yaml")
        bad = os.path.join(cls.tmp.name, "cams_bad.yaml")
        driver = os.path.join(cls.tmp.name, "driver_livecams.py")
        for path, content in ((full, YAML_FULL), (removed, YAML_REMOVED),
                              (empty, "cams: []\n"), (bad, "- not-a-mapping\n"),
                              (driver, DRIVER)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        env = dict(os.environ,
                   DB_PATH=os.path.join(cls.tmp.name, "test.db"),
                   ASSET_ROOT=os.path.join(cls.tmp.name, "assets"),
                   PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
        p = subprocess.run([sys.executable, driver, full, removed, empty, bad],
                           capture_output=True, text=True, env=env,
                           cwd=REPO, timeout=120)
        assert p.returncode == 0, "live-cams child failed: %s" % p.stderr
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _by_uri(self, rows):
        return {r["uri"]: r for r in rows}

    def test_bad_entries_skipped_good_cams_load(self):
        """A non-string URL, a non-mapping entry, and a missing URL are skipped."""
        rows = self._by_uri(self.out["rows1"])
        self.assertEqual(sorted(rows), sorted([URL_A, URL_B, URL_NAN, URL_INF]))
        self.assertEqual(self.out["n1"], 4)

    def test_bad_weight_defaults(self):
        """An unparseable weight loads the cam at 1.0 instead of crashing."""
        rows = self._by_uri(self.out["rows1"])
        self.assertEqual(rows[URL_A]["weight"], 2.0)
        self.assertEqual(rows[URL_B]["weight"], 1.0)
        self.assertEqual(rows[URL_NAN]["weight"], 1.0)
        self.assertEqual(rows[URL_INF]["weight"], 1.0)

    def test_disabled_dead_cam_preserved_on_reload(self):
        """A second load_cams() keeps enabled=0 and health untouched when uri is unchanged."""
        rows = self._by_uri(self.out["rows2"])
        self.assertEqual(rows[URL_A]["enabled"], 0)
        self.assertEqual(rows[URL_A]["health"], "dead")
        self.assertEqual(rows[URL_B]["enabled"], 1)
        self.assertEqual(rows[URL_B]["health"], "ok")

    def test_removed_cam_parked_not_deleted(self):
        """A cam absent from the YAML keeps its row, parked with enabled=0."""
        rows = self._by_uri(self.out["rows3"])
        self.assertIn(URL_B, rows)
        self.assertEqual(rows[URL_B]["enabled"], 0)

    def test_all_removed_cams_are_parked(self):
        rows = self.out["rows4"]
        self.assertTrue(rows)
        self.assertTrue(all(row["enabled"] == 0 for row in rows))

    def test_malformed_top_level_is_a_noop(self):
        self.assertEqual(self.out["n5"], 0)
        self.assertEqual(self.out["rows5"], self.out["rows4"])

    def test_changed_url_reuses_stable_id_and_revives_health(self):
        rows = self._by_uri(self.out["rows3"])
        self.assertNotIn(URL_A, rows)
        self.assertEqual(rows[URL_A_NEW]["id"], "stream:cam:cam-a")
        self.assertEqual(rows[URL_A_NEW]["health"], "ok")
        self.assertEqual(rows[URL_A_NEW]["enabled"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
