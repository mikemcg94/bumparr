# Park Recovery and Prune Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators a way to turn a system-parked row back on, and make `prune --drop-category`'s dry run tell the truth about what it will delete.

**Architecture:** `enabled` stays what `docs/FIX_PLAN.md` M5.3 says it is — operator intent that no loader may overwrite. We do not change any writer of `enabled=0`. Instead we add the missing *reader-side* recovery: `POST /api/pool/revive` gains the ability to clear a park it can physically verify (file is back and probes clean), and a new `POST /api/pool/enable` handles the cases revive cannot verify (live-cam streams). Separately, `prune.drop_categories` keeps deleting unregistered files but now enumerates them in the dry run.

**Tech Stack:** Python 3.12, FastAPI, SQLite (stdlib `sqlite3`), `unittest` (no pytest — CI runs `python -W error::ResourceWarning -m unittest discover -s tests -v`), `ruff` for F401/F821 only.

**Spec:** `docs/FIX_PLAN.md` (M5 at line 219, seed quality item 6 at line 450, M1 at line 117). The findings this plan closes came from the `/code-review low` pass on PR #1; two of the four original findings were withdrawn after checking them against that spec — see "Findings disposition" below.

## Findings disposition

| Review finding | Verdict | Covered by |
|---|---|---|
| `capture_windows.py:45` dropped `enabled=1` | **Not a bug.** Spec'd by FIX_PLAN seed item 6 ("do not automatically re-enable operator-disabled rows merely because a file reappears"). Restoring it would break `tests/test_seed.py::test_missing_files_park_media_and_clear_card_render`. The real gap is that nothing could *ever* re-enable. | Task 1 |
| `live_cams.py:96,99` dropped `enabled=1` | **Not a bug.** Spec'd by FIX_PLAN M5.3 ("never touch `enabled`"). Asserted by `tests/test_live_cams.py::test_disabled_dead_cam_preserved_on_reload` and `::test_changed_url_reuses_stable_id_and_revives_health`. But the docstring still promises the old behavior, and parked cams had no way back. | Tasks 2, 3 |
| `ingest.py:165` leaks the `.part` file | **False.** `_reject_portrait` (`bumparr/ingest.py:105-119`) calls `os.remove(path)` before returning `True`. No leak. No task. | — |
| `prune.py:166` comment contradicts the code | **True**, low severity. Not a regression — `main` unlinked every file in the dir too. | Task 4 |

## Global Constraints

- **Test runner is `unittest`, not pytest.** Every test file is a `unittest.TestCase` and must pass under `python -m unittest`. Verify with `python -m unittest tests.test_x -v`.
- **CI does not install ffmpeg/ffprobe.** Any test touching a code path that shells out to `ffprobe` MUST mock `subprocess.run`. Never assume the binary exists.
- **`config.DB_PATH`, `config.ASSET_ROOT`, `config.OUTPUT_DIR` are read at call time** by `db.conn()` and `paths.resolve_media()`, so in-process tests patch the `config` module attributes directly (see `tests/test_seed.py:12-20` for the exact setUp idiom). Copy that idiom; do not invent a new one.
- **Never write `enabled` from a config loader or an automatic sweep.** `live_cams.load_cams()`, `seed.seed_from_assets()`, and `capture_windows._upsert_window()` are OFF LIMITS for behavior changes in this plan. Only an explicit operator-triggered endpoint may set `enabled=1`.
- **`kind='on_this_day'` uses `enabled=0` as a calendar park** (`bumparr/generators/on_this_day.py:56-62`). Any query that selects `enabled=0` rows for re-enabling MUST exclude that kind, or every out-of-date historical card gets resurrected.
- Comment/docstring style: this codebase explains *why*, not *what*. Match the surrounding prose density.
- Commit after each task. Conventional-commit prefixes (`fix:`, `feat:`, `docs:`, `test:`).

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `bumparr/app.py` | HTTP surface | Modify `revive()` (line 608); add `enable_playable()` beside it |
| `bumparr/live_cams.py` | YAML → registry loader | Docstring only (lines 16-21) |
| `bumparr/prune.py` | Destructive CLI maintenance | Modify `drop_categories()` (lines 147-177) |
| `tests/test_pool_recovery.py` | **Create.** Covers revive's widened selection + the new enable endpoint | New |
| `tests/test_prune_drop.py` | **Create.** Covers the dry-run enumeration | New |
| `docs/API.md` | Endpoint reference | Update revive entry (line 116); add enable entry |
| `docs/CLI.md` | CLI flag reference | Update `--drop-category` row (line 85) |

---

### Task 1: `/api/pool/revive` clears a park it can verify

**Files:**
- Modify: `bumparr/app.py:608-651` (`revive`)
- Modify: `docs/API.md:116-121`
- Test: `tests/test_pool_recovery.py` (create)

**Interfaces:**
- Consumes: `db.conn()`, `paths.resolve_media(uri) -> Path | None`.
- Produces: `revive(dry_run: bool = False) -> dict` with the **unchanged** key set `{checked, restored, still_dead, skipped_streams, dry_run}`. Task 2 relies on this endpoint remaining a plain `def` (not `async def`) so tests can call it directly.

**Why the query changes:** today `revive` selects `WHERE health='dead'`. A row parked by `seed.py:115` has `enabled=0 AND health='dead'`, so revive *sees* it but only writes `health='ok'` — leaving `enabled=0` and the row permanently out of rotation, because `app.py:244/312/412` all require `enabled=1 AND health='ok'`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pool_recovery.py`:

```python
"""Recovery paths for rows the system parked.

`enabled` is operator intent and no loader may write it (docs/FIX_PLAN.md M5.3),
so the only way back from a park is an explicit operator action. These cover the
two: revive, which clears a park it can physically verify, and enable, which is
the operator saying so outright.

ffprobe is not installed in CI, so every probe is mocked.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import app as webapp
from bumparr import config, db


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

    def test_enabled_row_marked_dead_is_restored_without_touching_enabled(self):
        """The pre-existing health-only case still works."""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_pool_recovery -v`
Expected: `test_parked_media_with_readable_file_is_re_enabled` FAILS (`(0, 'ok') != (1, 'ok')` — health clears, enabled does not); `test_on_this_day_cards_are_never_revived` and `test_streams_are_skipped_not_enabled` may pass incidentally; `test_missing_ffprobe_does_not_raise` FAILS with `FileNotFoundError`.

- [ ] **Step 3: Widen the selection and the update**

In `bumparr/app.py`, in `revive()`, replace the SELECT:

```python
        rows = [dict(r) for r in c.execute(
            "SELECT id, type, uri FROM playables "
            "WHERE (health='dead' OR enabled=0) "
            "AND (kind IS NULL OR kind != 'on_this_day')").fetchall()]
```

Replace the ffprobe guard so a host without ffprobe degrades instead of raising
(`FileNotFoundError` is an `OSError`; the widened selection sends more rows down
this path, so the guard is load-bearing now):

```python
        except (subprocess.TimeoutExpired, OSError):
            still_dead.append(r["id"])
            continue
```

Replace the write:

```python
            c.executemany(
                "UPDATE playables SET health='ok', fail_count=0, enabled=1 WHERE id=?",
                [(i,) for i in restored])
```

- [ ] **Step 4: Update the docstring to describe the widened contract**

Replace `revive`'s docstring:

```python
    """Restore items the system retired whose media is actually fine.

    Two things retire an item without a human deciding to: a player reports a
    playback failure (health='dead'), and the asset sweep finds the file missing
    (seed.py parks it enabled=0, health='dead'). Both can be transient — a
    blocked autoplay, or a snippet caught mid-rewrite — and neither is an
    operator saying "switch this off". So this re-checks each one against
    reality and clears the park only when ffprobe can still read the file.

    on_this_day cards are excluded: their enabled=0 is a calendar rotation, not
    a retirement, and reviving them would put every wrong-date card back on air.
    Live streams are skipped because their health depends on the far end —
    POST /api/pool/enable is the deliberate way to bring one of those back.
    """
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_pool_recovery -v`
Expected: all 8 PASS.

- [ ] **Step 6: Run the full suite — nothing else may shift**

Run: `python -W error::ResourceWarning -m unittest discover -s tests -v 2>&1 | tail -20`
Expected: OK. If `tests/test_seed.py` or `tests/test_live_cams.py` fails, you changed a loader — revert that; this task touches `app.py` only.

- [ ] **Step 7: Update `docs/API.md`**

Replace the `POST /api/pool/revive` body (line ~118) with:

```markdown
Re-check items the system retired: `health='dead'` (a player reported a failure)
and `enabled=0` (the asset sweep found the file missing). A local file ffprobe
can still read is restored to `enabled=1, health='ok'`. `on_this_day` cards are
excluded — their `enabled=0` is calendar rotation, not retirement. Live streams
are skipped; use `POST /api/pool/enable` for those. `?dry_run=true` to preview.
Response: `{checked, restored, still_dead, skipped_streams, dry_run}`.
```

- [ ] **Step 8: Commit**

```bash
git add bumparr/app.py tests/test_pool_recovery.py docs/API.md
git commit -m "fix: let revive clear a park it can verify, not just health

A row parked by the asset sweep (enabled=0, health='dead') was unreachable
forever: revive restored only health, and rotation requires enabled=1 AND
health='ok'. Widen the selection to enabled=0 and clear both when ffprobe
proves the file is back. on_this_day is excluded because its enabled=0 is
calendar rotation, not retirement."
```

---

### Task 2: `POST /api/pool/enable` for what revive cannot verify

**Files:**
- Modify: `bumparr/app.py` (add beside `revive`, after line ~651)
- Modify: `docs/API.md` (add after the revive entry)
- Test: `tests/test_pool_recovery.py:PoolEnable` (append to the file Task 1 created)

**Interfaces:**
- Consumes: `db.conn()`, `JSONResponse` (already imported in `app.py`).
- Produces: `enable_playable(bumper_id: str) -> dict | JSONResponse`, returning `{"id": str, "enabled": True, "changed": bool}` on success and a 404 `{"error": "not found"}` otherwise.

**Why:** a live-cam row parked by `live_cams.py:113` is `type='stream'`, which revive skips by design — there is no local file to probe. Re-adding the cam to the YAML cannot re-enable it either, because M5.3 forbids the loader from writing `enabled`. Without this endpoint that row is unreachable.

**Route ordering:** existing `POST` routes under `/api/pool/` are all literal (`tidy` line 565, `revive` line 608); the only path-parameter route is the `DELETE` at `/api/pool/kind/{kind}` (line 504). A literal `POST /api/pool/enable` cannot be shadowed. Before writing, confirm with `grep -n '@app\.\(post\|get\|delete\)("/api/pool' bumparr/app.py` that no `POST /api/pool/{param}` route has appeared.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pool_recovery.py` (reuse the same setUp by subclassing):

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_pool_recovery.PoolEnable -v`
Expected: FAIL with `AttributeError: module 'bumparr.app' has no attribute 'enable_playable'`.

- [ ] **Step 3: Add the endpoint**

In `bumparr/app.py`, directly after `revive`:

```python
@app.post("/api/pool/enable")
def enable_playable(bumper_id: str):
    """Turn one parked row back on, no questions asked.

    `enabled` is operator intent: loaders and sweeps may park a row but never
    un-park one, so something has to speak for the operator. Revive covers what
    it can physically verify; this covers what it cannot — a live cam re-added
    to the YAML, or anything the operator simply wants back. Health is left
    alone: whether the far end answers is not this endpoint's claim to make.
    """
    with db.conn() as c:
        row = c.execute("SELECT id, enabled FROM playables WHERE id=?",
                        (bumper_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        changed = not row["enabled"]
        if changed:
            c.execute("UPDATE playables SET enabled=1 WHERE id=?", (bumper_id,))
    return {"id": bumper_id, "enabled": True, "changed": changed}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest tests.test_pool_recovery -v`
Expected: all 12 PASS (8 from Task 1 are inherited and re-run by the subclass — that is intentional and harmless).

- [ ] **Step 5: Run the full suite**

Run: `python -W error::ResourceWarning -m unittest discover -s tests -v 2>&1 | tail -20`
Expected: OK.

Then: `ruff check --select F401,F821 bumparr tests`
Expected: no findings.

- [ ] **Step 6: Update `docs/API.md`**

Insert after the revive entry:

```markdown
### `POST /api/pool/enable`

Turn one parked item back on: `?bumper_id=<id>`. `enabled` is operator intent —
loaders park rows (a cam dropped from `live_cams.yaml`, a file the asset sweep
could not find) but never un-park them, so this is how you say otherwise. Use it
for live streams, which `revive` cannot verify. Health is left untouched.
Response: `{id, enabled, changed}`, or 404 `{error}` if no such id.
```

- [ ] **Step 7: Commit**

```bash
git add bumparr/app.py tests/test_pool_recovery.py docs/API.md
git commit -m "feat: add POST /api/pool/enable for explicit un-parking

A live-cam row parked by the YAML removal pass is type='stream', which revive
skips by design, and M5.3 forbids the loader from writing enabled. That left
the row unreachable. This is the operator saying so outright."
```

---

### Task 3: Correct the `load_cams` docstring

**Files:**
- Modify: `bumparr/live_cams.py:16-21`

**Interfaces:** none — documentation only. No behavior change, no test change.

**Why:** the docstring promises `health` refresh "so editing the YAML re-animates a dead cam". Since FIX_PLAN M5.3 landed, `health` resets only when `uri` changed and `enabled` is never written, so editing the YAML does *not* re-animate a disabled cam. This stale sentence is what led a reviewer to file the loader's correct behavior as a regression.

- [ ] **Step 1: Confirm no test asserts the old wording**

Run: `grep -rn "re-animate" bumparr/ tests/ docs/`
Expected: only `bumparr/live_cams.py`. If a doc elsewhere repeats the claim, fix it in this task too.

- [ ] **Step 2: Replace the docstring**

In `bumparr/live_cams.py`, replace lines 16-21:

```python
def load_cams():
    """Upsert every cam from config_files/live_cams.yaml into the registry.

    Idempotent by configured id: an existing row gets its url/weight/label/kind
    refreshed, a new one is inserted. `enabled` is operator intent and is never
    written here, so a cam switched off stays off across reloads; `health`
    resets to 'ok' only when the url actually changed, so an unchanged dead feed
    stays dead instead of being revived on every boot. Cams dropped from the
    YAML are parked (enabled=0), not deleted — history matters. Bringing a
    parked cam back is a deliberate act: POST /api/pool/enable, because
    re-adding it here cannot speak for the operator.

    Runs on startup; returns the number of rows added+updated.
    """
```

- [ ] **Step 3: Verify nothing broke**

Run: `python -m compileall -q bumparr && python -m unittest tests.test_live_cams -v`
Expected: OK, 7 tests pass, unchanged.

- [ ] **Step 4: Commit**

```bash
git add bumparr/live_cams.py
git commit -m "docs: correct load_cams docstring on enabled/health semantics

It still promised that editing the YAML re-animates a dead cam. Since M5.3 the
loader never writes enabled and resets health only on a url change. The stale
sentence got the loader's correct behavior filed as a regression in review."
```

---

### Task 4: `drop_categories` dry run lists what it will actually delete

**Files:**
- Modify: `bumparr/prune.py:147-177` (`drop_categories`)
- Modify: `docs/CLI.md:85`
- Test: `tests/test_prune_drop.py` (create)

**Interfaces:**
- Consumes: `_remove_registered(rows, extra_files=()) -> (removed, cleanup_failed)`, `_resolve(uri) -> Path | None`, `paths.resolve_kind_dir(root, kind)`.
- Produces: `drop_categories(names, apply=False) -> dict` with the **unchanged** key set `{removed, dirs, cleanup_failed}` (or `{removed, dirs, error}` on an invalid category).

**Why:** `--apply` passes every unregistered file in the category dirs to `_remove_registered`, which stages and purges them (`prune.py:45-54`). The dry run prints only registered rows plus `"would remove dir X if empty"`, and the comment at line 166 claims "Unregistered contents are not silently deleted." Behavior is correct and matches `main`; the preview and the comment are the defects.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prune_drop.py`:

```python
"""drop_categories deletes unregistered files; the dry run must say so.

--apply stages every file in the category dir, registered or not. A preview that
lists only registered rows understates an irreversible action, which is worse
than no preview at all.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from bumparr import config, db, prune


class DropCategories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "prune.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        self.junk = config.ASSET_ROOT / "junk"
        self.junk.mkdir()
        self.registered = self.junk / "known.mp4"
        self.registered.write_bytes(b"known")
        self.stray = self.junk / "operator_notes.txt"
        self.stray.write_bytes(b"mine")
        with db.conn() as c:
            c.execute("INSERT INTO playables (id,type,kind,uri,duration) VALUES (?,?,?,?,?)",
                      ("vid:junk/known.mp4", "video", "junk", "junk/known.mp4", 3))
            c.commit()

    def _run(self, apply):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = prune.drop_categories(["junk"], apply=apply)
        return out, buf.getvalue()

    def test_dry_run_lists_the_unregistered_file(self):
        _, printed = self._run(apply=False)
        self.assertIn("operator_notes.txt", printed)
        self.assertIn("unregistered", printed)

    def test_dry_run_still_lists_registered_rows(self):
        _, printed = self._run(apply=False)
        self.assertIn("junk/known.mp4", printed)

    def test_dry_run_deletes_nothing(self):
        self._run(apply=False)
        self.assertTrue(self.registered.is_file())
        self.assertTrue(self.stray.is_file())

    def test_apply_removes_both_and_the_dir(self):
        out, _ = self._run(apply=True)
        self.assertEqual(out["removed"], 1)
        self.assertFalse(self.registered.exists())
        self.assertFalse(self.stray.exists())
        self.assertFalse(self.junk.exists())

    def test_apply_clears_the_registry_row(self):
        self._run(apply=True)
        with db.conn() as c:
            self.assertIsNone(c.execute(
                "SELECT 1 FROM playables WHERE id='vid:junk/known.mp4'").fetchone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_prune_drop -v`
Expected: `test_dry_run_lists_the_unregistered_file` FAILS (the string is absent). The other four PASS — they pin the behavior you must not change.

- [ ] **Step 3: Hoist `extras` and enumerate them in the dry run**

In `bumparr/prune.py`, replace the body of the `for name in names:` loop (lines 147-177) so `extras` is computed once for both branches:

```python
    for name in names:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, uri FROM playables WHERE kind=?", (name,)).fetchall()]
        print("[prune] category %-18s %d registered entr%s"
              % (name, len(rows), "y" if len(rows) == 1 else "ies"))
        extras = [f for d in category_dirs[name] if d.is_dir()
                  for f in d.iterdir() if f.is_file()]
        if apply:
            try:
                gone, failed = _remove_registered(rows, extras)
                removed.extend(gone)
                cleanup_failed += failed
            except Exception as exc:
                print("    category transaction failed: %s" % exc)
                continue
        else:
            registered = {p for p in (_resolve(row["uri"] or "") for row in rows) if p}
            for row in rows:
                print("    would remove %s" % (row["uri"] or "")[:64])
            for extra in extras:
                if extra not in registered:
                    print("    would remove unregistered %s" % extra)
        # Dropping a category takes its whole directory: the registered rows and
        # any unregistered leftovers were both staged above, so this rmdir is
        # clearing what is now empty. The dry run lists the leftovers by name —
        # deleting a file the operator never saw in the preview is the one
        # outcome this command must not produce.
        for d in category_dirs[name]:
            if not d.is_dir():
                continue
            if apply:
                try:
                    d.rmdir()
                    dirs.append(str(d))
                except OSError:
                    pass
            else:
                print("    would remove dir %s" % d)
```

Note `_resolve` returns `None` for http/unsafe URIs — the set comprehension filters those out, mirroring the guard in `_remove_registered:43-44`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest tests.test_prune_drop -v`
Expected: all 5 PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -W error::ResourceWarning -m unittest discover -s tests -v 2>&1 | tail -20`
Expected: OK.

- [ ] **Step 6: Update `docs/CLI.md`**

Replace the `--drop-category` row (line 85):

```markdown
| `--drop-category NAME` | remove a whole category — registry rows, their files, **and any unregistered files sitting in the category directory** (repeatable). The fix for a search that returned junk. The dry run lists every file by name; read it before `--apply`. |
```

- [ ] **Step 7: Commit**

```bash
git add bumparr/prune.py tests/test_prune_drop.py docs/CLI.md
git commit -m "fix: make the drop-category dry run list unregistered files

--apply stages every file in the category dir, registered or not, but the
preview listed only registered rows and the comment claimed the opposite. The
deletion is intended and matches main; the silence was the bug."
```

---

## Self-Review

**Spec coverage.** Task 1 closes the seed-park dead end (FIX_PLAN seed item 6 leaves it open by design; nothing else re-enabled). Task 2 closes the live-cam park dead end (FIX_PLAN M5.4 parks, M5.3 forbids the loader from un-parking). Task 3 fixes the stale docstring that misrepresents M5.3. Task 4 fixes the preview gap in M1's `drop_categories`. The withdrawn `ingest.py` finding needs no task — `_reject_portrait` already unlinks.

**Placeholder scan.** No TBDs. Every code step carries the literal code; every test step carries the literal test; every run step carries the exact command and expected result.

**Type consistency.** `revive(dry_run: bool = False) -> dict` keeps its five response keys across Tasks 1 and 2. `enable_playable(bumper_id: str)` is referenced by that exact name in Task 2's tests, its implementation, both docstrings, and `docs/API.md`. `drop_categories(names, apply=False) -> dict` keeps `{removed, dirs, cleanup_failed}`. `_remove_registered(rows, extra_files=())` is called with the same two positional args it already takes.

**Invariants the tasks must not break.** `tests/test_seed.py` (3 tests), `tests/test_live_cams.py` (7 tests) and `tests/test_downloads.py` encode spec'd behavior. No task edits a loader, so all must pass untouched at every step.
