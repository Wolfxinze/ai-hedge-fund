"""SSRF-block suite (PRD v4 §11.5, R2+). The core predicate ``_validate_ip`` is
pure (internal/metadata/IPv6/IPv4-mapped/encoded all rejected; public accepted);
the allowlist gate rejects off-allowlist + suffix-spoof hosts; and ``fetch_excerpt``
blocks each vector with the exact reason. Network is mocked via unittest.mock
(the graders run under the runner, not pytest) — a controllable resolver plus a
Session.send guard so no real HTTP can ever fire.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from unittest import mock

import requests

from src.evals.core import CodeGrader, EvalCase, Recorder
from src.evals.registry import suite
from src.serenity.evidence import source_type_for_host
from src.serenity.fetch import _host_as_ip, _validate_ip, fetch_excerpt
from src.storage.models import SourceType

_SUITE = "ssrf"

_INTERNAL_IPS = [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",  # loopback / RFC1918
    "169.254.169.254", "100.100.100.200",  # cloud metadata (link-local / Alibaba)
    "100.64.0.1", "0.0.0.0", "0.1.2.3",  # CGNAT / this-host
    "::1", "fc00::1", "fe80::1",  # IPv6 loopback / ULA / link-local
    "::ffff:10.0.0.1", "::ffff:169.254.169.254",  # IPv4-mapped
]
_PUBLIC_IPS = ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]


def _validate_ip_matrix(rec: Recorder) -> bool:
    for ip in _INTERNAL_IPS:
        if _validate_ip(ip):
            rec.record("validate_ip", ip=ip, wrongly_accepted=True)
            return False
    for ip in _PUBLIC_IPS:
        if not _validate_ip(ip):
            rec.record("validate_ip", ip=ip, wrongly_rejected=True)
            return False
    rec.record("validate_ip", internal_blocked=len(_INTERNAL_IPS), public_allowed=len(_PUBLIC_IPS))
    return True


def _encoded_ip_canonicalized_and_blocked(rec: Recorder) -> bool:
    # decimal/hex/octal encodings of 127.0.0.1 must canonicalize to an internal IP.
    for enc in ("2130706433", "0x7f000001", "0177.0.0.1"):
        canon = _host_as_ip(enc)
        if canon is None or _validate_ip(canon):
            rec.record("host_as_ip", enc=enc, canon=canon)
            return False
    rec.record("host_as_ip", encodings_blocked=3)
    return True


def _off_allowlist_unverified(rec: Recorder) -> bool:
    for host in ("evil.example", "sec.gov.evil.example", "evilsec.gov", "notsec.gov"):
        if source_type_for_host(host) != SourceType.UNVERIFIED:
            rec.record("source_type_for_host", host=host, leaked=True)
            return False
    if source_type_for_host("www.sec.gov") == SourceType.UNVERIFIED:  # control: a real host is allowlisted
        rec.record("source_type_for_host", host="www.sec.gov", control_failed=True)
        return False
    rec.record("source_type_for_host", off_allowlist_blocked=4)
    return True


class _NetTripwire(BaseException):
    """Subclasses BaseException so it cannot be swallowed by fetch_excerpt's blanket
    ``except Exception`` (which would mask a network-reach regression as
    'internal_error'). A true loud tripwire for the offline guarantee."""


def _raise_resolver(*_a, **_k):
    raise _NetTripwire("getaddrinfo must not be called (pre-DNS reject expected)")


def _resolver_to(*ips):
    def _f(host, port, *_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _f


@contextmanager
def _net(resolver):
    def _no_http(self, *_a, **_k):
        raise _NetTripwire("no real HTTP allowed in evals")

    with mock.patch.object(socket, "getaddrinfo", resolver), mock.patch.object(requests.Session, "send", _no_http):
        yield


def _fetch_blocks_vectors(rec: Recorder) -> bool:
    vectors = [
        ("http://www.sec.gov/x", _raise_resolver, "blocked_scheme"),  # non-https (pre-DNS)
        ("https://sec.gov@169.254.169.254/x", _raise_resolver, "blocked_scheme"),  # userinfo spoof (pre-DNS)
        ("https://evil.example/x", _raise_resolver, "off_allowlist"),  # off-allowlist (pre-DNS)
        ("https://169.254.169.254/x", _raise_resolver, "blocked_private_ip"),  # raw metadata IP (pre-DNS)
        ("https://www.sec.gov/x", _resolver_to("169.254.169.254"), "blocked_private_ip"),  # DNS rebinding
    ]
    for url, resolver, expected in vectors:
        with _net(resolver):
            result = fetch_excerpt(url)
        rec.record("fetch_excerpt", url=url, reason=result.reason, expected=expected, ok=result.ok)
        if result.ok or result.reason != expected:
            return False
    return True


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("validate_ip_matrix", _SUITE, CodeGrader("ssrf.validate_ip_matrix", _validate_ip_matrix), inputs={"internal": len(_INTERNAL_IPS), "public": len(_PUBLIC_IPS)}),
        EvalCase("encoded_ip_canonicalized_and_blocked", _SUITE, CodeGrader("ssrf.encoded_ip_canonicalized_and_blocked", _encoded_ip_canonicalized_and_blocked)),
        EvalCase("off_allowlist_unverified", _SUITE, CodeGrader("ssrf.off_allowlist_unverified", _off_allowlist_unverified)),
        EvalCase("fetch_blocks_vectors", _SUITE, CodeGrader("ssrf.fetch_blocks_vectors", _fetch_blocks_vectors)),
    ]
