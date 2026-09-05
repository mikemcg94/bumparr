"""Recovery paths for rows the system parked.

`enabled` is operator intent and no loader may write it (docs/FIX_PLAN.md M5.3),
so the only way back from a park is an explicit operator action. These cover the
two: revive, which clears a park it can physically verify, and enable, which is
the operator saying so outright.

ffprobe is not installed in CI, so every probe is mocked.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import app as webapp
from bumparr import config, db
from bumparr.generators import on_this_day


def _probe_ok(*a, **k):
    return subprocess.CompletedProcess(a[0] if a else [], 0, "h264\n", "")


def _probe_bad(*a, **k):
    return subprocess.CompletedProcess(a[0] if a else [], 1, "", "boom")


class PoolRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "recovery.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()

    def _file(self, rel):
        path = config.ASSET_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 64)
        return rel

    def _seed(self, *rows):
        with db.conn() as c:
            c.executemany(
                "INSERT INTO playables (id,type,kind,uri,duration,enabled,health,payload) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            c.commit()

    def _state(self):
        with db.conn() as c:
            return {r["id"]: (r["enabled"], r["health"])
                    for r in c.execute("SELECT id, enabled, health FROM playables")}

    def test_parked_media_with_readable_file_is_re_enabled(self):
        """A file that came back clears both the health and the enabled park."""
        self._seed(("v", "video", "ambient", self._file("ambient/back.mp4"), 3, 0, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            out = webapp.revive()
        self.assertEqual(out["restored"], 1)
        self.assertEqual(self._state()["v"], (1, "ok"))

    def test_row_left_enabled_but_marked_dead_is_returned_to_health_ok(self):
        """The health-only park: nothing disabled the row, it was only flagged.

        Widening revive to clear `enabled` as well must not cost it the case it
        already handled — a row still on air whose health went dead.
        """
        self._seed(("v", "video", "ambient", self._file("ambient/ok.mp4"), 3, 1, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            webapp.revive()
        self.assertEqual(self._state()["v"], (1, "ok"))

    def test_missing_file_stays_parked(self):
        self._seed(("v", "video", "ambient", "ambient/gone.mp4", 3, 0, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            out = webapp.revive()
        self.assertEqual(out["still_dead"], 1)
        self.assertEqual(self._state()["v"], (0, "dead"))

    def test_unreadable_file_stays_parked(self):
        self._seed(("v", "video", "ambient", self._file("ambient/junk.mp4"), 3, 0, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_bad):
            webapp.revive()
        self.assertEqual(self._state()["v"], (0, "dead"))

    def test_on_this_day_cards_are_never_revived(self):
        """The calendar parks these by date; reviving them would break rotation."""
        self._seed(("c", "card", "on_this_day", self._file("bumpers/otd.mp4"), 3, 0, "ok", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            webapp.revive()
        self.assertEqual(self._state()["c"], (0, "ok"))

    def test_streams_are_skipped_not_enabled(self):
        """A parked live cam has no local file to verify; enable is its path back."""
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 0, "ok", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            out = webapp.revive()
        self.assertEqual(out["skipped_streams"], 1)
        self.assertEqual(self._state()["s"], (0, "ok"))

    def test_dry_run_writes_nothing(self):
        self._seed(("v", "video", "ambient", self._file("ambient/dry.mp4"), 3, 0, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", _probe_ok):
            out = webapp.revive(dry_run=True)
        self.assertEqual(out["restored"], 1)
        self.assertEqual(self._state()["v"], (0, "dead"))

    def test_missing_ffprobe_does_not_raise(self):
        """ffprobe is absent on plenty of hosts, including CI; revive must not 500."""
        self._seed(("v", "video", "ambient", self._file("ambient/x.mp4"), 3, 0, "dead", "{}"))
        with mock.patch.object(webapp.subprocess, "run", side_effect=FileNotFoundError):
            out = webapp.revive()
        self.assertEqual(out["still_dead"], 1)
        self.assertEqual(self._state()["v"], (0, "dead"))


class PoolEnable(PoolRecovery):
    """Explicit operator re-enable — the only path back for a parked stream."""

    def test_parked_stream_is_enabled(self):
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 0, "ok", "{}"))
        out = webapp.enable_playable("s")
        self.assertEqual((out["enabled"], out["changed"]), (True, True))
        self.assertEqual(self._state()["s"], (1, "ok"))

    def test_enabling_an_enabled_row_is_a_noop(self):
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 1, "ok", "{}"))
        out = webapp.enable_playable("s")
        self.assertEqual((out["enabled"], out["changed"]), (True, False))

    def test_unknown_id_is_404(self):
        out = webapp.enable_playable("nope")
        self.assertEqual(out.status_code, 404)

    def test_enable_does_not_touch_health(self):
        """Reachability is the far end's business; this only states intent."""
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 0, "dead", "{}"))
        webapp.enable_playable("s")
        self.assertEqual(self._state()["s"], (1, "dead"))


class CalendarParkWarning(PoolRecovery):
    """Enabling a row whose `enabled` belongs to the calendar, not the operator.

    `enabled` is one column carrying three unrelated claims — operator intent, a
    system park, and the on_this_day date rotation — and nothing on the row says
    which wrote a 0. Revive dodges the calendar case by excluding the kind;
    enable cannot and must not, because the operator named an id. What it can do
    is stop the un-park from looking permanent when it is not.
    """

    def _otd(self, pid, for_date):
        payload = json.dumps({"lines": ["ON THIS DAY"], "for_date": for_date},
                             sort_keys=True)
        self._seed((pid, "card", "on_this_day", None, 8, 0, "ok", payload))

    def test_calendar_managed_card_is_enabled_and_warns(self):
        """The operator gets what they asked for, plus what happens next."""
        self._otd("c", "01-02" if on_this_day.today_key() != "01-02" else "03-04")
        out = webapp.enable_playable("c")
        self.assertEqual(self._state()["c"][0], 1)
        self.assertIn("warning", out)
        self.assertIn("parked by date", out["warning"])
        self.assertIn("dated_card_loop", out["warning"])
        self.assertIn("hourly", out["warning"])

    def test_todays_card_warns_that_the_rollover_still_owns_it(self):
        """Today's card is not about to be parked, so say the true thing."""
        self._otd("c", on_this_day.today_key())
        out = webapp.enable_playable("c")
        self.assertIn("warning", out)
        self.assertIn("belongs to today", out["warning"])
        self.assertIn("dated_card_loop", out["warning"])

    def test_ordinary_parked_row_carries_no_warning(self):
        """Nothing rotates a cam, so there is nothing to warn about."""
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 0, "ok", "{}"))
        self.assertNotIn("warning", webapp.enable_playable("s"))

    def test_existing_response_keys_are_unchanged(self):
        """The warning is additive: no consumer of the old three keys breaks."""
        self._otd("c", "01-02" if on_this_day.today_key() != "01-02" else "03-04")
        out = webapp.enable_playable("c")
        self.assertEqual({k: out[k] for k in ("id", "enabled", "changed")},
                         {"id": "c", "enabled": True, "changed": True})
        self._seed(("s", "stream", "webcam", "http://example.com/a.m3u8", 45, 0, "ok", "{}"))
        self.assertEqual(webapp.enable_playable("s"),
                         {"id": "s", "enabled": True, "changed": True})

    def test_unparsable_payload_still_warns(self):
        """A card with a broken payload is still the calendar's; do not crash."""
        self._seed(("c", "card", "on_this_day", None, 8, 0, "ok", "{not json"))
        out = webapp.enable_playable("c")
        self.assertIn("warning", out)
        self.assertEqual(self._state()["c"][0], 1)

    def _rotate(self):
        with db.conn() as c:
            counts = on_this_day.retire_other_days(c)
            c.commit()
        return counts

    def test_warning_matches_the_next_pass_for_a_compact_payload(self):
        """The drift, shape one: `{"for_date":"MM-DD"}` with no space after the
        colon. The warning parsed it and said "belongs to today, stays on"; the
        rotation matched serialized text with LIKE, missed it, and parked it."""
        payload = json.dumps({"lines": ["ON THIS DAY"],
                              "for_date": on_this_day.today_key()},
                             separators=(",", ":"))
        self._seed(("c", "card", "on_this_day", None, 8, 0, "ok", payload))
        self.assertIn("belongs to today", webapp.enable_playable("c")["warning"])
        self._rotate()
        self.assertEqual(self._state()["c"][0], 1)

    def test_warning_matches_the_next_pass_for_a_null_payload(self):
        """The drift, shape two, and its mirror: `payload NOT LIKE ?` is NULL for
        a NULL payload, so the promised park never came and the card stayed on
        air for good."""
        self._seed(("c", "card", "on_this_day", None, 8, 0, "ok", None))
        self.assertIn("parked by date", webapp.enable_playable("c")["warning"])
        self._rotate()
        self.assertEqual(self._state()["c"][0], 0)


class DatedCardRotation(PoolRecovery):
    """retire_other_days: which cards the calendar counts as today's.

    It used to decide with SQL `LIKE '%"for_date": "MM-DD"%'` against the
    serialized payload while /api/pool/enable's warning decided by parsing the
    same payload, so the two disagreed on every shape json.dumps' defaults do
    not produce. Both now ask on_this_day.is_todays_card, and these pin the
    shapes that diverged alongside the ordinary one.
    """

    def _other_day(self):
        return "01-02" if on_this_day.today_key() != "01-02" else "03-04"

    def _card(self, pid, for_date, enabled, compact=False):
        body = {"lines": ["ON THIS DAY"], "for_date": for_date}
        payload = (json.dumps(body, separators=(",", ":")) if compact
                   else json.dumps(body, sort_keys=True))
        self._seed((pid, "card", "on_this_day", None, 8, enabled, "ok", payload))

    def _rotate(self):
        with db.conn() as c:
            counts = on_this_day.retire_other_days(c)
            c.commit()
        return counts

    def test_todays_card_comes_back_and_another_days_is_parked(self):
        """The ordinary shape, pinned so the rewrite kept the behaviour."""
        self._card("today", on_this_day.today_key(), 0)
        self._card("other", self._other_day(), 1)
        self.assertEqual(self._rotate(), (1, 1))
        self.assertEqual(self._state()["today"][0], 1)
        self.assertEqual(self._state()["other"][0], 0)

    def test_compact_serialized_card_is_recognised_as_todays(self):
        """No space after the colon: the LIKE pattern missed it, so a card that
        belongs to today was parked on the next pass anyway."""
        self._card("compact", on_this_day.today_key(), 1, compact=True)
        self._card("compact_parked", on_this_day.today_key(), 0, compact=True)
        self.assertEqual(self._rotate(), (1, 0))
        self.assertEqual(self._state()["compact"][0], 1)
        self.assertEqual(self._state()["compact_parked"][0], 1)

    def test_compact_serialized_card_from_another_day_is_still_parked(self):
        """Recognising the shape must not mean keeping every card in it."""
        self._card("compact", self._other_day(), 1, compact=True)
        self.assertEqual(self._rotate(), (0, 1))
        self.assertEqual(self._state()["compact"][0], 0)

    def test_null_payload_card_is_parked_rather_than_ignored(self):
        """`payload NOT LIKE ?` is NULL for a NULL payload — never true — so SQL
        matching left these on air for ever. No for_date means not today."""
        self._seed(("null", "card", "on_this_day", None, 8, 1, "ok", None))
        self.assertEqual(self._rotate(), (0, 1))
        self.assertEqual(self._state()["null"][0], 0)

    def test_malformed_payload_card_is_parked_without_raising(self):
        self._seed(("junk", "card", "on_this_day", None, 8, 1, "ok", "{not json"))
        self._seed(("list", "card", "on_this_day", None, 8, 1, "ok", "[1, 2]"))
        self.assertEqual(self._rotate(), (0, 2))
        self.assertEqual(self._state()["junk"][0], 0)
        self.assertEqual(self._state()["list"][0], 0)

    def test_counts_report_only_the_rows_that_moved(self):
        """(on, off) is still a pair of rowcounts: cards switched on, cards
        parked. Rows already in the right state are in neither."""
        self._card("on_already", on_this_day.today_key(), 1)
        self._card("off_already", self._other_day(), 0)
        self.assertEqual(self._rotate(), (0, 0))
        self.assertEqual(self._state()["on_already"][0], 1)
        self.assertEqual(self._state()["off_already"][0], 0)

    def test_other_kinds_are_left_alone(self):
        """The rotation owns on_this_day's `enabled` and nothing else's."""
        self._seed(("cam", "stream", "webcam", "http://x/a.m3u8", 45, 1, "ok", "{}"),
                   ("triv", "card", "trivia", None, 8, 0, "ok", "{}"))
        self.assertEqual(self._rotate(), (0, 0))
        self.assertEqual(self._state()["cam"][0], 1)
        self.assertEqual(self._state()["triv"][0], 0)


class EnabledFilter(PoolRecovery):
    """`GET /api/bumpers?enabled=false` — finding the parked rows to recover.

    Without it, spotting a parked row means paging the whole pool by eye, which
    is what made the recovery endpoints hard to reach in the first place.
    """

    def _pair(self):
        """One live cam and one parked cam. Seeded per test, not in setUp, so the
        inherited revive cases still see the empty pool they were written for."""
        self._seed(("on", "stream", "webcam", "http://example.com/a.m3u8", 45, 1, "ok", "{}"),
                   ("off", "stream", "webcam", "http://example.com/b.m3u8", 45, 0, "ok", "{}"))

    def _ids(self, **kw):
        out = webapp.list_bumpers(None, q=None, limit=100, offset=0, **kw)
        return sorted(b["id"] for b in out["bumpers"])

    def test_absent_means_no_filter_not_false(self):
        """The default listing must keep showing both, or the park hides itself."""
        self._pair()
        self.assertEqual(self._ids(), ["off", "on"])
        self.assertEqual(self._ids(enabled=None), ["off", "on"])

    def test_false_selects_only_parked_rows(self):
        self._pair()
        self.assertEqual(self._ids(enabled=False), ["off"])

    def test_true_selects_only_live_rows(self):
        self._pair()
        self.assertEqual(self._ids(enabled=True), ["on"])

    def test_filter_composes_with_type(self):
        self._pair()
        self._seed(("card", "card", "trivia", None, 8, 0, "ok", "{}"))
        self.assertEqual(self._ids(enabled=False, type="stream"), ["off"])
        self.assertEqual(self._ids(enabled=False, type="card"), ["card"])

    def test_invalid_type_still_rejected_alongside_the_new_filter(self):
        self._pair()
        out = webapp.list_bumpers(None, type="nope", enabled=False,
                                  q=None, limit=100, offset=0)
        self.assertEqual(out.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
