"""Outbound URL safety for user-supplied integration URLs (SPEC-8 D-152,
YONDER-192).

A per-user connection makes the server fetch a URL a user typed, which is a
server-side request forgery risk. The policy:

- `https` only, no credentials in the URL.
- The host must resolve to public addresses only: loopback, private (RFC 1918,
  ULA), link-local (which covers cloud metadata), CGNAT, multicast and reserved
  ranges are refused.
- Checked again **at connect time** by the guarded opener, which connects to the
  address it just vetted (DNS rebinding can't swap it afterwards).
- Redirects are never followed: urllib would carry the credential header to the
  new host.
- Connect/read timeouts come from the caller; `MAX_RESPONSE_BYTES` caps a body.
- **Admin allowlist** (`TX_INTEGRATION_ALLOWED_HOSTS`, comma-separated host or
  host:port): those hosts may be private and may use plain http — e.g. the home
  AirTrail at `http://airtrail:3000`. The hosts of the deprecated server-wide
  TX_AIRTRAIL_URL / TX_ADVENTURELOG_URL are trusted too while their credential is
  set, since the operator configured them (D-150 migration).

Errors are short and safe to show a user; they never include a response body.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 20_000_000          # an AirTrail flight list for a heavy flyer fits


class BlockedURL(OSError):
    """The URL breaks the outbound policy. The message is user-safe. An OSError
    so that, raised at connect time, urllib and the adapters report it like any
    other connection failure."""


def _host_key(host: str, port: int | None) -> tuple[str, str]:
    h = (host or "").strip().lower().rstrip(".")
    return h, (f"{h}:{port}" if port else h)


def allowed_hosts() -> set[str]:
    from app.config import settings
    out = {h.strip().lower() for h in (settings.integration_allowed_hosts or "").split(",")
           if h.strip()}
    for url, cred in ((settings.airtrail_url, settings.airtrail_token),
                      (settings.adventurelog_url, settings.adventurelog_api_key)):
        if url and cred:
            parts = urlsplit(url)
            if parts.hostname:
                out.add(parts.hostname.lower())
                if parts.port:
                    out.add(f"{parts.hostname.lower()}:{parts.port}")
    return out


def is_allowlisted(host: str, port: int | None = None) -> bool:
    allow = allowed_hosts()
    h, hp = _host_key(host, port)
    return h in allow or hp in allow


def _public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback
                                 or ip.is_link_local or ip.is_private)


def _resolve(host: str, port: int) -> list[tuple]:
    try:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise BlockedURL(f"{host} doesn't resolve") from None


def vetted_addresses(host: str, port: int) -> list[tuple]:
    """getaddrinfo results for host:port, all of them public, or BlockedURL.
    An allowlisted host skips the address check."""
    infos = _resolve(host, port)
    if is_allowlisted(host, port):
        return infos
    for info in infos:
        if not _public(ipaddress.ip_address(info[4][0])):
            raise BlockedURL(f"{host} is a private or local address; "
                             "an admin can allow it (TX_INTEGRATION_ALLOWED_HOSTS)")
    return infos


def check_url(url: str, *, resolve: bool = True) -> str:
    """The URL, normalised (no trailing slash), if the policy allows it."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise BlockedURL("enter a full URL, e.g. https://airtrail.example.com")
    if parts.username or parts.password:
        raise BlockedURL("put the token in its own field, not in the URL")
    if parts.query or parts.fragment:
        raise BlockedURL("the base URL can't have a query or fragment")
    try:
        port = parts.port
    except ValueError:
        raise BlockedURL("that port isn't valid") from None
    allow = is_allowlisted(parts.hostname, port)
    if parts.scheme != "https" and not allow:
        raise BlockedURL("use https (plain http is only for hosts an admin allows)")
    if resolve:
        vetted_addresses(parts.hostname, port or (443 if parts.scheme == "https" else 80))
    return raw.rstrip("/")


# ------------------------------------------------------------ guarded opener

def _connect(host: str, port: int, timeout) -> socket.socket:
    last: OSError | None = None
    for family, socktype, proto, _, addr in vetted_addresses(host, port):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(addr)              # the address just vetted, not a re-lookup
            return sock
        except OSError as err:
            sock.close()
            last = err
    raise last or OSError(f"can't connect to {host}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        if not is_allowlisted(self.host, self.port):
            raise BlockedURL("use https (plain http is only for hosts an admin allows)")
        self.sock = _connect(self.host, self.port, self.timeout)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _connect(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A URLError, not an HTTPError: a 3xx status would read as success to
        # the adapters, which only treat >= 400 as a failure.
        raise urllib.error.URLError(f"the server redirected (HTTP {code}); "
                                    "redirects aren't followed")


def guarded_opener() -> urllib.request.OpenerDirector:
    """An opener that enforces the policy on every connection: no proxies from
    the environment, vetted addresses only, no redirects."""
    import ssl
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _HTTPHandler(),
        _HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
