# Bumparr documentation

| Doc | What it covers |
|---|---|
| [INTEGRATION.md](INTEGRATION.md) | Wiring Bumparr into your channel: ErsatzTV, Tunarr, Dispatcharr, anything else. |
| [CARDS.md](CARDS.md) | Making the cards yours: the shapes per kind, the model prompts, adding a whole new kind. |
| [API.md](API.md) | The full HTTP API: status, the output contract (random/fill/m3u), management actions, stream proxy, dashboard. |
| [CONFIG.md](CONFIG.md) | Every environment variable, its default, and what it does. `.env.example` is the commonly-touched subset. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How acquisition → production → registry → selection → service fit together, the file map, and the invariants to preserve. |
| [ROTATION.md](ROTATION.md) | The scoring model (base × season × daypart × recency × affinity × fatigue) and practical tuning guidance. |
| [STATION.md](STATION.md) | Running the bumper pool as live and standby HLS channels. |
| [SCHEMA.md](SCHEMA.md) | The SQLite schema: column-by-column reference, id conventions, the card lifecycle, upsert rules. |
| [CLI.md](CLI.md) | Every `python -m bumparr.…` module and flag, plus `tools/overnight.sh`. |
| [RENDERING.md](RENDERING.md) | How cards become MP4s: the pipeline, per-kind rendering, volatile cards and their TTLs. |

Read order for a first deploy: the main [README](../README.md), then
[INTEGRATION.md](INTEGRATION.md). Read order for contributors:
[ARCHITECTURE.md](ARCHITECTURE.md), then [ROTATION.md](ROTATION.md),
[STATION.md](STATION.md), and [SCHEMA.md](SCHEMA.md), then the module docstrings
(they carry the "why").
