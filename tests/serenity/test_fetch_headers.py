"""Phase 7: fetch_excerpt forwards caller-supplied headers (e.g. SEC EDGAR's required
User-Agent) to the transport on every hop — WITHOUT the headers being able to influence
the SSRF guard. No real network: getaddrinfo + the HTTP transport are stubbed.
"""

import socket

import requests

from src.serenity.fetch import fetch_excerpt


def _resolve_to(monkeypatch, *ips):
    def f(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", f)


class _FakeResp:
    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self._chunks = chunks

    def iter_content(self, n):
        return iter(self._chunks)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def test_headers_forwarded_to_transport(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")  # public IP → gate passes
    captured = {}

    def fake_request(self, method, url, **kw):
        captured.update(kw)
        return _FakeResp(200, {"Content-Type": "text/html"}, [b"hello world filing body text here"])

    monkeypatch.setattr(requests.Session, "request", fake_request)
    res = fetch_excerpt("https://www.sec.gov/x", headers={"User-Agent": "ua/1.0 contact@example.com"})

    assert res.ok
    assert captured["headers"] == {"User-Agent": "ua/1.0 contact@example.com"}


def test_headers_default_none_unchanged(monkeypatch):
    """Existing callers pass no headers → headers=None reaches the transport (back-compat)."""
    _resolve_to(monkeypatch, "93.184.216.34")
    captured = {}

    def fake_request(self, method, url, **kw):
        captured.update(kw)
        return _FakeResp(200, {"Content-Type": "text/html"}, [b"hello world filing body text here"])

    monkeypatch.setattr(requests.Session, "request", fake_request)
    res = fetch_excerpt("https://www.sec.gov/x")

    assert res.ok
    assert captured["headers"] is None


def test_credentials_stripped_on_cross_host_redirect(monkeypatch):
    """Credentials must NOT be carried to a different host across a redirect (leakage), but
    a non-sensitive header like User-Agent is preserved. (sec.gov → reuters.com: both
    allowlisted, so the redirect proceeds and the second hop fires.)"""
    _resolve_to(monkeypatch, "93.184.216.34")
    seen = []

    def fake_request(self, method, url, **kw):
        seen.append((url, dict(kw.get("headers") or {})))
        if "sec.gov/start" in url:
            return _FakeResp(302, {"Location": "https://www.reuters.com/end"}, [b""])
        return _FakeResp(200, {"Content-Type": "text/html"}, [b"hello world body text here ok"])

    monkeypatch.setattr(requests.Session, "request", fake_request)
    res = fetch_excerpt(
        "https://www.sec.gov/start",
        headers={"User-Agent": "ua/1.0", "Authorization": "Bearer SECRET", "Cookie": "s=1"},
    )

    assert res.ok
    assert seen[0][1] == {"User-Agent": "ua/1.0", "Authorization": "Bearer SECRET", "Cookie": "s=1"}
    assert seen[1][1] == {"User-Agent": "ua/1.0"}  # credentials dropped, UA kept
