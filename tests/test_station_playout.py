import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import config, db
from bumparr.station import conform, playout


def idx(item_id, key, segs):
    return {"id": item_id, "key": key, "segments": list(segs), "duration": round(sum(segs), 3), "conformed_at": 1}


INDEX = {"a": idx("a", "k-a", [4.0, 4.0, 2.0]),
         "b": idx("b", "k-b", [4.0, 1.5]),
         "c": idx("c", "k-c", [4.0, 4.0, 4.0, 4.0])}

URL = lambda key, n: "http://x/station/seg/%s/%03d.ts" % (key, n)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "p.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"; config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        with db.conn() as c:
            for i, kind in (("a", "ambient"), ("b", "station_id"), ("c", "trivia")):
                c.execute("INSERT INTO playables (id,type,kind,uri,duration,title) VALUES (?,?,?,?,?,?)",
                          (i, "video", kind, "bumpers/%s.mp4" % i, 10, "Title " + i))
            c.commit()
        playout.reset(); self.addCleanup(playout.reset)
        self.patch = mock.patch.object(conform, "load_index", return_value=dict(INDEX)); self.patch.start()
        self.addCleanup(self.patch.stop)

    def channel(self, name="live", kinds=None, now=1000.0, seed=1):
        return playout.Channel(name, kinds, now, random.Random(seed))


class Timeline(Base):
    def test_extends_to_cover_the_lookahead_and_never_repeats_adjacent(self):
        ch = self.channel()
        ch.advance(1000.0)
        self.assertGreaterEqual(ch.timeline[-1].end, 1000.0 + ch.lookahead)
        self.assertEqual(ch.timeline[0].start, 1000.0)
        for e1, e2 in zip(ch.timeline, ch.timeline[1:]):
            self.assertEqual(e1.end, e2.start); self.assertNotEqual(e1.item_id, e2.item_id)

    def test_kinds_filter_restricts_the_pool(self):
        ch = self.channel("standby", {"station_id"})
        ch.advance(1000.0)
        self.assertTrue(all(e.item_id == "b" for e in ch.timeline))

    def test_empty_pool_airs_the_slate_or_nothing(self):
        with mock.patch.object(conform, "load_index", return_value={"slate": idx("slate", "slate", [4.0, 4.0, 2.0])}):
            ch = self.channel("standby", {"station_id"}); ch.advance(1000.0)
            self.assertTrue(ch.timeline and all(e.item_id == "slate" for e in ch.timeline))
        with mock.patch.object(conform, "load_index", return_value={}):
            ch = self.channel(); self.assertIsNone(ch.playlist(1000.0, URL))

    def test_stale_timeline_restarts_from_now_and_keeps_sequence_monotonic(self):
        ch = self.channel(); ch.advance(1000.0)
        seq_before = ch.seq_base; n = sum(len(e.segments) for e in ch.timeline)
        ch.advance(5000.0)
        self.assertEqual(ch.timeline[0].start, 5000.0)
        self.assertGreaterEqual(ch.seq_base, seq_before + n)

    def test_zero_score_candidates_never_air(self):
        ch = self.channel()
        with mock.patch.object(playout.seasons, "factors_now", return_value={
                "ambient": 0.0, "station_id": 1.0, "trivia": 0.0}), \
                mock.patch.object(playout.dayparts, "factors_now", return_value={}):
            self.assertEqual(ch._pick(1000.0, None)["id"], "b")

    def test_positive_previous_item_repeats_when_alternatives_are_gated(self):
        ch = self.channel()
        with mock.patch.object(playout.seasons, "factors_now", return_value={
                "ambient": 1.0, "station_id": 0.0, "trivia": 0.0}), \
                mock.patch.object(playout.dayparts, "factors_now", return_value={}):
            self.assertEqual(ch._pick(1000.0, "a")["id"], "a")

    def test_slate_airs_when_every_candidate_is_gated(self):
        index = {**INDEX, "slate": idx("slate", "slate", [4.0])}
        ch = self.channel()
        with mock.patch.object(conform, "load_index", return_value=index), \
                mock.patch.object(playout.seasons, "factors_now", return_value={
                    "ambient": 0.0, "station_id": 0.0, "trivia": 0.0}), \
                mock.patch.object(playout.dayparts, "factors_now", return_value={}):
            self.assertEqual(ch._pick(1000.0, None)["id"], "slate")


class Reporting(Base):
    def rows(self):
        with db.conn() as c:
            plays = dict(c.execute("SELECT id, play_count FROM playables").fetchall())
            hist = c.execute("SELECT playable_id, played_at FROM play_history WHERE channel_id='station:live' ORDER BY played_at").fetchall()
            cur = c.execute("SELECT current_id, started_at FROM playout WHERE channel_id='station:live'").fetchone()
        return plays, [tuple(h) for h in hist], (tuple(cur) if cur else None)

    def test_reports_each_started_entry_exactly_once(self):
        ch = self.channel(); ch.advance(1000.0)
        plays, hist, cur = self.rows()
        self.assertEqual(hist, [(ch.timeline[0].item_id, 1000.0)]); self.assertEqual(sum(plays.values()), 1)
        self.assertEqual(cur, (ch.timeline[0].item_id, 1000.0))
        ch.advance(1000.0); self.assertEqual(len(self.rows()[1]), 1)
        t = ch.timeline[1].start; ch.advance(t)
        plays, hist, cur = self.rows()
        self.assertEqual(len(hist), 2); self.assertEqual(cur, (ch.timeline[1].item_id, t))
        with db.conn() as c:
            lp = c.execute("SELECT last_played FROM playables WHERE id=?", (ch.timeline[1].item_id,)).fetchone()[0]
        self.assertEqual(lp, t)

    def test_never_writes_weight(self):
        ch = self.channel(); ch.advance(1000.0); ch.advance(1100.0)
        with db.conn() as c:
            self.assertEqual({r[0] for r in c.execute("SELECT weight FROM playables")}, {1.0})

    def test_reconnect_does_not_report_unwatched_lookahead(self):
        ch = self.channel(); ch.playlist(1000.0, URL)
        reconnect = 1000.0 + ch.lookahead + 1.0
        old_end = ch.timeline[-1].end
        self.assertLess(reconnect, old_end + ch.lookahead)

        ch.playlist(reconnect, URL)

        _, hist, cur = self.rows()
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0][1], 1000.0)
        self.assertEqual(hist[1][1], reconnect)
        self.assertEqual(cur[1], reconnect)
        self.assertEqual(ch.timeline[0].start, reconnect)


class Playlist(Base):
    def test_window_shape_and_discontinuities(self):
        ch = self.channel(); body = ch.playlist(1000.0, URL)
        lines = body.splitlines()
        self.assertEqual(lines[:2], ["#EXTM3U", "#EXT-X-VERSION:3"])
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", lines); self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:0", lines)
        self.assertNotIn("#EXT-X-ENDLIST", lines)
        first = ch.timeline[0]
        self.assertEqual(lines[lines.index("#EXT-X-DISCONTINUITY") + 1], "#EXTINF:%.3f," % first.segments[0])
        self.assertIn(URL(first.key, 0), lines)
        segs = [l for l in lines if l.endswith(".ts")]
        self.assertGreaterEqual(len(segs), config.STATION_WINDOW_SEGMENTS)
        target = int([l for l in lines if l.startswith("#EXT-X-TARGETDURATION:")][0].split(":")[1])
        self.assertGreaterEqual(target, 4)

    def test_sequence_numbers_advance_with_time(self):
        ch = self.channel(); ch.playlist(1000.0, URL)
        first = ch.timeline[0]
        # One segment length past the first entry's end: nothing of it can be in the window.
        later = ch.playlist(first.end + config.STATION_SEGMENT_SECONDS + 0.1, URL).splitlines()
        seq = int([l for l in later if l.startswith("#EXT-X-MEDIA-SEQUENCE:")][0].split(":")[1])
        dseq = int([l for l in later if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")][0].split(":")[1])
        self.assertEqual(seq, len(first.segments))
        self.assertEqual(dseq, 1)
        self.assertNotIn(first.key, "\n".join(later))

    def test_mid_entry_window_counts_the_hidden_discontinuity(self):
        ch = self.channel(); ch.playlist(1000.0, URL)
        e1 = ch.timeline[1]
        # A window opening after the second entry's first segment has fully aged out:
        # both entry 0's tag and entry 1's own tag precede the playlist.
        t = e1.start + e1.segments[0] + config.STATION_SEGMENT_SECONDS + 0.1
        lines = ch.playlist(t, URL).splitlines()
        dseq = int([l for l in lines if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")][0].split(":")[1])
        self.assertEqual(dseq, 2)


class Snapshot(Base):
    def test_now_and_next(self):
        ch = self.channel(); ch.playlist(1000.0, URL); s = ch.snapshot(1000.0)
        self.assertEqual(s["now"]["id"], ch.timeline[0].item_id)
        self.assertEqual(s["now"]["started_at"], 1000.0); self.assertEqual(s["now"]["title"], "Title " + ch.timeline[0].item_id)
        self.assertEqual(s["next"]["id"], ch.timeline[1].item_id)
        self.assertEqual(ch.active_keys(), {e.key for e in ch.timeline})

    def test_snapshot_is_read_only_and_does_not_report(self):
        ch = self.channel()
        self.assertEqual(ch.snapshot(1000.0), {"now": None, "next": None})
        self.assertEqual(ch.timeline, [])
        ch.playlist(1000.0, URL)
        timeline = list(ch.timeline)
        reported = ch.reported

        self.assertEqual(ch.snapshot(5000.0), {"now": None, "next": None})
        self.assertEqual(ch.timeline, timeline)
        self.assertEqual(ch.reported, reported)
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM play_history").fetchone()[0], 1)

    def test_registry_creates_known_channels_only(self):
        self.assertIsNone(playout.get("nope"))
        live = playout.get("live", 1000.0); self.assertIs(live, playout.get("live"))
        self.assertEqual(playout.get("standby", 1000.0).kinds, set(config.STANDBY_KINDS))
        live.advance(1000.0)
        self.assertTrue(playout.active_keys())


if __name__ == "__main__":
    unittest.main()
