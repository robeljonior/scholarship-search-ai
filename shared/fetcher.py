"""Safe fetcher for untrusted URLs (SSRF-safe, size/time limited).

- only http/https; no credentials in URLs
- resolves the host and refuses private, loopback, link-local, multicast and reserved IPs
- follows at most 5 redirects, checking every hop again
- timeouts, maximum size, content-type allow-list, simple per-domain rate limit

In demo mode (`fetch_mode: demo`) URLs are served from demo/pages/index.json fixtures, so
the whole pipeline can be tested offline with realistic domains.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import PACKAGE_ROOT, Settings
from .extract_text import extract_text

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20
MAX_REDIRECTS = 5
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/pdf")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) personal-scholarship-tracker/2.0"
_last_hit: dict[str, float] = {}
MIN_INTERVAL_PER_DOMAIN = 2.0


class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    content_type: str
    body: bytes
    text: str
    html: str
    sha256: str

    @property
    def domain(self) -> str:
        return domain_of(self.final_url)


def domain_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise FetchError("credentials in URL not allowed")
    host = parts.hostname
    if not host:
        raise FetchError("no host")
    if host.lower() in ("localhost",) or host.lower().endswith(".localhost") or host.lower().endswith(".local"):
        raise FetchError("local hosts not allowed")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_private)):
            raise FetchError(f"{host} resolves to a non-public address ({ip})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _rate_limit(domain: str) -> None:
    last = _last_hit.get(domain)
    if last is not None:
        wait = MIN_INTERVAL_PER_DOMAIN - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[domain] = time.monotonic()


def fetch_live(url: str) -> FetchResult:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        _rate_limit(domain_of(current))
        req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf"})
        try:
            resp = _opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urllib.parse.urljoin(current, e.headers["Location"])
                continue
            raise FetchError(f"HTTP {e.code} for {current}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"fetch failed for {current}: {e}") from e
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith(ALLOWED_TYPES):
                raise FetchError(f"content type not allowed: {ctype}")
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError("response too large")
        return _result(url, current, ctype or "text/html", body)
    raise FetchError("too many redirects")


def _result(url: str, final_url: str, ctype: str, body: bytes) -> FetchResult:
    text, html = extract_text(body, ctype)
    return FetchResult(url=url, final_url=final_url, content_type=ctype, body=body, text=text, html=html,
                       sha256=hashlib.sha256(text.encode()).hexdigest())


class DemoFetcher:
    """Serves fixtures from demo/pages. Same interface as fetch_live."""

    def __init__(self, pages_dir=None):
        self.dir = pages_dir or (PACKAGE_ROOT / "demo" / "pages")
        self.index = json.loads((self.dir / "index.json").read_text())

    def __call__(self, url: str) -> FetchResult:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"scheme not allowed: {parts.scheme!r}")
        name = self.index.get(url) or self.index.get(url.rstrip("/"))
        if not name:
            raise FetchError(f"HTTP 404 for {url} (not in demo fixtures)")
        body = (self.dir / name).read_bytes()
        return _result(url, url, "text/html", body)


def get_fetcher(settings: Settings):
    return DemoFetcher() if settings.get("fetch_mode") == "demo" else fetch_live
