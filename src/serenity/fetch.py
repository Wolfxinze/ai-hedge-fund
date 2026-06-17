"""SSRF-guarded outbound fetch for Serenity evidence (PRD v4 Phase 6/7, §9.6/§11.5).

The single I/O boundary for fetching attacker-influenceable evidence URLs. evidence.py
stays pure; this module owns the SSRF guard. The defense pipeline — re-run in full for
the initial URL AND every redirect hop — is:

  1. scheme gate: https only; reject userinfo (@) in the authority
  2. allowlist gate (pre-DNS): host must map to a known source_type, else off_allowlist
  3. raw-IP-literal gate: an IP host (any encoding) is validated immediately; internal →
     blocked, public raw-IP → off_allowlist (evidence sources are named domains)
  4. resolve ONCE via getaddrinfo
  5. reject if ANY resolved IP is non-public (defeats multi-record / CNAME rebinding)
  6. pin the connection to the validated IP (closes the resolve->connect TOCTOU)
  7. streamed byte cap (never trust Content-Length alone) + timeout
  8. textual content-type gate

``fetch_excerpt`` is TOTAL: it never raises; every failure is encoded in
``FetchResult.reason``. Host-allowlisting authenticates the SERVER, not the author —
``evidence.is_substantiated`` remains the independent content gate, so a trusted host
alone never substantiates.

IP-pin mechanism: socket.getaddrinfo is scoped (per request) to return the pre-validated
IP for the target host — version-robust (no urllib3 internals) but process-global, so a
module lock serialises fetches. The Serenity research path is sequential; revisit if this
is ever called from the hot API path.
"""

import ipaddress
import logging
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests

from src.serenity.evidence import DEFAULT_HOST_ALLOWLIST, source_type_for_host
from src.storage.models import SourceType

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
        return v if v > 0 else default  # fail-closed: never 'unlimited'
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, str(default)))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


EVIDENCE_FETCH_MAX_BYTES = _int_env("EVIDENCE_FETCH_MAX_BYTES", 2_000_000)
FETCH_TIMEOUT_SECONDS = _float_env("SERENITY_FETCH_TIMEOUT", 10.0)
MAX_REDIRECTS = _int_env("SERENITY_MAX_REDIRECTS", 3)

_TEXT_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json", "application/xml")

# Deny nets beyond ipaddress flags (is_private misses CGNAT 100.64/10 and is inconsistent
# on 0.0.0.0/8 across versions); plus the usual private/loopback/link-local for clarity.
_DENY_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
    )
]
# Cloud-metadata IPs not covered by link-local (Alibaba 100.100.100.200 is CGNAT-adjacent).
_DENY_IPS = {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}

_pin_lock = threading.Lock()


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    excerpt: str | None
    final_url: str | None
    status: int | None
    content_type: str | None
    reason: str
    bytes_read: int = 0


def _validate_ip(ip: str) -> bool:
    """True iff ``ip`` is a publicly routable unicast address (the core SSRF predicate)."""
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if obj.is_private or obj.is_loopback or obj.is_link_local or obj.is_multicast or obj.is_reserved or obj.is_unspecified:
        return False
    if str(obj) in _DENY_IPS:
        return False
    for net in _DENY_NETS:
        if obj.version == net.version and obj in net:
            return False
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) — the embedded v4 must also pass.
    if obj.version == 6 and obj.ipv4_mapped is not None:
        return _validate_ip(str(obj.ipv4_mapped))
    return True


def _host_as_ip(host: str) -> str | None:
    """Canonical IP if ``host`` is an IP literal in any encoding (decimal/octal/hex/v6)."""
    h = host.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        return str(ipaddress.ip_address(int(h, 0)))  # 2130706433, 0x7f000001
    except (ValueError, TypeError):
        pass
    try:
        return str(ipaddress.ip_address(socket.inet_aton(h)))  # dotted-octal / short v4 (0177.0.0.1)
    except (OSError, ValueError):
        return None


def _resolve_public_ips(host: str) -> tuple[tuple[str, ...], str | None]:
    """Resolve once; reject the host if ANY resolved IP is non-public (fail-closed)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return (), "dns_error"
    ips = tuple({info[4][0] for info in infos})
    if not ips:
        return (), "dns_error"
    if not all(_validate_ip(ip) for ip in ips):
        return (), "blocked_private_ip"
    return ips, None


def _gate(url: str, allowlist: dict[str, SourceType]) -> tuple[tuple[str, ...] | None, str, str]:
    """Scheme + allowlist + raw-IP + resolve + validate. Returns (ips|None, host, reason);
    reason == 'ok' only on success with non-empty ips. Re-used for the URL and every redirect."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return None, "", "blocked_scheme"
    if "@" in (parts.netloc or ""):  # userinfo spoof — never trust the pre-@ segment
        return None, "", "blocked_scheme"
    host = (parts.hostname or "").lower()  # strips brackets + userinfo correctly
    if not host:
        return None, "", "blocked_scheme"
    raw = _host_as_ip(host)
    if raw is not None:
        if not _validate_ip(raw):
            return None, host, "blocked_private_ip"
        return None, host, "off_allowlist"  # public raw IP is still not a named allowlist host
    if source_type_for_host(host, allowlist) is SourceType.UNVERIFIED:
        return None, host, "off_allowlist"
    ips, reason = _resolve_public_ips(host)
    if reason:
        return None, host, reason
    return ips, host, "ok"


@contextmanager
def _pin_getaddrinfo(host: str, ip: str):
    """Scope socket.getaddrinfo so the target host resolves only to the validated IP."""
    real = socket.getaddrinfo

    def patched(h, *args, **kwargs):
        # host is already lowercased; compare case-insensitively so a different-cased
        # re-resolution can't slip past the pin (which would reopen the TOCTOU window).
        return real(ip, *args, **kwargs) if isinstance(h, str) and h.lower() == host else real(h, *args, **kwargs)

    with _pin_lock:
        socket.getaddrinfo = patched
        try:
            yield
        finally:
            socket.getaddrinfo = real


def resolve_allowlist(env_value: str | None) -> dict[str, SourceType]:
    """Parse SERENITY_HOST_ALLOWLIST ('host:source_type,...') extending DEFAULT, fail-closed."""
    out = dict(DEFAULT_HOST_ALLOWLIST)
    if not env_value:
        return out
    by_value = {s.value: s for s in SourceType if s is not SourceType.UNVERIFIED}
    for entry in env_value.split(","):
        e = entry.strip().lower()
        host, _, stype = e.partition(":")
        host = host.strip()
        stype = stype.strip()
        if not host or not stype or "*" in e or stype not in by_value:
            logger.warning("ignoring invalid SERENITY_HOST_ALLOWLIST entry: %r", entry)
            continue
        out[host] = by_value[stype]
    return out


def _decode_and_truncate(raw: bytes, content_type: str | None, max_bytes: int) -> str:
    enc = "utf-8"
    if content_type and "charset=" in content_type.lower():
        enc = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        text = raw.decode(enc, errors="replace")
    except (LookupError, TypeError):
        text = raw.decode("utf-8", errors="replace")
    return text[:max_bytes]


def _read_response(resp, final_url: str, max_bytes: int) -> FetchResult:
    status = resp.status_code
    ctype = resp.headers.get("Content-Type", "")
    if not (200 <= status < 300):
        resp.close()
        return FetchResult(False, None, final_url, status, ctype, "http_error")
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        resp.close()
        return FetchResult(False, None, final_url, status, ctype, "too_large")
    if not any(ctype.lower().startswith(t) for t in _TEXT_CONTENT_TYPES):
        resp.close()
        return FetchResult(False, None, final_url, status, ctype, "bad_content_type")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(8192):
        total += len(chunk)
        if total > max_bytes:
            resp.close()
            return FetchResult(False, None, final_url, status, ctype, "too_large")
        chunks.append(chunk)
    resp.close()
    raw = b"".join(chunks)
    return FetchResult(True, _decode_and_truncate(raw, ctype, max_bytes), final_url, status, ctype, "ok", len(raw))


def fetch_excerpt(
    url: str,
    *,
    allowlist: dict[str, SourceType] | None = None,
    max_bytes: int | None = None,
    timeout: float | None = None,
    max_redirects: int | None = None,
) -> FetchResult:
    """Fetch ``url`` behind the SSRF guard and return a bounded text excerpt. Total: never raises."""
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    max_bytes = max_bytes or EVIDENCE_FETCH_MAX_BYTES
    timeout = timeout or FETCH_TIMEOUT_SECONDS
    max_redirects = MAX_REDIRECTS if max_redirects is None else max_redirects

    session = requests.Session()
    session.trust_env = False  # ignore proxy / NO_PROXY env (proxy-based SSRF)
    current = url
    on_redirect = False
    try:
        for _ in range(max_redirects + 1):
            ips, host, reason = _gate(current, allowlist)
            if reason != "ok":
                # A redirect that targets a blocked URL reports 'blocked_redirect' so it is
                # distinguishable from an initial-URL failure.
                return FetchResult(False, None, None, None, None, "blocked_redirect" if on_redirect else reason)
            with _pin_getaddrinfo(host, ips[0]):
                resp = session.request("GET", current, allow_redirects=False, stream=True, timeout=(timeout, timeout))
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                status = resp.status_code
                resp.close()
                if not loc:
                    return FetchResult(False, None, current, status, None, "http_error")
                current = urljoin(current, loc)
                on_redirect = True
                continue
            return _read_response(resp, current, max_bytes)
        return FetchResult(False, None, current, None, None, "blocked_redirect")  # depth exceeded
    except requests.Timeout:
        return FetchResult(False, None, current, None, None, "timeout")
    except requests.RequestException:
        return FetchResult(False, None, current, None, None, "connect_error")
    except Exception as exc:  # totality — never raise into grading
        logger.warning("serenity fetch unexpected error for %s: %s", current, exc)
        return FetchResult(False, None, current, None, None, "connect_error")
    finally:
        session.close()
