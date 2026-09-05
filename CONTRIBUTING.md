# Contributing

Bug reports and patches welcome. A few things that will make a change land
faster, and one rule that is not negotiable.

## The rule: no invented facts

Card kinds split into three classes, and they are held to different standards:

- **Factual** (trivia, fun-facts, numbers) — must come from a real source.
  Trivia is Open Trivia DB, fun-facts are Wikipedia, numbers are a vendored
  verified dataset. A model may not invent these. This is not stylistic: model-
  invented "true obscure numbers" produced the speed of light as 78 mph and
  absolute zero as +273C, which is why the number kind is grounded now.
- **Interactive** (tiny_games) — an either/or prompt must not assert an answer.
- **Comedic** (psa, corrections, coming_up, achievements,
  technical_difficulties) — absurd and surreal is the *point*. Do not "fix"
  these for making no sense.

`bumparr/card_validation.py` enforces the structural half of this at generation
time. If you add a card kind, add its rule there and a test.

## Setup

```bash
# CI and the Docker image run Python 3.12 (see .python-version); the pinned
# pillow wheel does not build on newer interpreters, so use 3.12 explicitly.
python3.12 -m venv .venv && . .venv/bin/activate   # or: uv venv --python 3.12 .venv
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check bumparr tests                            # same rule set CI enforces
```

Run it:

```bash
cp .env.example .env      # every setting has a working default
uvicorn bumparr.app:app --port 8780
```

No model is required. If you want one, point `LLM_BASE` at any
OpenAI-compatible endpoint.

## Conventions

- **Settings are plain function-named env vars** — `LLM_BASE`, `ASSET_ROOT`,
  `WINDOW_REFRESH_HOURS`. No prefix, no product name in a variable. The
  complete reference is `docs/CONFIG.md`; when you add or change a setting,
  update that table and (if it's a commonly-touched one) `.env.example` in
  the same change.
- **Nothing personal or branded ships.** No real hostnames, IPs, paths,
  locations, or names in code, comments, or config. The default brand is the
  neutral `TV`.
- **Only redistribution-cleared assets.** Bundled fonts are SIL OFL. A
  personal-use font is a deployer's config, never a committed file. Same for
  media: Bumparr ships no video, audio, or images.
- Keep comments about *why*, not *what*.
- **Docstrings are load-bearing.** Every module and every public function
  carries one, and they explain the decision, not the mechanics. If you can't
  write the why, that's a signal to re-read the code, not to skip it.
- **Docs and code change together.** The reference docs are generated from
  the same facts as the code, so a behavior change ships with its doc update:
  endpoints → `docs/API.md`, settings → `docs/CONFIG.md` + `.env.example`,
  CLI flags → `docs/CLI.md`, schema → `docs/SCHEMA.md`, the scoring model →
  `docs/ROTATION.md`, card rendering → `docs/RENDERING.md`, station behaviour →
  `docs/STATION.md`.

- **CI has no ffmpeg/ffprobe.** Any test that reaches a subprocess must mock
  it; the station tests show the pattern in `tests/test_station_conform.py`'s
  fake process.

## Tests

`python -m unittest discover -s tests`. CI additionally boots the app with **no
model configured** and asserts cards are still produced in every kind — if a
change makes Bumparr require a model, CI fails, by design.

## Pull requests

Say what broke and how you verified the fix. A test that fails before your
change and passes after is the most persuasive thing you can include.
