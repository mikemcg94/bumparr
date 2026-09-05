# Making the cards yours

Cards are the text bumpers — the trivia, the fake PSAs, the "please stand by".
They are the part of Bumparr with a *voice*, and the shipped ones are
deliberately plain so you replace them. This is how.

There are three ways to get cards, and they are independent — use any or all:

1. **Write your own** into `card_seeds.json`. No model, no network, full control.
2. **Let a model generate them** from prompts you can edit.
3. **Pull them from real sources** (trivia, fun-facts) — grounded, not invented.

## 1. Write your own

`bumparr/config_files/card_seeds.json` is the shipped starter set. Edit it,
delete what you don't like, add as much as you want. It is read at startup, and
registering is idempotent, so restarting after an edit adds the new ones without
duplicating the old.

Most kinds are just lines of text:

```json
{
  "psa": [
    {"lines": ["This is a test.", "It has always been a test."]},
    {"lines": ["Please remain calm.", "Calm is mandatory."]}
  ],
  "achievements": [
    {"lines": ["ACHIEVEMENT UNLOCKED", "Still Watching"]}
  ]
}
```

Each entry becomes one card. `lines` is rendered top to bottom — usually a setup
and a punchline, but one line is fine and three is fine.

Restart to pick up changes:

```bash
docker compose restart bumparr
curl -X POST http://localhost:8780/api/render/cards   # re-render to video
```

### The shapes, per kind

| Kind | Shape | Notes |
|---|---|---|
| `psa`, `corrections`, `coming_up`, `achievements` | `{"lines": [...]}` | Free text. Absurd is the point. |
| `tiny_games` | `{"lines": ["prompt", "option A", "option B"]}` | An either/or. **Do not** add an `answer` for a preference question. |
| `trivia` | `{"lines": ["question", "A ...", "B ..."], "answer": "B"}` | Unlabelled options are auto-labelled A–F; mixed/inconsistent labels are rejected; the answer maps to a letter. |
| `number` | `{"number": "8,848.86 m", "meaning": "Height of Mount Everest"}` | Must be a real, checkable figure. |
| `technical_difficulties` | `{"text": "PLEASE STAND BY", "variant": "bars"}` | `variant` is `bars`, `static`, or `nosignal`. |

`{BRAND}` in any text is replaced with your brand, so
`"{BRAND} WILL RETURN"` renders as your station name.

### The one rule

Cards are validated before they are stored, and the rules differ by kind
because the *contract with the viewer* differs:

- **Factual kinds — `trivia`, `fun_facts`, `number` — must be true.** These are
  grounded in real sources, never invented. This is not fussiness: when numbers
  were model-generated, Bumparr confidently aired the speed of light as 78 mph
  and absolute zero as +273°C. If you hand-write a number card, check it.
- **`tiny_games` must not assert an answer to a matter of taste.** "Coffee or
  tea?" has no correct answer, and a card that declares one is broken. Only a
  factual framing ("which came first?") may carry an `answer`.
- **Comedic kinds can be as absurd as you like.** `psa`, `corrections`,
  `coming_up`, `achievements` are *supposed* to make no sense. Nothing will
  "correct" them.

A card that fails validation is rejected with a reason rather than aired, so if
something you added doesn't appear, check the logs.

## 2. Generate with a model

Point Bumparr at any OpenAI-compatible endpoint and it writes cards in bulk:

```bash
LLM_BASE=http://your-model-host:8080/v1
LLM_MODEL=your-model-id
LLM_DISABLE_THINKING=1   # reasoning models otherwise spend the whole budget "thinking"
```

Then:

```bash
curl -X POST 'http://localhost:8780/api/generate/psa?n=20'
```

**The prompts are yours to edit.** They live in `PROMPTS` in
`bumparr/generators/cards.py`, one per kind. That is where the voice of your
channel actually lives — if the generated cards don't sound like your station,
change the prompt, not the model. Generated cards go through the same
structural validation, so malformed model output is rejected per item.

Kinds a model can write: `psa`, `corrections`, `coming_up`, `achievements`,
`tiny_games`.

## 3. Pull from real sources

Grounded kinds need no model at all:

```bash
curl -X POST http://localhost:8780/api/generate/trivia     # Open Trivia DB
curl -X POST http://localhost:8780/api/generate/fun_facts  # Wikipedia
curl -X POST http://localhost:8780/api/generate/number     # vendored dataset
```

`number` reads a verified local dataset, so it works with no network — it is
registered automatically at startup. `trivia` and `fun_facts` fetch on demand,
which is why they are not populated on a fresh install.

To add your own verified figures, append to
`bumparr/config_files/number_facts.json`:

```json
{"number": "299,792,458 m/s", "meaning": "Speed of light in a vacuum"}
```

## Adding a whole new kind

1. Add a renderer for it in `bumparr/render_cards.py` (start by copying the
   closest existing kind).
2. If it needs rules, add them to `bumparr/card_validation.py` — and add a test
   in `tests/test_card_validation.py`. If it is purely comedic, add it to
   `COMEDIC_KINDS` so it is not held to a factual standard.
3. To generate it with a model, add a prompt to `PROMPTS` in
   `generators/cards.py` and list the kind in the generate endpoint's
   `model_kinds`.
4. To ship starter examples, add them to `card_seeds.json` and to
   `MODEL_CARD_KINDS` in `bumparr/ingest.py` so they register at startup.

## Getting rid of ones you don't like

```bash
# disable a single card by id (from /api/bumpers)
sqlite3 data/bumparr.db "UPDATE playables SET enabled=0 WHERE id='card:psa:...';"

# drop an entire kind
sqlite3 data/bumparr.db "UPDATE playables SET enabled=0 WHERE kind='numbers_station';"
```

Disabling rather than deleting keeps the card out of rotation without losing it.
On startup, the asset scan parks missing local media and clears stale rendered-
card URIs so they can be rebuilt; `POST /api/pool/tidy` removes zero-byte files
and empty directories.
