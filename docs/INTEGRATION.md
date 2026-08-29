# Wiring Bumparr into your channel

Bumparr produces bumpers. It does not decide what airs or when — that is your
channel generator's job. This is how to hand its output to one.

Everything below assumes Bumparr is reachable at `http://bumparr:8780`. If it
sits behind a reverse proxy, set `PUBLIC_URL` to the address your *consumers*
reach, because the playlist hands out absolute URLs to a separate player process
that has no idea where the playlist came from.

```
PUBLIC_URL=https://bumpers.example.com
```

## The three ways out

| Endpoint | Gives you | Use it when |
|---|---|---|
| `GET /playlist.m3u` | M3U of every playable bumper, absolute URLs | Your tool ingests a playlist |
| `GET /api/bumpers/random` | One bumper, JSON | You want to pick one yourself |
| `GET /api/bumpers/fill?seconds=N` | A *set* that adds up to N seconds | You have a gap to fill |

The third one is the interesting one, and the reason Bumparr exists.

## Filling a gap (the part nothing else does)

Ask for a duration and Bumparr hands back bumpers that add up to it:

```bash
curl 'http://bumparr:8780/api/bumpers/fill?seconds=47'
```

```
seconds=47&tolerance=1.5&max_items=8&types=video,card
```

- `seconds` — the gap you need to fill (required)
- `tolerance` — acceptable over/under, default `1.5s`
- `max_items` — cap on how many pieces, default `8`
- `types` — restrict to `video`, `card`, `stream`, `image`

It solves this as a small subset-sum with randomised restarts, not a greedy
pass. That matters: greedy grabs the biggest clip that fits and leaves a
stubborn remainder no single clip covers, which is exactly how filler systems
end up with dead air at the end of every break. Bumparr will combine a 22s
clip, an 18s card and a 7s ident to land on 47.

## ErsatzTV

ErsatzTV has first-class filler support, so point it at the playlist and let it
schedule.

1. **Add the library.** Bumparr writes finished bumpers to its output directory
   (`OUTPUT`, default `<assets>/bumpers`). Mount that path into ErsatzTV and add
   it as a local library — this is the simplest, most reliable route, because
   ErsatzTV indexes real files.
2. **Build a Filler Preset** (Playout → Filler Presets) pointing at that
   library. Set it to *Pad* or *Pre-roll / Post-roll* depending on where you
   want bumpers.
3. **Attach the filler** to the schedule items that need it.

If you would rather not share a filesystem, add `http://bumparr:8780/playlist.m3u`
as a playlist source instead — just make sure `PUBLIC_URL` is set so the URLs
resolve from ErsatzTV's container.

## Tunarr

Tunarr also does flex/filler natively.

1. Add Bumparr's output directory as a media source (same mount approach as
   above), or add the M3U.
2. In the channel's **Flex** settings, choose the bumper library as filler
   content.
3. Set flex to fill the gap rather than pad with a static image.

## Dispatcharr

Dispatcharr is stream-oriented, so the playlist is the natural seam:

```
http://bumparr:8780/playlist.m3u
```

Add it as an M3U source and refresh on whatever interval you like — the
playlist is generated live, so new bumpers appear without touching config.

## Anything else

If your tool speaks neither M3U nor local files, drive it from the API:

```bash
# what's in the pool
curl http://bumparr:8780/api/status

# one bumper as JSON
curl http://bumparr:8780/api/bumpers/random

# fill a 30-second gap with video only
curl 'http://bumparr:8780/api/bumpers/fill?seconds=30&types=video'
```

## Keeping the pool fresh

Bumparr maintains itself, but the useful manual levers are:

```bash
# pull the shipped starter seeds (needs PEXELS_API_KEY / PIXABAY_API_KEY)
curl -X POST http://bumparr:8780/api/starter

# generate more cards of a kind
curl -X POST http://bumparr:8780/api/generate/psa

# render text cards to video files
curl -X POST http://bumparr:8780/api/render/cards

# drop dead entries / revive ones marked unhealthy
curl -X POST http://bumparr:8780/api/pool/tidy
curl -X POST http://bumparr:8780/api/pool/revive
```

Text cards are structured content until they are rendered. If your consumer
plays files (ErsatzTV, Tunarr), run `/api/render/cards` so they become real
MP4s; a consumer that reads the API can use them directly.

## Notes

- **Live cams need `yt-dlp` on PATH** for the YouTube-backed ones. The direct
  DOT/HLS cams work without it, as does everything else.
- **A local model is optional.** Grounded cards, procedural kinds, and a
  built-in starter set all work with no model configured. See the README.
