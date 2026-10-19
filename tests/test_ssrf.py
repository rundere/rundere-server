"""Security review, area 4: outbound fetches a person asks for (YONDER-293).

Web capture and Maps short links read URLs a user typed. They go through
netpolicy's public opener: public addresses only, every redirect hop vetted at
connect time, never the admin allowlist, never ftp:/file:. A local HTTP server
stands in for "a public page", by treating 127.0.0.1 as public in these tests.
"""
import ipaddress
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import netpolicy  # noqa: E402

_REAL_PUBLIC = netpolicy._public


@pytest.fixture()
def server(monkeypatch):
    """A 'public' page at 127.0.0.1 that redirects as /to?u=… asks, or serves
    HTML. Only 127.0.0.1 is treated as public; every other private range stays
    blocked."""
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/to?u="):
                self.send_response(302)
                self.send_header("Location", self.path[len("/to?u="):])
                self.end_headers()
                return
            body = b"<html><title>public page</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(netpolicy, "_public",
                        lambda ip: str(ip) == "127.0.0.1" or _REAL_PUBLIC(ip))
    yield f"http://127.0.0.1:{httpd.server_port}", hits
    httpd.shutdown()


def test_public_page_is_fetched(server):
    from app.capture import _fetch_public_html
    base, _ = server
    assert "public page" in (_fetch_public_html(base + "/page") or "")


def test_redirect_into_the_lan_is_refused(server, monkeypatch):
    """The bug: a public page redirecting to a private address was followed and
    read back as the person's capture."""
    from app.capture import _fetch_public_html
    base, _ = server
    connected = []
    real_connect = netpolicy._connect

    def spy(host, port, timeout, *, public_only=False):
        sock = real_connect(host, port, timeout, public_only=public_only)
        connected.append(host)                    # only sockets that really opened
        return sock
    monkeypatch.setattr(netpolicy, "_connect", spy)
    for target in ("http://10.0.0.1/admin", "http://192.168.1.1/", "http://169.254.169.254/",
                   "http://100.64.0.1/"):
        assert _fetch_public_html(f"{base}/to?u={target}") is None, target
    # The private hosts were refused before any socket was opened to them.
    assert all(h == "127.0.0.1" for h in connected)


def test_redirect_to_ftp_or_file_is_refused(server):
    from app.capture import _fetch_public_html
    base, _ = server
    assert _fetch_public_html(f"{base}/to?u=file:///etc/passwd") is None
    assert _fetch_public_html(f"{base}/to?u=ftp://10.0.0.1/x") is None


def test_public_fetch_ignores_the_integration_allowlist(monkeypatch):
    """TX_INTEGRATION_ALLOWED_HOSTS lets a CONNECTION reach the LAN; it must
    never let a web capture read it."""
    monkeypatch.setattr(netpolicy, "allowed_hosts", lambda: {"airtrail", "airtrail:3000"})
    monkeypatch.setattr(netpolicy, "_resolve",
                        lambda host, port: [(2, 1, 6, "", ("10.0.0.5", port))])
    assert netpolicy.vetted_addresses("airtrail", 3000)          # integrations: allowed
    with pytest.raises(netpolicy.BlockedURL):
        netpolicy.vetted_addresses("airtrail", 3000, public_only=True)


def test_nat64_and_mapped_addresses_are_judged_by_their_ipv4():
    pub = _REAL_PUBLIC
    assert not pub(ipaddress.ip_address("64:ff9b::a00:1"))       # NAT64 → 10.0.0.1
    assert not pub(ipaddress.ip_address("64:ff9b::7f00:1"))      # NAT64 → 127.0.0.1
    assert pub(ipaddress.ip_address("64:ff9b::808:808"))         # NAT64 → 8.8.8.8
    assert not pub(ipaddress.ip_address("::ffff:192.168.1.1"))
    assert not pub(ipaddress.ip_address("100.64.0.1"))           # CGNAT / Tailscale


def test_maps_short_link_expansion_cannot_be_bounced_into_the_lan(monkeypatch):
    from extract import maps_url
    opened = []

    def fake_connect(host, port, timeout, *, public_only=False):
        opened.append((host, public_only))
        raise netpolicy.BlockedURL("blocked in test")
    monkeypatch.setattr(netpolicy, "_connect", fake_connect)
    url = "https://maps.app.goo.gl/abc"
    assert maps_url._expand_short(url) == url              # best-effort: unchanged
    assert opened == [("maps.app.goo.gl", True)]           # went through the public opener
