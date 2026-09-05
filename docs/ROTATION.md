# How rotation works

What plays next is decided by one scoring model, in `bumparr/rotation.py`:

```text
score = base × season × daypart × recency × affinity × fatigue
```

Every Bumparr path that probabilistically ranks candidates —
`/api/bumpers/random` and a co-deployed player — uses
`rotation.weights_for`. `/api/bumpers/fill` instead optimizes for duration,
and `/playlist.m3u` lists the full playable set for its downstream scheduler.
This page is the user-facing version of the module docstring; the code is the
authority and the two should be read together.

## Declared vs computed

The design split is the whole point. **One number is declared, never touched
by the system; five are computed fresh at every selection, never written back.**

| Factor | Where it comes from | Meaning |
|---|---|---|
| `base` | the `weight` column (and `payload.base_weight` for produced clips) | editorial intent — how much this material is wanted at all |
| `season` | `seasons.py`, from `config_files/seasons.yaml` | is this category appropriate for today's date |
| `daypart` | `dayparts.py`, from `config_files/dayparts.yaml` | does this kind belong to this hour of the local day |
| `recency` | `last_played` | did THIS clip just play — deep drop, slow recovery |
| `affinity` | most recent play of the same *kind* | did something LIKE it just play — shallower drop, quicker recovery |
| `fatigue` | `play_count` vs the pool's median | has it been overplayed across its whole life |

Because nothing is written back, the model is idempotent and explainable after
the fact: adjust a curve and behaviour changes immediately, with no migration
of stored state. A row's stored weight is exactly what you declared it to be.
(`seasons.restore_base_weights` still exists to heal rows from the older
in-place version — see the module docstring.)

`base <= 0` means *deliberately off air*. A season factor can independently
gate a category off for the current date, which is different from merely
unlikely.

## The curves

All three recovery/penalty curves are of the form `t / (t + tau)`: zero at
age 0, 0.5 at `tau`, approaching 1 without ever arriving. The shape is
deliberate — recovery is fast at first, then slow — so a clip is eligible
again reasonably soon but does not fully reset for a long while. That is what
makes a rotation feel like a rotation rather than a shuffle.

- **Recency** (the strongest signal): just played ≈ out of the running; it
  takes hours to come back. An item never played scores 1.0, so new material
  gets on air promptly instead of losing a lottery against a pool of hundreds.
- **Affinity**: two traffic cams back to back is a *texture* problem, not a
  repetition problem — so the category steps aside briefly (shallower floor,
  faster recovery) instead of leaving.
- **Fatigue**: judged against the pool's own median play count, not an
  absolute number, so it scales with library size. Square-rooted and clamped,
  so an overplayed item is roughly halved at four times the median —
  discounted, never silenced.

## Seasonality

`seasons.py` supplies the `season` factor per category, computed at selection
time from `config_files/seasons.yaml` (MM-DD windows, year-wrap safe). It
ramps rather than switches — real channels tease a holiday for weeks and drop
it overnight afterwards — and in-season categories are boosted above 1.0 on
purpose: October ought to feel like October, which means Halloween material
*beating* the everyday pool, not merely joining it. A category can own
several dates (`windows:`), which is how fireworks belong to both New Year and
Independence Day.

## Dayparts

`dayparts.py` supplies the `daypart` factor per kind from
`config_files/dayparts.yaml`: named windows of the local day, each with its
own kind multipliers. Like the season factor it is computed at selection time
and never stored. Outside every window the factor is 1.0 for everything. The
same windows title the station's guide blocks (see the station docs), which
is why each carries a viewer-facing description.

## Tuning

All parameters are environment variables with the defaults below — see
[CONFIG.md](CONFIG.md) for the full table. Practical guidance:

- **"The pool feels small."** Recency is usually the cause: raise
  `RECENCY_TAU` so clips come back sooner.
- **"Everything looks the same."** Lower `AFFINITY_FLOOR` / shorten
  `AFFINITY_TAU` so categories take a bigger step aside after airing.
- **"One clip keeps winning."** Raise `FATIGUE_STRENGTH`, or check its
  declared `weight` — fatigue is relative to the median, so an unusually high
  base will out-pull the penalty.
- **"Holiday stuff shows up at the wrong time."** Edit
  `config_files/seasons.yaml` (windows, `lead_in`, `tail`, `off_weight`);
  `python -m bumparr.seasons --preview` shows each category's curve across
  the year.

`rotation.describe_settings()` prints the active curve parameters for status
output, and `rotation.explain(item, ctx)` breaks down every factor behind a
specific score — the tool for "why did that play?".

## What this model does not do

- It does not schedule. It returns a ranking; the consumer (or the fill
  endpoint's subset-sum) decides the actual sequence.
- It does not enforce type quotas. There are no per-type shares; the mix comes
  from each type's own weights and availability. (Type composition is an
  editorial decision made at the weight column, not by the model.)
- It does not remember per-consumer state. History is per-channel (`playout` /
  `play_history`), so one pool can serve several consumers.
