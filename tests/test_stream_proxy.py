import base64
import http.server
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bumparr.stream_proxy as sp


def _unsigned(url):
    return base64.urlsafe_b64encode(url.encode()).decode()


class _Resp:
    def __init__(self, body, ct="video/mp2t", declared=None, final_url=None):
        self._body = body
        self._pos = 0
        self.closed = False
        self.final_url = final_url
        self.headers = {"Content-Type": ct}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body) - self._pos
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def close(self):
        self.closed = True

    def geturl(self):
        return self.final_url


class StreamProxy(unittest.TestCase):
    def setUp(self):
        self.record = sp._stream_record
        self.fetch = sp._fetch
        self.opener = sp._opener
        self.seg_limit = sp._SEGMENT_MAX
        self.playlist_limit = sp._PLAYLIST_MAX
        self.private = os.environ.get("ALLOW_PRIVATE_UPSTREAM")
        sp._stream_record = lambda pid: {
            "uri": "https://media.example/up.m3u8", "payload": "{}"}

    def tearDown(self):
        sp._stream_record = self.record
        sp._fetch = self.fetch
        sp._opener = self.opener
        sp._SEGMENT_MAX = self.seg_limit
        sp._PLAYLIST_MAX = self.playlist_limit
        if self.private is None:
            os.environ.pop("ALLOW_PRIVATE_UPSTREAM", None)
        else:
            os.environ["ALLOW_PRIVATE_UPSTREAM"] = self.private

    def test_unknown_pid_404(self):
        sp._stream_record = lambda pid: None
        self.assertEqual(sp.stream_seg("nope", _unsigned("file:///etc/hostname")).status_code, 404)

    def test_unsigned_and_file_tokens_rejected_without_fetch(self):
        called = []
        sp._fetch = lambda *args: called.append(args)
        self.assertEqual(sp.stream_seg("cam", _unsigned("https://media.example/x.ts")).status_code, 400)
        self.assertEqual(sp.stream_seg("cam", sp._mint_token("cam", "file:///etc/hostname")).status_code, 400)
        self.assertEqual(called, [])

    def test_token_is_bound_to_pid(self):
        token = sp._mint_token("cam-a", "https://media.example/x.ts")
        self.assertEqual(sp.stream_seg("cam-b", token).status_code, 400)

    def test_foreign_origin_and_port_rejected(self):
        for url in ("https://evil.example/x.ts", "http://media.example/x.ts",
                    "https://media.example:444/x.ts"):
            self.assertEqual(sp.stream_seg("cam", sp._mint_token("cam", url)).status_code, 400)

    def test_same_origin_and_configured_cdn_are_proxied_and_closed(self):
        responses = []
        def fetch(url, allowed):
            response = _Resp(b"SEGDATA")
            responses.append((url, allowed, response))
            return response
        sp._fetch = fetch
        same = sp.stream_seg("cam", sp._mint_token("cam", "https://media.example/x.ts"))
        self.assertEqual(same.body, b"SEGDATA")
        self.assertTrue(responses[-1][2].closed)

        sp._stream_record = lambda pid: {
            "uri": "https://media.example/up.m3u8",
            "payload": json.dumps({"proxy_hosts": ["https://cdn.example"]}),
        }
        cdn = sp.stream_seg("cam", sp._mint_token("cam", "https://cdn.example/x.ts"))
        self.assertEqual(cdn.status_code, 200)

    def test_rewrite_mints_signed_media_and_key_tokens_only_for_allowed_origins(self):
        allowed = {("https", "media.example", 443), ("https", "cdn.example", 443)}
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nseg.ts\nhttps://cdn.example/two.ts\nhttps://evil.example/no.ts'
        rewritten = sp._rewrite(text, "https://media.example/a/index.m3u8", "cam", allowed)
        self.assertIn("/api/stream/cam/seg/", rewritten)
        self.assertIn("https://evil.example/no.ts", rewritten)
        tokens = [part.split('/seg/', 1)[1].split('"', 1)[0]
                  for part in rewritten.splitlines() if "/seg/" in part]
        self.assertEqual(len(tokens), 3)
        self.assertTrue(all(sp._decode_token("cam", token) for token in tokens))

    def test_redirected_playlist_uses_final_url_as_relative_base(self):
        final = "https://media.example/redirected/master.m3u8"
        response = _Resp(b"#EXTM3U\nseg.ts", ct="application/vnd.apple.mpegurl",
                         final_url=final)
        sp._fetch = lambda *args: response
        result = sp.stream_index("cam")
        token = result.body.decode().split("/seg/", 1)[1]
        self.assertEqual(sp._decode_token("cam", token),
                         "https://media.example/redirected/seg.ts")

    def test_redirected_child_playlist_uses_final_url_and_type(self):
        initial = "https://media.example/child"
        final = "https://media.example/nested/index.m3u8"
        response = _Resp(b"#EXTM3U\npart.ts", final_url=final)
        sp._fetch = lambda *args: response
        result = sp.stream_seg("cam", sp._mint_token("cam", initial))
        token = result.body.decode().split("/seg/", 1)[1]
        self.assertEqual(sp._decode_token("cam", token),
                         "https://media.example/nested/part.ts")

    def test_index_and_segment_caps_close_response(self):
        sp._PLAYLIST_MAX = 4
        sp._SEGMENT_MAX = 4
        for method, token in ((sp.stream_index, None),
                              (sp.stream_seg, sp._mint_token("cam", "https://media.example/x.ts"))):
            response = _Resp(b"12345", ct="video/mp2t")
            sp._fetch = lambda *args, response=response: response
            result = method("cam") if token is None else method("cam", token)
            self.assertEqual(result.status_code, 502)
            self.assertTrue(response.closed)

    def test_declared_oversize_is_rejected_and_closed(self):
        sp._SEGMENT_MAX = 4
        response = _Resp(b"x", declared=99)
        sp._fetch = lambda *args: response
        result = sp.stream_seg("cam", sp._mint_token("cam", "https://media.example/x.ts"))
        self.assertEqual(result.status_code, 502)
        self.assertTrue(response.closed)

    def test_redirect_to_file_foreign_or_loopback_is_blocked(self):
        os.environ["ALLOW_PRIVATE_UPSTREAM"] = "1"
        class Opener:
            def __init__(self, target): self.target = target; self.calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                             {"Location": self.target}, io.BytesIO())
        allowed = {("https", "media.example", 443)}
        for target in ("file:///etc/hostname", "https://evil.example/x",
                       "http://127.0.0.1/latest/meta-data"):
            sp._opener = Opener(target)
            with self.assertRaises(ValueError):
                sp._fetch("https://media.example/start", allowed)

    def test_allowed_origin_redirect_is_dns_checked_again(self):
        os.environ.pop("ALLOW_PRIVATE_UPSTREAM", None)

        class Opener:
            def __init__(self): self.calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found", {"Location": "/next"}, io.BytesIO())

        public = (None, None, None, None, ("93.184.216.34", 443))
        loopback = (None, None, None, None, ("127.0.0.1", 443))
        sp._opener = Opener()
        allowed = {("https", "media.example", 443)}
        with mock.patch.object(sp.socket, "getaddrinfo",
                               side_effect=[[public], [loopback]]):
            with self.assertRaisesRegex(ValueError, "private or special"):
                sp._fetch("https://media.example/start", allowed)
        self.assertEqual(sp._opener.calls, 1)

    def test_private_origin_requires_explicit_opt_in(self):
        info = (None, None, None, None, ("10.0.0.5", 443))
        allowed = {("https", "camera.local", 443)}
        os.environ.pop("ALLOW_PRIVATE_UPSTREAM", None)
        with mock.patch.object(sp.socket, "getaddrinfo", return_value=[info]):
            with self.assertRaisesRegex(ValueError, "private or special"):
                sp._validate_url("https://camera.local/live.m3u8", allowed, resolve=True)
            os.environ["ALLOW_PRIVATE_UPSTREAM"] = "1"
            sp._validate_url("https://camera.local/live.m3u8", allowed, resolve=True)

    def test_error_detail_is_not_reflected(self):
        sp._fetch = lambda *args: (_ for _ in ()).throw(RuntimeError("SECRET-PATH"))
        response = sp.stream_index("cam")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b"SECRET-PATH", response.body)


class StreamProxyIntegration(unittest.TestCase):
    """Real local HTTP fetches for same-origin and configured-CDN playback."""

    @staticmethod
    def _server(routes):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, content_type, body = routes.get(
                    self.path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def setUp(self):
        self.original_record = sp._stream_record
        self.private = os.environ.get("ALLOW_PRIVATE_UPSTREAM")
        os.environ["ALLOW_PRIVATE_UPSTREAM"] = "1"
        self.servers = []

    def tearDown(self):
        sp._stream_record = self.original_record
        if self.private is None:
            os.environ.pop("ALLOW_PRIVATE_UPSTREAM", None)
        else:
            os.environ["ALLOW_PRIVATE_UPSTREAM"] = self.private
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @staticmethod
    def _token(response):
        line = next(line for line in response.body.decode().splitlines()
                    if line and not line.startswith("#"))
        return line.split("/seg/", 1)[1]

    def test_real_same_origin_playlist_and_segment(self):
        server, thread = self._server({
            "/same/index.m3u8": (200, "application/vnd.apple.mpegurl",
                                  b"#EXTM3U\nseg.ts\n"),
            "/same/seg.ts": (200, "video/mp2t", b"same-origin-segment"),
        })
        self.servers.append((server, thread))
        upstream = "http://127.0.0.1:%d/same/index.m3u8" % server.server_port
        sp._stream_record = lambda _pid: {"uri": upstream, "payload": "{}"}
        index = sp.stream_index("same")
        segment = sp.stream_seg("same", self._token(index))
        self.assertEqual(index.status_code, 200)
        self.assertEqual(segment.body, b"same-origin-segment")

    def test_real_configured_cross_origin_cdn_segment(self):
        cdn, cdn_thread = self._server({
            "/cdn/seg.ts": (200, "video/mp2t", b"cdn-segment"),
        })
        cdn_url = "http://127.0.0.1:%d/cdn/seg.ts" % cdn.server_port
        origin, origin_thread = self._server({
            "/cross/index.m3u8": (200, "application/vnd.apple.mpegurl",
                                   ("#EXTM3U\n%s\n" % cdn_url).encode()),
        })
        self.servers.extend(((cdn, cdn_thread), (origin, origin_thread)))
        upstream = "http://127.0.0.1:%d/cross/index.m3u8" % origin.server_port
        payload = json.dumps({"proxy_hosts": [
            "http://127.0.0.1:%d" % cdn.server_port]})
        sp._stream_record = lambda _pid: {"uri": upstream, "payload": payload}
        index = sp.stream_index("cross")
        segment = sp.stream_seg("cross", self._token(index))
        self.assertEqual(index.status_code, 200)
        self.assertEqual(segment.body, b"cdn-segment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
