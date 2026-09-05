# Easy-Wins Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land four low-risk, independently shippable improvements found by a repo survey on 2026-09-05: a Docker build-context ignore file, a repo-pinned ruff/Python toolchain so local checks match CI, a safe lint cleanup, and one shared ffmpeg frame-pipe helper replacing two copies of the same 35-line block.

**Architecture:** Every task is behavior-preserving. Nothing here changes an endpoint, a setting, a card rule, or a scheduler weight. The only code refactor (Task 4) is gated by the pre-existing `tests/test_processes.py`, which already asserts the kill/reap/partial-removal contract for both call sites.

**Tech Stack:** Python 3.12 (CI and Docker both pin it), FastAPI, `unittest` (not pytest), `ruff==0.16.1`, Node built-in test runner, Docker.

**Spec:** This document is its own spec. The survey evidence is in the "Why these four" section below; `docs/FIX_PLAN.md` is fully checked off and is not reopened.

## Why these four (survey evidence)

| Finding | Evidence | Fix |
|---|---|---|
| No `.dockerignore` | `Dockerfile` copies only `requirements.txt` and `bumparr/`, but the build context is the whole checkout. `.gitignore` lists `data/` and `assets/` at repo root, so a dev who has run locally ships their entire media library and SQLite DB to the daemon on every `docker compose up --build`. Repo is 97 MB before any media lands. | Task 1 |
| Local `ruff check` disagrees with CI | CI runs `ruff check --select F401,F821`. With no repo config, a bare `ruff check` reports 452 findings from whatever user-level config is present. Contributors cannot tell which findings block a PR. | Task 2 |
| Interpreter drift breaks setup | `CONTRIBUTING.md` says `python -m venv`. On a host whose `python3` is 3.14, `pillow==11.1.0` has no wheel and the install fails; 15 test modules then error on `ModuleNotFoundError: fastapi`. CI and Docker are both 3.12. | Task 2 |
| Safe-fix lint debt in non-critical spots | `ruff check --isolated`: 18 unsorted imports, 3 regex-flag aliases, 2 static splits, 1 `object` base, 3 set-comprehension rewrites, 2 unused unpacked `reason` vars in tests, 1 loop-variable closure (`tests/test_downloads.py:83`, B023), 1 needless-bool return, 1 unused `_repo` alias in `config.py`. All auto-fixable or one-liners. | Task 3 |
| Duplicated ffmpeg pipe block | `station_ids.py:82-117` and `render_cards.py:848-881` are the same Popen→write frames→close stdin→communicate(timeout)→kill/reap/unlink→raise sequence, already drifting (300s vs 600s timeout, 500 vs 600 byte stderr tail). | Task 4 |

Things surveyed and **deliberately not** in this plan: the 13 `except Exception: pass` sites (most carry a why-comment and are genuine best-effort cleanup); `date.today()` in `seasons.py` (container sets `TZ`); `print()` vs `logging` split (only `error`/`warning`/`exception` levels are used, so nothing is silently dropped; a migration is not an easy win); dependency freshening (`fastapi==0.115.6` is a year old, but bumps need their own validation pass).

## Global Constraints

- **A teammate is committing to this branch in parallel.** Their working set is `bumparr/app.py`, `bumparr/live_cams.py`, `bumparr/prune.py`, `bumparr/generators/on_this_day.py`, `bumparr/web/*`, `docs/API.md`, `tests/test_pool_recovery.py`, `tests/test_prune_drop.py`. **Do not modify any of those files.** If a task's lint fix would land in one of them, skip that fix and say so.
- **Test runner is `unittest`, not pytest.** Run with the 3.12 venv at `/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin/python` (already provisioned with `requirements.txt` + `requirements-dev.txt`). Full gate: `<venv>/python -W error::ResourceWarning -m unittest discover -s tests`. Expected baseline: `Ran 248 tests ... OK`.
- **CI does not install ffmpeg/ffprobe.** Any test touching a path that shells out MUST mock `subprocess.Popen`/`subprocess.run`.
- **Do not add suppression directives** (`# noqa`, `# type: ignore`, `per-file-ignores` for real findings). Fix the root cause or leave it and report.
- **Comment/docstring style:** explain *why*, not *what*. Every module and public function carries a docstring. Match surrounding density.
- **Dispatched workers do not run `git add` or `git commit`.** The orchestrator reviews and commits each task with a conventional prefix (`chore:`, `refactor:`, `ci:`, `docs:`).
- Full final gate for every task (run from repo root):

```bash
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/python -m compileall -q bumparr
$V/python -W error::ResourceWarning -m unittest discover -s tests
$V/ruff check --select F401,F821 bumparr tests
node --check bumparr/web/app.js && node --test bumparr/web/app.test.js
```

## File Structure

| File | Task | Change |
|---|---|---|
| `.dockerignore` | 1 | **Create.** Excludes VCS, caches, venvs, runtime data, docs, tests from the build context |
| `ruff.toml` | 2 | **Create.** `target-version`, the CI rule set, `line-length` off |
| `.python-version` | 2 | **Create.** `3.12` |
| `.github/workflows/ci.yml` | 2 | Modify: ruff step reads config; `cache: pip` on setup-python |
| `CONTRIBUTING.md:24-29` | 2 | Modify: setup uses an explicit 3.12 interpreter; explain why |
| `bumparr/config.py:26` | 3 | Remove unused `_repo` alias |
| `bumparr/card_validation.py:59` | 3 | Return the condition directly |
| `bumparr/generators/cards.py`, `enrich_bg.py`, `grounded.py`, `bumparr/ingest.py`, `produce.py`, `rotation.py`, `sources/capture_windows.py` | 3 | Safe autofixes only (I001, FURB167, SIM905, UP004, C401) |
| `tests/test_downloads.py:83` | 3 | Bind loop variable via default argument |
| `tests/test_card_validation.py:42,49` | 3 | Drop unused `reason` unpack |
| `tests/*.py` (except the two teammate files) | 3 | Import sorting only |
| `bumparr/ffmpeg_pipe.py` | 4 | **Create.** `encode_frames(cmd, frames, dest, *, timeout, tail)` |
| `bumparr/station_ids.py:82-117` | 4 | Call the helper; also the RUF046 `int(round())` at line 69 |
| `bumparr/render_cards.py:848-881` | 4 | Call the helper; also RUF046 at lines 142 and 828 |
| `tests/test_processes.py` | 4 | Existing tests stay as-is; add direct helper tests |

---

### Task 1: Docker build-context ignore

**Files:**
- Create: `.dockerignore`

**Interfaces:** none.

- [ ] **Step 1: Record the current build-context size**

```bash
cd /home/chris/git/bumparr
docker build -q --no-cache -t bumparr-ctx-before . 2>&1 | tail -1
docker history bumparr-ctx-before --no-trunc --format '{{.Size}} {{.CreatedBy}}' | head -3
```

Just note the numbers. The image content is unaffected by this task (only `requirements.txt` and `bumparr/` are copied), so this is a sanity check that the build still succeeds afterward, not a size diff.

- [ ] **Step 2: Create `.dockerignore`**

```gitignore
# Docker build context. The Dockerfile copies only requirements.txt and
# bumparr/, but everything under the context is still sent to the daemon.
# A developer who has run locally has data/ and assets/ at the repo root
# (see .gitignore), which is the entire media library plus the SQLite DB.

# VCS and editor state
.git
.gitignore
.github
.DS_Store

# Python build/cache/venv (Docker matches patterns from the context root,
# so nested caches need the **/ prefix)
**/__pycache__
**/*.py[cod]
*.egg-info
.venv
venv
.ruff_cache
.pytest_cache
.mypy_cache

# Runtime data: the deployer's, never the image's
data
assets
*.db
*.db-wal
*.db-shm
.cache

# Not part of the runtime image
docs
tests
CHANGELOG.md
CONTRIBUTING.md
README.md
LICENSE
docker-compose.yml
Dockerfile
.env
.env.example
```

Keep `bumparr/config_files/`, `bumparr/fonts/`, `bumparr/web/` and every `*.json`/`*.yaml` under `bumparr/` in: they are inside the copied tree and are runtime data.

- [ ] **Step 3: Verify the context excludes the right things**

```bash
cd /home/chris/git/bumparr
# Dry-run the context: build a throwaway stage that lists what arrived.
printf 'FROM busybox\nCOPY . /ctx\nRUN find /ctx -maxdepth 2 | sort\n' > /tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/Dockerfile.ctx
docker build --no-cache --progress=plain -f /tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/Dockerfile.ctx . 2>&1 | grep -E '^#[0-9]+ [0-9.]+ /ctx' | sed 's/^#[0-9]* [0-9.]* //'
```

Expected: `/ctx/bumparr`, `/ctx/requirements.txt`, `/ctx/requirements-dev.txt` and the `bumparr/*` subtree. **Not** present: `/ctx/.git`, `/ctx/docs`, `/ctx/tests`, `/ctx/data`, `/ctx/assets`, any `__pycache__`.

- [ ] **Step 4: Confirm the real image still builds**

```bash
docker build -q . && echo BUILD-OK
```

Expected: `BUILD-OK`.

- [ ] **Step 5: Commit (orchestrator)**

```bash
git add .dockerignore
git commit -m "chore: add .dockerignore so local data/assets never enter the build context"
```

---

### Task 2: Pin the toolchain the repo actually targets

**Files:**
- Create: `ruff.toml`
- Create: `.python-version`
- Modify: `.github/workflows/ci.yml` (setup-python step and the ruff step)
- Modify: `CONTRIBUTING.md:24-29` (Setup block)

**Interfaces:**
- Produces: `ruff check bumparr tests` with no flags is now the canonical lint command; Task 3 relies on it.

- [ ] **Step 1: Confirm the current mismatch**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/ruff check --select F401,F821 bumparr tests   # expected: All checks passed!
$V/ruff check --isolated bumparr tests | tail -1  # expected: Found 452 errors (or similar, non-zero)
```

- [ ] **Step 2: Create `ruff.toml`**

```toml
# The repo's lint contract. CI runs `ruff check bumparr tests` with exactly
# this file, so a clean local run means a clean CI run. The rule set is
# deliberately narrow: undefined names and dead imports are the two classes
# that have shipped real bugs here (see docs/FIX_PLAN.md Phase 5). Widen it
# in its own change, with the resulting fixes, not as a side effect.
target-version = "py312"
line-length = 120

[lint]
select = ["F401", "F821"]
```

- [ ] **Step 3: Create `.python-version`**

```
3.12
```

(One line, trailing newline.) `uv`, `pyenv`, and `actions/setup-python` all read this file.

- [ ] **Step 4: Update the CI workflow**

In `.github/workflows/ci.yml`, change the setup-python step from:

```yaml
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with:
          python-version: "3.12"
```

to:

```yaml
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with:
          python-version-file: ".python-version"
          cache: pip
```

and change the ruff step from:

```yaml
      - name: Dead-code and undefined-name check
        run: ruff check --select F401,F821 bumparr tests
```

to:

```yaml
      # Rule set lives in ruff.toml so local `ruff check` and CI agree.
      - name: Dead-code and undefined-name check
        run: ruff check bumparr tests
```

Do not touch any other step. Do not change the pinned action SHAs.

- [ ] **Step 5: Update CONTRIBUTING.md Setup**

Replace the block at lines 24-29:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

with:

```bash
# CI and the Docker image run Python 3.12 (see .python-version); the pinned
# pillow wheel does not build on newer interpreters, so use 3.12 explicitly.
python3.12 -m venv .venv && . .venv/bin/activate   # or: uv venv --python 3.12 .venv
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check bumparr tests                            # same rule set CI enforces
```

Leave the rest of the file alone.

- [ ] **Step 6: Verify**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/ruff check bumparr tests            # expected: All checks passed!
$V/ruff check --show-settings bumparr/config.py | grep -E 'linter.target_version|linter.rules.enabled' -A3 | head -8
cat .python-version
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml parses')"
```

Expected: ruff passes; settings show `py312` and only F401/F821 enabled; `3.12`; `ci.yml parses`. (If `yaml` is missing from system python, use `$V/python` — pyyaml is in requirements.)

- [ ] **Step 7: Commit (orchestrator)**

```bash
git add ruff.toml .python-version .github/workflows/ci.yml CONTRIBUTING.md
git commit -m "ci: pin ruff rules and Python 3.12 in-repo so local checks match CI"
```

---

### Task 3: Safe lint cleanup outside the teammate's files

**Files:**
- Modify: `bumparr/config.py:26-27`
- Modify: `bumparr/card_validation.py:55-60`
- Modify: `tests/test_downloads.py:79-88`
- Modify: `tests/test_card_validation.py:42,49`
- Modify (autofix only): `bumparr/generators/cards.py`, `bumparr/generators/enrich_bg.py`, `bumparr/generators/grounded.py`, `bumparr/ingest.py`, `bumparr/produce.py`, `bumparr/rotation.py`, `bumparr/sources/capture_windows.py`, and `tests/test_card_seeds.py`, `tests/test_cards_gen.py`, `tests/test_fetch_queue.py`, `tests/test_jobs.py`, `tests/test_paths.py`, `tests/test_render_hardening.py`, `tests/test_stream_proxy.py`, `tests/test_validation_extra.py`

**Interfaces:** none. **Excluded on purpose:** `bumparr/app.py` (teammate), `bumparr/station_ids.py` and `bumparr/render_cards.py` (Task 4 owns them).

- [ ] **Step 1: Baseline**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/python -W error::ResourceWarning -m unittest discover -s tests 2>&1 | tail -3   # Ran 248 ... OK
```

- [ ] **Step 2: Apply the safe autofixes to the allowed files only**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/ruff check --isolated --select I001,FURB167,SIM905,UP004,C401 --fix \
  bumparr/config.py bumparr/card_validation.py \
  bumparr/generators/cards.py bumparr/generators/enrich_bg.py bumparr/generators/grounded.py \
  bumparr/ingest.py bumparr/produce.py bumparr/rotation.py bumparr/sources/capture_windows.py \
  tests/test_card_seeds.py tests/test_cards_gen.py tests/test_card_validation.py tests/test_downloads.py \
  tests/test_fetch_queue.py tests/test_jobs.py tests/test_paths.py tests/test_render_hardening.py \
  tests/test_stream_proxy.py tests/test_validation_extra.py
git status --short   # must NOT list app.py, live_cams.py, prune.py, on_this_day.py, web/, station_ids.py, render_cards.py
```

`C401` is only safe-fixable when ruff marks it so; if any C401 remains, rewrite `set(x for x in ...)` as `{x for x in ...}` by hand.

- [ ] **Step 3: Hand fixes**

`bumparr/config.py` lines 26-27 currently read:

```python
_repo = _here.parent  # repo root (parent of bumparr/)
_root = _here.parent
```

`_repo` is never referenced. Replace both lines with:

```python
_root = _here.parent  # repo root (parent of bumparr/)
```

`bumparr/card_validation.py` lines 58-60 currently read:

```python
    if re.search(r"\[[^\]]*\.$", t):          # bracket cut: "... [Cap."
        return True
    return False
```

Replace with:

```python
    return bool(re.search(r"\[[^\]]*\.$", t))  # bracket cut: "... [Cap."
```

`tests/test_downloads.py` line 80: the inner `run` closes over the loop variable `small`. It happens to work because `run` is called before the next iteration, but bind it explicitly so the test cannot silently start testing the wrong branch:

```python
        for small in (False, True):
            def run(cmd, small=small, **kwargs):
```

`tests/test_card_validation.py` lines 42 and 49: `ok, reason = ...` where `reason` is unused. Change to `ok, _ = ...` on both lines.

- [ ] **Step 4: Verify**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/python -m compileall -q bumparr
$V/python -W error::ResourceWarning -m unittest discover -s tests 2>&1 | tail -3   # Ran 248 ... OK
$V/ruff check bumparr tests                                                     # All checks passed!
$V/ruff check --isolated --select I001,FURB167,SIM905,UP004,C401,B023,RUF059,SIM103 \
  --exclude bumparr/app.py --exclude bumparr/station_ids.py --exclude bumparr/render_cards.py \
  --exclude bumparr/live_cams.py --exclude bumparr/prune.py --exclude bumparr/generators/on_this_day.py \
  --exclude tests/test_pool_recovery.py --exclude tests/test_prune_drop.py bumparr tests   # All checks passed!
git diff --stat
```

Read the diff. Every hunk must be an import reorder, a regex-flag rename (`re.I` → `re.IGNORECASE` etc.), a `"a b".split()` → list literal, a `class X(object)` → `class X`, a set comprehension, or one of the four hand fixes. Anything else: revert that hunk.

- [ ] **Step 5: Commit (orchestrator)**

```bash
git add -- bumparr/config.py bumparr/card_validation.py bumparr/generators bumparr/ingest.py bumparr/produce.py bumparr/rotation.py bumparr/sources/capture_windows.py tests/test_card_seeds.py tests/test_cards_gen.py tests/test_card_validation.py tests/test_downloads.py tests/test_fetch_queue.py tests/test_jobs.py tests/test_paths.py tests/test_render_hardening.py tests/test_stream_proxy.py tests/test_validation_extra.py
git commit -m "chore: apply safe ruff autofixes and drop unused bindings"
```

---

### Task 4: One ffmpeg frame-pipe helper for both raw-video encoders

**Files:**
- Create: `bumparr/ffmpeg_pipe.py`
- Modify: `bumparr/station_ids.py:69` (RUF046) and `:82-117` (`_render_still` body after `cmd` is built)
- Modify: `bumparr/render_cards.py:142,828` (RUF046) and `:848-881` (`_encode_frames` body after `cmd` is built)
- Test: `tests/test_processes.py` (existing tests unchanged; add a `FramePipe` class)

**Interfaces:**
- Produces:

```python
def encode_frames(cmd, frames, dest, *, timeout, tail=600):
    """Feed raw frames to an ffmpeg command over stdin and land `dest`, or land nothing.

    `cmd` is the full argv (ffmpeg reading rawvideo on pipe:0, writing `dest`).
    `frames` is an iterable of `bytes`, pulled lazily so the caller draws each
    frame only when ffmpeg is ready for it. Returns None on success. Raises
    RuntimeError, with `dest` already removed, when ffmpeg exits non-zero or
    exceeds `timeout` seconds after the last frame; the message ends with the
    last `tail` bytes of ffmpeg's stderr so the caller can log the cause.
    """
```

Both callers must be tested through their **public** entry points exactly as `tests/test_processes.py` already does (`render_cards._encode_frames(...)` and `station_ids._render_still(...)`), which patch `subprocess.Popen` on the shared `subprocess` module object, so the patch reaches the new module without changes.

- [ ] **Step 1: Baseline the existing process tests**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/python -m unittest tests.test_processes -v 2>&1 | tail -8   # 4 tests, OK
```

- [ ] **Step 2: Write the failing helper tests**

Append to `tests/test_processes.py` (reuse the module's existing `_Input` and `_Process` fakes; do not redefine them):

```python
class FramePipe(unittest.TestCase):
    """The shared helper owns the kill/reap/unlink contract both encoders rely on."""

    def test_timeout_kills_reaps_and_removes_partial(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process()
            with mock.patch.object(subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg timed out"):
                    ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1)
            self.assertTrue(process.killed)
            self.assertEqual(process.calls, 2)
            self.assertFalse(dest.exists())

    def test_nonzero_exit_removes_partial_and_reports_tail(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process(timeout=False, returncode=1)
            with mock.patch.object(subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "bounded diagnostic tail"):
                    ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1)
            self.assertFalse(dest.exists())

    def test_success_drains_stderr_once_and_keeps_dest(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp4"
            dest.write_bytes(b"encoded")
            process = _Process(timeout=False, returncode=0)
            with mock.patch.object(subprocess, "Popen", return_value=process):
                self.assertIsNone(ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1))
            self.assertEqual(process.calls, 1)
            self.assertTrue(dest.exists())

    def test_frames_are_pulled_lazily(self):
        from bumparr import ffmpeg_pipe
        pulled = []

        def gen():
            for i in range(3):
                pulled.append(i)
                yield b"\x00"
        process = _Process(timeout=False, returncode=0)
        with mock.patch.object(subprocess, "Popen", return_value=process):
            ffmpeg_pipe.encode_frames(["ffmpeg"], gen(), "unused.mp4", timeout=1)
        self.assertEqual(pulled, [0, 1, 2])
```

- [ ] **Step 3: Run them to confirm they fail**

```bash
$V/python -m unittest tests.test_processes.FramePipe 2>&1 | tail -3
```

Expected: 4 errors, `ModuleNotFoundError: No module named 'bumparr.ffmpeg_pipe'`.

- [ ] **Step 4: Create `bumparr/ffmpeg_pipe.py`**

```python
"""Feed drawn frames to ffmpeg over stdin without ever leaving a wedged
process or a half-written file behind.

Station IDs and rendered cards both encode this way, and both once carried
their own copy of the same sequence. The copies had already drifted (one
waited 300s, the other 600s), and the sequence is subtle enough that a
drift is a bug: stdin has to be closed *before* waiting so ffmpeg can flush,
stderr has to be drained *while* waiting so a chatty run cannot deadlock the
pipe, and a timeout has to kill, reap, and unlink so neither a zombie nor a
truncated MP4 survives to be registered as a playable.
"""
import subprocess
from pathlib import Path


def encode_frames(cmd, frames, dest, *, timeout, tail=600):
    """Feed raw frames to an ffmpeg command over stdin and land `dest`, or land nothing.

    `cmd` is the full argv (ffmpeg reading rawvideo on pipe:0, writing `dest`).
    `frames` is an iterable of `bytes`, pulled lazily so the caller draws each
    frame only when ffmpeg is ready for it. Returns None on success. Raises
    RuntimeError, with `dest` already removed, when ffmpeg exits non-zero or
    exceeds `timeout` seconds after the last frame; the message ends with the
    last `tail` bytes of ffmpeg's stderr so the caller can log the cause.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        for frame in frames:
            proc.stdin.write(frame)
    except (BrokenPipeError, ValueError):
        pass                      # ffmpeg exited early; its stderr has the reason
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.stdin = None         # so communicate() does not try to close it again
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        _, err = proc.communicate()
        _unlink(dest)
        raise RuntimeError("ffmpeg timed out: %s" % _tail(err, tail)) from exc
    if proc.returncode != 0:
        _unlink(dest)
        raise RuntimeError(_tail(err, tail) or "ffmpeg exited %s" % proc.returncode)


def _tail(err, n):
    return (err or b"").decode("utf-8", "ignore")[-n:]


def _unlink(dest):
    # The partial output is the hazard; a missing file is the goal, not an error.
    try:
        Path(dest).unlink()
    except OSError:
        pass
```

- [ ] **Step 5: Run the helper tests**

```bash
$V/python -m unittest tests.test_processes.FramePipe -v 2>&1 | tail -3   # 4 tests OK
```

- [ ] **Step 6: Switch `station_ids._render_still` to the helper**

In `bumparr/station_ids.py`, replace everything in `_render_still` from the `proc = subprocess.Popen(` line (line 82) through the final `raise RuntimeError(err.decode("utf-8", "ignore")[-500:])` (line 117) with:

```python
    def frames():
        for i in range(n):
            t = i / float(FPS)
            if spec is not None:
                face = brandslam.face_at(spec, t, slam_at)
            else:
                face = static_face if t >= slam_at else None
            yield brandslam.draw(plate.copy(), brand, face, size).tobytes()

    ffmpeg_pipe.encode_frames(cmd, frames(), dest, timeout=300, tail=500)
```

Add `ffmpeg_pipe` to the existing `from bumparr import brandslam, config, db` import (keep alphabetical: `brandslam, config, db, ffmpeg_pipe`). Keep `import subprocess` only if something else in the module still uses it; `grep -n subprocess bumparr/station_ids.py` and remove the import if the only remaining use was this block. (`tests/test_processes.py` patches `station_ids.subprocess.Popen`; if you remove the import that test breaks, so in that case keep `import subprocess` and leave a one-line comment that the process tests patch through it. Prefer keeping it.)

Also fix line 69 RUF046: it is `int(round(x))`. `round()` with no `ndigits` already returns an `int`, so drop the redundant `int(...)` wrapper and leave `round(x)`.

- [ ] **Step 7: Switch `render_cards._encode_frames` to the helper**

In `bumparr/render_cards.py`, replace everything in `_encode_frames` from `proc = subprocess.Popen(` (line 848) through the closing `"ffmpeg exited %s" % proc.returncode)` (line 881) with:

```python
    def frames():
        for i in range(n):
            img = frame_fn(i / float(fps))
            if img.mode != "RGB":
                img = img.convert("RGB")
            yield img.tobytes()

    ffmpeg_pipe.encode_frames(cmd, frames(), dest, timeout=600, tail=600)
```

Add `ffmpeg_pipe` to the module's `from bumparr import ...` line (alphabetical). Same `subprocess` import rule as Step 6; `render_cards` uses `subprocess.run` elsewhere (`_encode_noise`, probes), so the import certainly stays.

Fix RUF046 at lines 142 and 828 the same way: `int(round(x))` → `round(x)`.

- [ ] **Step 8: Full verification**

```bash
cd /home/chris/git/bumparr
V=/tmp/claude-1000/-home-chris-git-bumparr/282e37da-11ea-4168-96c9-72036d461e7c/scratchpad/venv/bin
$V/python -m compileall -q bumparr
$V/python -m unittest tests.test_processes -v 2>&1 | tail -12        # 8 tests OK (4 old + 4 new)
$V/python -W error::ResourceWarning -m unittest discover -s tests 2>&1 | tail -3   # Ran 252 ... OK
$V/ruff check bumparr tests                                          # All checks passed!
$V/ruff check --isolated --select RUF046 bumparr/station_ids.py bumparr/render_cards.py
git diff --stat   # only station_ids.py, render_cards.py, ffmpeg_pipe.py, tests/test_processes.py
```

- [ ] **Step 9: Commit (orchestrator)**

```bash
git add bumparr/ffmpeg_pipe.py bumparr/station_ids.py bumparr/render_cards.py tests/test_processes.py
git commit -m "refactor: share one ffmpeg frame-pipe helper between station IDs and card renders"
```

---

## Landing order

Tasks 1–4 touch disjoint files and can run in parallel. Commit in numeric order after review. No CHANGELOG entry: none of these change user-visible behavior.

## Execution notes (2026-09-05)

All four tasks landed as separate commits on `fix/comprehensive-review-remediation`. Two deviations from the text above, both made at review:

- **Task 3:** the SIM905 rewrite of the two stop-word sets was reverted. The multi-line `"...".split()` literal reads better than the 500-character list ruff produces; SIM905 is not part of the enforced rule set anyway.
- **Task 4:** `station_ids.py` no longer imports `subprocess` at all. Rather than re-export the module from the helper to keep the old patch target alive, `tests/test_processes.py` now patches `subprocess.Popen` on the module object directly, which reaches every caller.
