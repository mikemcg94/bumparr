# Bumparr

**A bumper generator for the \*arr stack.** Point it at your sources and it builds
and maintains a self-refreshing pool of TV bumpers / interstitials — station IDs,
"please stand by" cards, trivia, live "window" cams, public-domain clips, ambient
loops — that any channel generator (Dispatcharr, ErsatzTV, Tunarr) or player can
consume.

Bumparr does the one thing the \*arr ecosystem doesn't: turn source material into
finished, varied bumpers, automatically. It is not a channel scheduler — it
produces the interstitials; something downstream decides what airs and when.

## Run it

```bash
cp .env.example .env      # set your storage paths (optional; sane defaults)
docker compose up -d
# dashboard: http://localhost:8780
```

Runs as its own service on port `8780`, next to the rest of your stack.

> **Note:** The live cams that ship enabled are open, direct-HLS feeds — no key,
> no scraping, genuinely real-time. Bumparr can *also* capture YouTube-backed
> cams, but ships none enabled: whether to scrape YouTube is your call to make
> for your own install, not this project's to make for you. See
> `bumparr/config_files/live_cams.yaml` for a commented template.

## What it produces

Bumpers are "playables" of several types, all served from one pool:

- **video** — public-domain clips (Internet Archive), NASA/ambient loops, vintage
  station IDs, cartoons, your own media
- **stream** — live "window" cams (open DOT traffic cams play truly live;
  YouTube-backed cams are opt-in, captured as fresh looping snippets)
- **card** — generated text cards: trivia & fun-facts (**grounded in real sources**,
  not model hallucination), surreal PSAs, "we'll be right back", unexplained
  numbers, on-this-day, and more
- **image** — timed image cards

Everything is configurable — sources, cams, fonts, brand, and the local model
endpoint — via `.env` and `config_files/`. No code changes to adopt.

**A local model is optional.** Grounded cards (trivia, fun-facts, numbers) come
from real sources, and the procedural kinds plus every other kind ship with a
small built-in starter set, so a fresh install with no model still has content in
every kind. Point Bumparr at any OpenAI-compatible endpoint to generate far more,
in your own voice — the model diversifies the pool, it is not required to run.

## Render cards to video first

Video, stream and image bumpers are files (or live URLs) the moment they land.
**Text cards are not** — they start life as structured content, so they have to be
rendered before anything outside Bumparr can play them:

```bash
curl -X POST http://localhost:8780/api/render/cards          # or --limit N in batches
docker compose exec bumparr python -m bumparr.render_cards   # same thing, with progress
```

This lays each card out and encodes a 1080p30 H.264 MP4 with a silent AAC track,
timed answer/brand reveals included. It is offline and idempotent — already-rendered
cards are skipped, so it is safe to run on a schedule after generating new ones.
Until a card is rendered it stays out of `/playlist.m3u` and its `media_url` is
`null`, which is deliberate: Bumparr never advertises something a consumer cannot play.

Two kinds are intentionally never rendered. `weather` and `local_time` display live
values, so a frozen file would air stale data; they remain player-only.

## Consume it

Three ways for a channel generator or player to pull bumpers:

| Endpoint | Use |
|---|---|
| `GET /api/bumpers/random?count=N&max_duration=S&types=video,card` | Hand me N bumpers to drop between shows |
| `GET /playlist.m3u` | An M3U of every playable bumper (video, stream, rendered cards) for IPTV tooling |
| `GET /media/<path>` | The actual media files |

Plus `GET /api/status`, `GET /api/bumpers`, `POST /api/render/cards`, and
`POST /api/generate/<kind>` / `POST /api/sources/<action>` to drive it from the
dashboard or scripts.

Every URL Bumparr hands out is absolute, because the thing that fetches a playlist
is rarely the thing that plays its entries. Behind a reverse proxy, set
`PUBLIC_URL` to the address consumers actually reach; otherwise Bumparr
derives it from the incoming request.

Also `GET /api/bumpers/fill?seconds=N`, which hands back a *set* of bumpers that
adds up to a gap — solved as a subset-sum rather than a greedy pass, so a break
doesn't end in dead air. That is the contract a channel generator actually
needs, and nothing else in the \*arr ecosystem offers it.

**→ [Wiring Bumparr into your channel](docs/INTEGRATION.md)** — concrete setup
for ErsatzTV, Tunarr and Dispatcharr.

## The pool maintains itself

- Live-window cams re-capture on a schedule so they stay current.
- A self-healing fetch queue retries public-domain downloads that failed (e.g. a
  temporarily-down archive node).
- Generated card content is grounded where it must be true and free-wheeling only
  where invention is the point.

## Sources & licensing

Bumparr ships pointed at genuinely public-domain / open sources. Anything you add
via config is your responsibility. Bundled fonts live in `bumparr/fonts/` and are
OFL (commercial-OK); a personal-use font is your own config, never redistributed.
Point `CARD_FONT` / `BRAND_FONT` at any `.ttf`/`.otf` to restyle
rendered cards.

## License

**GNU AGPL-3.0** — see [LICENSE](LICENSE). Contributions welcome.

Copyleft, matching the rest of the \*arr stack (Sonarr/Radarr/Prowlarr are GPL-3.0,
Dispatcharr is AGPL-3.0). You're free to use, modify, and self-host Bumparr,
including commercially — but any modified version you distribute *or run as a
network service* must make its complete source available under the same license.
That keeps every fork open and prevents closed-source resale.

Copyright (c) 2026 the Bumparr authors.
