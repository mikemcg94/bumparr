# Bundled fonts

Only fonts cleared for redistribution ship here. Both are SIL Open Font License,
which permits bundling in an AGPL project and in a commercial product.

- `card-nunito.ttf` — Nunito (OFL). Default card body font.
- `card-quicksand.ttf` — Quicksand (OFL). Alternate body font.

A deployer's own font is CONFIG, never a bundled asset: drop a `.ttf`/`.otf`
anywhere on the font search path and set `CARD_FONT` / `BRAND_FONT`
to its filename. Personal-use-only fonts must never be committed here.

Note for renderers: `.woff2` is a web-only format. Pillow cannot read it, so the
video renderer resolves a `.ttf`/`.otf` of the same stem when the configured font
is a web font.
