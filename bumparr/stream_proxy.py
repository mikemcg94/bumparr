"""Same-origin, bounded HLS proxy for configured live streams."""
import base64
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import urllib.error
import urllib.request
from contextlib import closing
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

from bumparr import db

router = APIRouter()
log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TOKEN_SECRET = secrets.token_bytes(32)
_PLAYLIST_MAX = 2 * 1024 * 1024
_SEGMENT_MAX = int(os.environ.get("STREAM_SEGMENT_MAX_MB", "64")) * 1024 * 1024
_MAX_REDIRECTS = 5
_M3U8 = "application/vnd.apple.mpegurl"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _mint_token(pid, url):
    encoded = _b64(url.encode("utf-8"))
    sig = hmac.new(_TOKEN_SECRET, (pid + "\0" + url).encode("utf-8"),
                   hashlib.sha256).digest()
    return encoded + "." + _b64(sig)


def _decode_token(pid, token):
    try:
        encoded, supplied = token.split(".", 1)
        url = _unb64(encoded).decode("utf-8")
        expected = hmac.new(_TOKEN_SECRET, (pid + "\0" + url).encode("utf-8"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(supplied), expected):
            return None
        return url
    except (ValueError, UnicodeError, TypeError):
        return None


def _origin(url):
    """Return a normalized (scheme, host, effective-port) origin."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, parsed.hostname.rstrip(".").lower(), port
    except (TypeError, ValueError):
        return None


def _allowed_origins(upstream, payload):
    base = _origin(upstream)
    if base is None:
        return set()
    allowed = {base}
    try:
        hosts = json.loads(payload or "{}").get("proxy_hosts", [])
    except (TypeError, ValueError, json.JSONDecodeError):
        hosts = []
    if not isinstance(hosts, list):
        return allowed
    for value in hosts:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        candidate = value if "://" in value else "%s://%s" % (base[0], value)
        origin = _origin(candidate)
        if origin is not None:
            allowed.add(origin)
    return allowed


def _private_targets_allowed():
    value = os.environ.get("ALLOW_PRIVATE_UPSTREAM",
                           os.environ.get("ALLOW_LOOPBACK_UPSTREAM", ""))
    return value.lower() in ("1", "true", "yes")


def _validate_url(url, allowed_origins, resolve=False):
    origin = _origin(url)
    if origin is None or (allowed_origins is not None and origin not in allowed_origins):
        raise ValueError("upstream URL is not allowed")
    if not resolve or _private_targets_allowed():
        return
    try:
        infos = socket.getaddrinfo(origin[1], origin[2], type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("upstream hostname did not resolve") from exc
    if not infos:
        raise ValueError("upstream hostname did not resolve")
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global:
            raise ValueError("private or special upstream address is blocked")


def _fetch(url, allowed_origins):
    """Fetch with manual, policy-checked redirects and a shared cookie jar."""
    current = url
    for redirects in range(_MAX_REDIRECTS + 1):
        _validate_url(current, allowed_origins, resolve=True)
        req = urllib.request.Request(current,
                                     headers={"User-Agent": _UA, "Accept": "*/*"})
        try:
            return _opener.open(req, timeout=20)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                exc.close()
                raise
            location = exc.headers.get("Location")
            exc.close()
            if not location or redirects == _MAX_REDIRECTS:
                raise ValueError("invalid or excessive upstream redirect")
            current = urljoin(current, location)
    raise ValueError("excessive upstream redirects")


def _read_bounded(resp, limit):
    declared = resp.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > limit:
            raise ValueError("upstream body exceeds limit")
    chunks = []
    total = 0
    while total <= limit:
        chunk = resp.read(min(262144, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise ValueError("upstream body exceeds limit")
    return b"".join(chunks)


def _stream_record(pid):
    with db.conn() as c:
        row = c.execute(
            "SELECT uri, payload FROM playables WHERE id=? AND type='stream'", (pid,)
        ).fetchone()
    return dict(row) if row else None


def _proxy_uri(pid, absolute, allowed_origins):
    _validate_url(absolute, allowed_origins)
    return "/api/stream/%s/seg/%s" % (pid, _mint_token(pid, absolute))


def _rewrite(text, base_url, pid, allowed_origins):
    """Rewrite only URLs permitted by the stream's explicit origin policy."""
    out = []
    uri_attr = re.compile(r'URI="([^"]+)"')
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            def replace(match):
                absolute = urljoin(base_url, match.group(1))
                try:
                    return 'URI="%s"' % _proxy_uri(pid, absolute, allowed_origins)
                except ValueError:
                    return match.group(0)
            out.append(uri_attr.sub(replace, line))
            continue
        absolute = urljoin(base_url, stripped)
        try:
            out.append(_proxy_uri(pid, absolute, allowed_origins))
        except ValueError:
            # Foreign URLs remain client-side; no server-fetch token is minted.
            out.append(line)
    return "\n".join(out)


@router.get("/api/stream/{pid}/index.m3u8")
def stream_index(pid: str):
    record = _stream_record(pid)
    if not record:
        return PlainTextResponse("not found", status_code=404)
    allowed = _allowed_origins(record["uri"], record.get("payload"))
    if not allowed:
        return Response(status_code=400)
    try:
        with closing(_fetch(record["uri"], allowed)) as resp:
            # urllib follows only redirects that _fetch has policy-checked.  A
            # relative URI in the returned playlist is relative to that final
            # URL, not necessarily to the originally configured URL.
            final_url = (resp.geturl() or record["uri"]
                         if callable(getattr(resp, "geturl", None))
                         else record["uri"])
            data = _read_bounded(resp, _PLAYLIST_MAX).decode("utf-8", "replace")
    except Exception as exc:
        log.warning("HLS index fetch failed for %s: %s", pid, exc)
        return PlainTextResponse("upstream unavailable", status_code=502)
    return PlainTextResponse(_rewrite(data, final_url, pid, allowed),
                             media_type=_M3U8)


@router.get("/api/stream/{pid}/seg/{token}")
def stream_seg(pid: str, token: str):
    record = _stream_record(pid)
    if not record:
        return PlainTextResponse("not found", status_code=404)
    url = _decode_token(pid, token)
    if url is None:
        return Response(status_code=400)
    allowed = _allowed_origins(record["uri"], record.get("payload"))
    try:
        _validate_url(url, allowed)
    except ValueError:
        return Response(status_code=400)
    try:
        with closing(_fetch(url, allowed)) as resp:
            final_url = (resp.geturl() or url
                         if callable(getattr(resp, "geturl", None)) else url)
            content_type = resp.headers.get("Content-Type", "")
            playlist = (urlparse(final_url).path.lower().endswith(".m3u8")
                        or "mpegurl" in content_type)
            body = _read_bounded(resp, _PLAYLIST_MAX if playlist else _SEGMENT_MAX)
    except Exception as exc:
        log.warning("HLS child fetch failed for %s: %s", pid, exc)
        return Response(status_code=502)
    if playlist:
        return PlainTextResponse(
            _rewrite(body.decode("utf-8", "replace"), final_url, pid, allowed),
            media_type=_M3U8,
        )
    return Response(content=body, media_type=content_type or "video/mp2t")
