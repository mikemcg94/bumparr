"""Same-origin HLS proxy for live "window" streams.

Public cam / broadcast HLS endpoints usually can't be played directly by the
browser: they send no CORS headers and often session-gate the child playlists
(Akamai "Access Denied"). So the browser asks Bumparr for the stream, and Bumparr
fetches the upstream playlists + segments server-side — same origin to the
browser (no CORS), one cookie session per process (no session gating) — and
relays them, rewriting playlist URLs to route back through this proxy.
"""
import base64
import http.cookiejar
import urllib.request
from urllib.parse import urljoin

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

from bumparr import db

router = APIRouter()

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    return _opener.open(req, timeout=20)


def _upstream(pid):
    with db.conn() as c:
        r = c.execute("SELECT uri FROM playables WHERE id=? AND type='stream'", (pid,)).fetchone()
    return r["uri"] if r else None


def _rewrite(text, base_url, pid):
    """Point every media/variant URI in an m3u8 back through this proxy."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
            continue
        absu = urljoin(base_url, s)
        tok = base64.urlsafe_b64encode(absu.encode()).decode()
        out.append("/api/stream/%s/seg/%s" % (pid, tok))
    return "\n".join(out)


_M3U8 = "application/vnd.apple.mpegurl"


@router.get("/api/stream/{pid}/index.m3u8")
def stream_index(pid: str):
    up = _upstream(pid)
    if not up:
        return PlainTextResponse("not found", status_code=404)
    try:
        data = _fetch(up).read().decode("utf-8", "ignore")
    except Exception as e:
        return PlainTextResponse("#EXTM3U\n# upstream error: %s" % e, status_code=502)
    return PlainTextResponse(_rewrite(data, up, pid), media_type=_M3U8)


@router.get("/api/stream/{pid}/seg/{token}")
def stream_seg(pid: str, token: str):
    try:
        url = base64.urlsafe_b64decode(token.encode()).decode()
    except Exception:
        return Response(status_code=400)
    try:
        resp = _fetch(url)
        body = resp.read()
    except Exception:
        return Response(status_code=502)
    ct = resp.headers.get("Content-Type", "")
    # A nested playlist (variant -> media) gets rewritten too; segments relay as-is.
    if url.split("?")[0].endswith(".m3u8") or "mpegurl" in ct:
        return PlainTextResponse(_rewrite(body.decode("utf-8", "ignore"), url, pid), media_type=_M3U8)
    return Response(content=body, media_type=ct or "video/mp2t")
