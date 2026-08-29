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
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
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
  `SHARE_STREAM`. No prefix, no product name in a variable.
- **Nothing personal or branded ships.** No real hostnames, IPs, paths,
  locations, or names in code, comments, or config. The default brand is the
  neutral `TV`.
- **Only redistribution-cleared assets.** Bundled fonts are SIL OFL. A
  personal-use font is a deployer's config, never a committed file. Same for
  media: Bumparr ships no video, audio, or images.
- Keep comments about *why*, not *what*.

## Tests

`python -m unittest discover -s tests`. CI additionally boots the app with **no
model configured** and asserts cards are still produced in every kind — if a
change makes Bumparr require a model, CI fails, by design.

## Pull requests

Say what broke and how you verified the fix. A test that fails before your
change and passes after is the most persuasive thing you can include.
