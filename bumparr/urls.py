"""Absolute URLs for everything Bumparr hands out.

Lives outside app.py because the station router needs the same rule and
must not import the app to get it. The rule itself: explicit PUBLIC_URL
wins; otherwise mirror the request, and warn once when that mirror is a
loopback address, because that playlist looks fine and plays nowhere.
"""
import re

from bumparr import config

_LOOPBACK_WARNED = False


def public_base(request):
    """Base URL external consumers should use to reach this Bumparr.

    Explicit config wins (needed behind a reverse proxy, where the app cannot
    see its own public hostname). Otherwise derive it from the request, which is
    correct for direct access on a LAN port.

    The derived form mirrors whoever asked, which is right when the fetcher and
    the player are the same machine and silently wrong when they are not: fetch
    the playlist over loopback and every entry says 127.0.0.1, which no other
    host or container can play. That failure is invisible -- the playlist looks
    fine and simply does not work -- so warn once rather than let a deployer
    discover it through a dead channel.
    """
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    if not request:
        return ""
    base = str(request.base_url).rstrip("/")
    global _LOOPBACK_WARNED
    if not _LOOPBACK_WARNED and re.search(r"//(127\.0\.0\.1|localhost|\[::1\])\b", base):
        _LOOPBACK_WARNED = True
        print("[bumparr] WARNING: handing out %s URLs, derived from a loopback "
              "request. Anything else -- another container, another host, a "
              "player -- cannot reach those. Set PUBLIC_URL to the address your "
              "consumers actually use." % base)
    return base


def absolutize(url, request):
    """Make a Bumparr-relative URL absolute.

    Every URL Bumparr hands out is consumed by something else — ErsatzTV,
    Dispatcharr, Tunarr, Jellyfin, VLC, or a player. None of them can resolve a
    bare path, and an M3U in particular has no base-URL rule, so relative
    entries are simply unplayable. Upstream URLs that are already absolute
    (direct live-cam streams) pass through untouched.
    """
    if not url or url.startswith(("http://", "https://")):
        return url
    return public_base(request) + url
