"""Phase 6: SSRF-guarded Serenity fetch. No real network — getaddrinfo is mocked and
the HTTP transport is stubbed. Tests encode WHY (each is an SSRF bypass that must be
blocked), not just behaviour.
"""

import socket

import pytest
import requests

from src.serenity import fetch
from src.serenity.fetch import _validate_ip, fetch_excerpt, resolve_allowlist
from src.storage.models import SourceType


# ── _validate_ip (the core predicate) ─────────────────────────────────────────

@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",  # loopback / RFC1918
        "169.254.169.254", "100.100.100.200",  # metadata (link-local / Alibaba)
        "100.64.0.1",  # CGNAT (NOT is_private)
        "0.0.0.0", "0.1.2.3",  # 0.0.0.0/8 this-host
        "::1", "fc00::1", "fe80::1",  # IPv6 loopback / ULA / link-local
        "::ffff:10.0.0.1", "::ffff:169.254.169.254",  # IPv4-mapped
    ],
)
def test_validate_ip_rejects_internal(ip):
    assert _validate_ip(ip) is False


@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_validate_ip_accepts_public(ip):
    assert _validate_ip(ip) is True


# ── helpers ───────────────────────────────────────────────────────────────────

def _no_resolve(monkeypatch):
    """Assert the network resolver is never consulted (pre-DNS rejection)."""
    def boom(*a, **k):
        raise AssertionError("getaddrinfo must not be called")
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _resolve_to(monkeypatch, *ips):
    def f(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", f)


class _FakeResp:
    def __init__(self, status, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [b""]
        self.closed = False

    def iter_content(self, n):
        return iter(self._chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _stub_http(monkeypatch, *responses):
    seq = iter(responses)
    def fake(self, method, url, **kw):
        return next(seq)
    monkeypatch.setattr(requests.Session, "request", fake)


# ── pre-DNS rejections (no socket) ────────────────────────────────────────────

def test_rejects_non_https_scheme(monkeypatch):
    _no_resolve(monkeypatch)
    assert fetch_excerpt("http://sec.gov/x").reason == "blocked_scheme"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://sec.gov/", "dict://sec.gov/", "ftp://sec.gov/", "data:text/plain,hi"])
def test_rejects_dangerous_schemes(monkeypatch, url):
    _no_resolve(monkeypatch)
    assert fetch_excerpt(url).reason == "blocked_scheme"


def test_off_allowlist_short_circuits(monkeypatch):
    _no_resolve(monkeypatch)
    assert fetch_excerpt("https://randomblog.com/x").reason == "off_allowlist"


@pytest.mark.parametrize("url", ["https://sec.gov.evil.com/", "https://evilsec.gov/"])
def test_suffix_prefix_spoof_off_allowlist(monkeypatch, url):
    _no_resolve(monkeypatch)
    assert fetch_excerpt(url).reason == "off_allowlist"


def test_raw_ip_metadata_blocked_before_connect(monkeypatch):
    _no_resolve(monkeypatch)
    assert fetch_excerpt("https://169.254.169.254/").reason == "blocked_private_ip"


@pytest.mark.parametrize("url", ["https://2130706433/", "https://0x7f000001/", "https://0177.0.0.1/"])
def test_encoded_ip_literals_blocked(monkeypatch, url):
    _no_resolve(monkeypatch)
    assert fetch_excerpt(url).reason == "blocked_private_ip"


def test_ipv6_bracketed_literal_blocked(monkeypatch):
    _no_resolve(monkeypatch)
    assert fetch_excerpt("https://[::1]/").reason == "blocked_private_ip"


def test_userinfo_spoof_uses_real_host(monkeypatch):
    _no_resolve(monkeypatch)
    # '@' in authority is rejected outright — the pre-@ 'sec.gov' is never trusted.
    assert fetch_excerpt("https://sec.gov@169.254.169.254/").reason == "blocked_scheme"


# ── DNS-based rejections (resolver consulted, fail-closed) ─────────────────────

def test_dns_rebinding_to_internal_rejected(monkeypatch):
    _resolve_to(monkeypatch, "169.254.169.254")  # allowlisted name resolves internal
    assert fetch_excerpt("https://sec.gov/x").reason == "blocked_private_ip"


def test_cname_to_private_rejected(monkeypatch):
    _resolve_to(monkeypatch, "10.1.2.3")
    assert fetch_excerpt("https://sec.gov/x").reason == "blocked_private_ip"


def test_multi_record_one_internal_fails_closed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34", "169.254.169.254")  # reject-if-ANY
    assert fetch_excerpt("https://sec.gov/x").reason == "blocked_private_ip"


# ── redirects (manual, re-validated per hop) ──────────────────────────────────

def test_open_redirect_to_internal_blocked(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(302, {"Location": "http://169.254.169.254/"}))
    assert fetch_excerpt("https://sec.gov/x").reason == "blocked_redirect"


def test_redirect_off_allowlist_blocked(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(302, {"Location": "https://randomblog.com/"}))
    assert fetch_excerpt("https://sec.gov/x").reason == "blocked_redirect"


def test_redirect_depth_capped(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    # every hop redirects to another allowlisted host → exceeds the cap
    loop = [_FakeResp(302, {"Location": "https://reuters.com/next"}) for _ in range(10)]
    _stub_http(monkeypatch, *loop)
    assert fetch_excerpt("https://sec.gov/x", max_redirects=2).reason == "blocked_redirect"


# ── response handling ─────────────────────────────────────────────────────────

def test_oversized_content_length_rejected_early(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/html", "Content-Length": "999999999"}))
    assert fetch_excerpt("https://sec.gov/x", max_bytes=1000).reason == "too_large"


def test_oversized_stream_truncated_lying_header(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    big = [b"x" * 600, b"y" * 600]  # 1200 bytes, no/short Content-Length
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/plain"}, chunks=big))
    assert fetch_excerpt("https://sec.gov/x", max_bytes=1000).reason == "too_large"


def test_non_text_content_type_rejected(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "application/pdf"}))
    assert fetch_excerpt("https://sec.gov/x").reason == "bad_content_type"


def test_non_2xx_http_error_with_meta(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(404, {"Content-Type": "text/html"}))
    res = fetch_excerpt("https://sec.gov/x")
    assert res.reason == "http_error" and res.status == 404 and res.final_url


def test_timeout_maps_to_reason(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    def raise_timeout(self, method, url, **kw):
        raise requests.Timeout("slow")
    monkeypatch.setattr(requests.Session, "request", raise_timeout)
    assert fetch_excerpt("https://sec.gov/x").reason == "timeout"


def test_never_raises_on_connect_error(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    def raise_conn(self, method, url, **kw):
        raise requests.ConnectionError("reset")
    monkeypatch.setattr(requests.Session, "request", raise_conn)
    assert fetch_excerpt("https://sec.gov/x").reason == "connect_error"


def test_happy_path(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    body = b"supplier concentration bottleneck across the chain " * 3
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, chunks=[body]))
    res = fetch_excerpt("https://sec.gov/x")
    assert res.ok and res.reason == "ok" and res.bytes_read == len(body)
    assert "bottleneck" in res.excerpt


def test_excerpt_truncated_to_cap(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/plain"}, chunks=[b"a" * 50]))
    res = fetch_excerpt("https://sec.gov/x", max_bytes=10)
    # read aborts past the cap → too_large (never returns an over-cap excerpt)
    assert res.reason == "too_large"


# ── config parsing (fail-closed) ──────────────────────────────────────────────

def test_resolve_allowlist_drops_malformed():
    al = resolve_allowlist("*,,foo.gov:bogus,bar.gov:regulatory")
    assert al.get("bar.gov") is SourceType.REGULATORY
    assert "foo.gov" not in al
    assert "sec.gov" in al  # DEFAULT preserved


def test_resolve_allowlist_empty_returns_default_copy():
    al = resolve_allowlist(None)
    assert al == fetch.DEFAULT_HOST_ALLOWLIST
    assert al is not fetch.DEFAULT_HOST_ALLOWLIST  # fresh dict, not mutated


# ── the IP pin itself (keystone — a disabled pin must NOT pass) ────────────────

def test_pin_forces_validated_ip_and_restores(monkeypatch):
    import ipaddress

    order = []

    def resolver(host, port=None, *a, **k):
        order.append(host)
        try:
            ipaddress.ip_address(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0))]  # an IP resolves to itself
        except ValueError:
            # 'sec.gov' is public on the FIRST resolve, then 'rebinds' to an internal IP.
            ip = "93.184.216.34" if order.count("sec.gov") == 1 else "10.0.0.5"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    seen = {}
    def fake_request(self, method, url, **kw):
        # What the connection layer would resolve the host to AT CONNECT TIME.
        seen["ip"] = socket.getaddrinfo("sec.gov", 443)[0][4][0]
        return _FakeResp(200, {"Content-Type": "text/plain"}, chunks=[b"enough body text to pass the gate"])

    monkeypatch.setattr(requests.Session, "request", fake_request)

    res = fetch_excerpt("https://sec.gov/x")
    assert res.ok
    # The pin makes connect-time resolution return the PRE-VALIDATED IP, not the rebind (10.0.0.5).
    assert seen["ip"] == "93.184.216.34"
    assert order.count("sec.gov") == 1  # host resolved exactly once (no re-resolve)
    assert socket.getaddrinfo is resolver  # pin restored, no global leak


# ── legitimate redirect IS followed (a block-everything regression must fail) ──

def test_legit_redirect_followed_returns_final_body(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(
        monkeypatch,
        _FakeResp(302, {"Location": "https://reuters.com/final"}),
        _FakeResp(200, {"Content-Type": "text/html"}, chunks=[b"final body text here, enough words"]),
    )
    res = fetch_excerpt("https://sec.gov/x")
    assert res.ok and res.reason == "ok"
    assert res.final_url.endswith("/final")
    assert "final body" in res.excerpt


# ── charset decoding ──────────────────────────────────────────────────────────

def test_decode_respects_latin1(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    body = "café supplier bottleneck concentration".encode("latin-1")
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/html; charset=latin-1"}, chunks=[body]))
    assert "café" in fetch_excerpt("https://sec.gov/x").excerpt


def test_decode_unknown_charset_falls_back_utf8(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    _stub_http(monkeypatch, _FakeResp(200, {"Content-Type": "text/plain; charset=unknown-8bit"}, chunks=[b"hello world body"]))
    res = fetch_excerpt("https://sec.gov/x")
    assert res.ok and "hello" in res.excerpt  # no crash on a bogus charset


# ── response is closed on every terminal branch (no socket leak) ──────────────

@pytest.mark.parametrize(
    "resp",
    [
        _FakeResp(200, {"Content-Type": "text/html"}, chunks=[b"body text here"]),
        _FakeResp(404, {"Content-Type": "text/html"}),
        _FakeResp(200, {"Content-Type": "application/pdf"}),
        _FakeResp(200, {"Content-Type": "text/html", "Content-Length": "999999999"}),
    ],
)
def test_response_closed_on_every_branch(monkeypatch, resp):
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **k: resp)
    fetch_excerpt("https://sec.gov/x", max_bytes=1000)
    assert resp.closed is True


# ── env config fail-closed (a dropped >0 guard would mean 'unlimited') ─────────

@pytest.mark.parametrize("val", ["0", "-5", "abc", ""])
def test_int_env_fail_closed(monkeypatch, val):
    monkeypatch.setenv("X_TEST_INT", val)
    assert fetch._int_env("X_TEST_INT", 100) == 100


@pytest.mark.parametrize("val", ["0", "-1.0", "nope"])
def test_float_env_fail_closed(monkeypatch, val):
    monkeypatch.setenv("X_TEST_FLT", val)
    assert fetch._float_env("X_TEST_FLT", 9.0) == 9.0
