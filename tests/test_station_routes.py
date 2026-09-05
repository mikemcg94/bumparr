import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from bumparr import config, db
from bumparr.app import app
from bumparr.station import conform, playout


def idx(item_id, key, segs):
    return {"id": item_id, "key": key, "segments": list(segs), "duration": round(sum(segs), 3), "conformed_at": 1}


class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR, config.PUBLIC_BASE_URL
        config.DB_PATH = str(Path(self.tmp.name) / "r.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"; config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        config.PUBLIC_BASE_URL = "http://bumparr.example:8780"
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR", "PUBLIC_BASE_URL"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        with db.conn() as c:
            c.execute("INSERT INTO playables (id,type,kind,uri,duration,title) VALUES (?,?,?,?,?,?)",
                      ("a", "video", "station_id", "bumpers/a.mp4", 10, "Ident"))
            c.commit()
        playout.reset(); self.addCleanup(playout.reset)
        self.client = TestClient(app)

    def test_playlist_is_live_and_absolute(self):
        with mock.patch.object(conform, "load_index", return_value={"a": idx("a", "k-a", [4.0, 4.0, 2.0])}):
            r = self.client.get("/station/live/index.m3u8")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/vnd.apple.mpegurl")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("http://bumparr.example:8780/station/seg/k-a/000.ts", r.text)
        self.assertNotIn("#EXT-X-ENDLIST", r.text)

    def test_transport_stream_segment_has_video_content_type(self):
        root = config.ASSET_ROOT / ".cache" / "station"
        segment = root / "k-a" / "000.ts"
        segment.parent.mkdir(parents=True)
        segment.write_bytes(b"transport-stream")
        static = next(route.app for route in app.routes
                      if getattr(route, "name", None) == "station-segments")
        with mock.patch.object(static, "directory", str(root)), \
                mock.patch.object(static, "all_directories", [str(root)]), \
                mock.patch.object(static, "config_checked", False), \
                mock.patch("starlette.responses.guess_type",
                           return_value=("text/vnd.trolltech.linguist", None)):
            r = self.client.get("/station/seg/k-a/000.ts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"transport-stream")
        self.assertEqual(r.headers["content-type"], "video/mp2t")

    def test_unknown_channel_and_empty_cache(self):
        self.assertEqual(self.client.get("/station/nope/index.m3u8").status_code, 404)
        with mock.patch.object(conform, "load_index", return_value={}):
            r = self.client.get("/station/standby/index.m3u8")
        self.assertEqual(r.status_code, 503); self.assertIn("conform", r.text)

    def test_channel_m3u_and_guide(self):
        r = self.client.get("/station/channel.m3u")
        self.assertIn('tvg-id="bumparr.live"', r.text); self.assertIn('tvg-id="bumparr.standby"', r.text)
        self.assertIn("http://bumparr.example:8780/station/standby/index.m3u8", r.text)
        g = self.client.get("/station/guide.xml")
        self.assertEqual(g.status_code, 200); self.assertIn('channel id="bumparr.live"', g.text)
        self.assertEqual(g.headers["content-type"].split(";")[0], "application/xml")

    def test_status_shape(self):
        with mock.patch.object(conform, "load_index", return_value={"a": idx("a", "k-a", [4.0]), "slate": idx("slate", "slate", [4.0])}), \
                mock.patch.object(conform, "ffmpeg_path", return_value=None):
            s = self.client.get("/api/station").json()
        self.assertEqual((s["ffmpeg"], s["conformed"], s["eligible"], s["pending"]), (False, 1, 1, 0))
        self.assertEqual(s["urls"]["channel_m3u"], "http://bumparr.example:8780/station/channel.m3u")
        self.assertIsNone(s["channels"]["live"]["now"])
        self.assertIn("standby", s["channels"])

    def test_status_does_not_start_or_report_playout(self):
        index = {"a": idx("a", "k-a", [4.0]), "slate": idx("slate", "slate", [4.0])}
        with mock.patch.object(conform, "load_index", return_value=index):
            with mock.patch("bumparr.station.routes.time.time", return_value=1000.0):
                self.client.get("/api/station")
            with mock.patch("bumparr.station.routes.time.time", return_value=5000.0):
                self.client.get("/api/station")

        self.assertEqual(playout.get("live").timeline, [])
        self.assertEqual(playout.get("standby").timeline, [])
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM play_history").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT play_count FROM playables WHERE id='a'").fetchone()[0], 0)
            self.assertIsNone(c.execute("SELECT current_id FROM playout").fetchone())

    def test_conform_action_is_a_job(self):
        with mock.patch.object(conform, "sweep", return_value={"conformed": 0}) as sweep:
            r = self.client.post("/api/station/conform?limit=5")
            self.assertEqual(r.status_code, 200); job = r.json()["job_id"]
            for _ in range(50):
                st = self.client.get("/api/request/" + job).json()
                if st["status"] != "working":
                    break
        self.assertEqual(st["status"], "done"); sweep.assert_called_once()
        self.assertEqual(sweep.call_args.kwargs.get("limit"), 5)


if __name__ == "__main__":
    unittest.main()
