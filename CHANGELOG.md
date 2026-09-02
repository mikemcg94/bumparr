# Changelog

## v0.1.0 — first public release

First release. A bumper generator for the *arr stack: point it at source media
and it builds and maintains a self-refreshing pool of TV bumpers and
interstitials that any channel generator can consume.

**Output**
- `GET /playlist.m3u` — absolute-URL M3U of every playable bumper.
- `GET /api/bumpers/fill?seconds=N` — a *set* of bumpers summing to a gap,
  solved as subset-sum rather than greedy, so a break doesn't end in dead air.
- `GET /api/bumpers/random`, `/api/status`, and per-bumper lookup.

**Bumper kinds**
- Video from your own media and public-domain archives.
- Live "window" cams — open direct-HLS feeds play genuinely live and ship
  enabled. YouTube-backed snapshot cams are supported but ship disabled; a
  commented template in `live_cams.yaml` shows how to add your own.
- Text cards: grounded trivia and fun-facts, verified numbers, plus surreal
  PSAs, fake corrections, achievements and more.
- Procedural station IDs and technical-difficulties cards.

**Works without a model.** Grounded cards come from real sources, procedural
kinds are code, and every model-generated kind ships a built-in starter set —
so a fresh install with no endpoint still has content in every kind. Point
`LLM_BASE` at any OpenAI-compatible endpoint to generate more, in your own
voice. The model diversifies the pool; it is not required.

**Content integrity.** Factual cards are grounded in real sources rather than
invented, and `card_validation` rejects malformed cards at generation time
(unlabelled options with a bare-letter answer, either/or prompts asserting an
answer, truncated facts, placeholder numbers).

**Notes**
- No YouTube entries ship enabled. Whether to scrape YouTube is the operator's
  call, so Bumparr keeps the capability (`yt-dlp` is installed) but leaves it
  switched off by default.
- With no `PUBLIC_URL` set, emitted URLs are derived from the incoming request.
  Bumparr warns if that derivation is a loopback address, since those URLs
  cannot be reached by any other host, container or player.
- Bundled fonts are SIL OFL. No media assets ship with Bumparr.
