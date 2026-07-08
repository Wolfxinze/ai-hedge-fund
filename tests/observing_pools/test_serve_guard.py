"""§19 runtime backstop (PRD §19): ``NonLoopbackServeGuard`` is layer 2 behind the
``enforce_nonloopback_signoff`` env gate. WHY it exists: the layer-1 gate keys on
``SERVER_BIND_HOST``, so a hand-rolled ``uvicorn --host 0.0.0.0`` that leaves that env
var unset binds a public interface with no sign-off check at all. This guard closes that
hole per-connection: real uvicorn reports ``scope['server'][0]`` as the NUMERIC local
address the connection actually landed on (bind 127.0.0.1 → '127.0.0.1'; bind 0.0.0.0 +
loopback arrival → '127.0.0.1'; bind 0.0.0.0 + LAN arrival → the LAN IP), so a
non-loopback arrival with no approved counsel sign-off is refused at request time.

These tests drive the pure-ASGI middleware directly with hand-built scopes and a trivial
recording inner app — no server boot. Two contracts are pinned beyond "it refuses":
(a) the loopback / unparseable paths NEVER consult the sign-off ledger — proven with a
fail-loud ``signoff_recorded`` monkeypatch (mirrors test_signoff_gate.py) so a refactor
that hoists the lazy import above the loopback short-circuit goes RED; (b) DENY is never
cached (record a sign-off → next request on the same instance is served) while ALLOW is
cached (a signed-off host is not re-consulted). The refusal body must name the §19 gate
but must NOT leak the ledger filesystem path to external clients.
"""

import asyncio

import pytest

from src.compliance import NonLoopbackServeGuard
from src.evals.reporting import record_signoff


class _RecordingApp:
    """Trivial inner ASGI app: records that it was reached and returns a 200 for http."""

    def __init__(self):
        self.called = False
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(server):
    return {"type": "http", "server": server, "path": "/x", "headers": []}


def _ws_scope(server):
    return {"type": "websocket", "server": server, "path": "/x", "headers": []}


def _drive(guard, scope):
    """Run one request through the guard, returning the list of ASGI messages it sent."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    return sent


def _status(sent):
    for msg in sent:
        if msg.get("type") == "http.response.start":
            return msg["status"]
    return None


def _body(sent):
    return b"".join(msg.get("body", b"") for msg in sent if msg.get("type") == "http.response.body")


# IPv4, IPv6, bracketed-IPv6, and IPv4-MAPPED non-loopback arrivals. The IPv6 cases close the
# proven fail-open seam where a parse-narrowing regression (ip_address → IPv4Address) would fail
# to parse every IPv6 arrival and serve it unsigned with the suite green; the bracketed form pins
# the ``.strip('[]')`` normalization; the mapped form ('::ffff:203.0.113.9', the real shape of an
# IPv4 LAN arrival under a dual-stack ``--host ::`` bind) pins the ``.is_loopback`` half of the
# mapped-loopback clause — weakening it to "any mapped address" must go RED here, not stay green.
@pytest.mark.parametrize("host", ["203.0.113.9", "2001:db8::1", "[2001:db8::1]", "::ffff:203.0.113.9"])
def test_non_loopback_without_signoff_refuses_503_naming_19_without_path(host, monkeypatch, tmp_path):
    ledger = tmp_path / "no-signoff.jsonl"
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    inner = _RecordingApp()
    guard = NonLoopbackServeGuard(inner)
    sent = _drive(guard, _http_scope((host, 80)))
    assert _status(sent) == 503
    body = _body(sent).decode("utf-8")
    assert "§19" in body, "refusal body must name the §19 counsel sign-off gate"
    assert str(ledger) not in body, "refusal body must NOT leak the ledger filesystem path"
    assert inner.called is False, "the inner app must not run on refusal"


def test_non_loopback_with_approved_signoff_is_served(monkeypatch, tmp_path):
    ledger = tmp_path / "signoff.jsonl"
    record_signoff(ledger, reviewer="counsel", notes="approved for shared exposure", approved=True)
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    inner = _RecordingApp()
    sent = _drive(NonLoopbackServeGuard(inner), _http_scope(("203.0.113.9", 80)))
    assert inner.called is True
    assert _status(sent) == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.5", "::ffff:127.0.0.1"])
def test_loopback_hosts_served_and_ledger_never_consulted(host, monkeypatch, tmp_path):
    # A loopback arrival must be served WITHOUT reading the sign-off ledger — fail loud if it is,
    # so a refactor that hoists the lazy consult above the loopback short-circuit goes RED.
    # '::ffff:127.0.0.1' (IPv4-mapped loopback under a dual-stack ``uvicorn --host ::`` bind) has
    # is_loopback == False on py3.12 but must still count as loopback — this is the RED-first pin
    # for the ipv4_mapped code fix.
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("loopback arrival must not consult the sign-off ledger"),
    )
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "nope.jsonl"))
    inner = _RecordingApp()
    _drive(NonLoopbackServeGuard(inner), _http_scope((host, 80)))
    assert inner.called is True


def test_unparseable_host_served_and_ledger_never_consulted(monkeypatch, tmp_path):
    # 'testserver' (test-harness hostname, not a uvicorn socket) does not parse as an IP → passthrough.
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("unparseable host must not consult the sign-off ledger"),
    )
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "nope.jsonl"))
    inner = _RecordingApp()
    _drive(NonLoopbackServeGuard(inner), _http_scope(("testserver", 80)))
    assert inner.called is True


def test_missing_server_in_scope_is_served(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("no-server scope must not consult the sign-off ledger"),
    )
    inner = _RecordingApp()
    scope = _http_scope(None)
    _drive(NonLoopbackServeGuard(inner), scope)
    assert inner.called is True


def test_allow_verdict_is_cached_per_host(monkeypatch, tmp_path):
    ledger = tmp_path / "signoff.jsonl"
    record_signoff(ledger, reviewer="counsel", notes="ok", approved=True)
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    inner = _RecordingApp()
    guard = NonLoopbackServeGuard(inner)
    _drive(guard, _http_scope(("203.0.113.9", 80)))  # first: consults ledger, caches ALLOW
    assert inner.called is True
    # After the ALLOW is cached, a second request on the SAME instance must not re-consult.
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("cached ALLOW host must not re-consult the sign-off ledger"),
    )
    inner.called = False
    _drive(guard, _http_scope(("203.0.113.9", 80)))
    assert inner.called is True


def test_deny_verdict_is_not_cached(monkeypatch, tmp_path):
    # A recorded sign-off must take effect on the NEXT request without a restart → DENY is never cached.
    ledger = tmp_path / "signoff.jsonl"
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    inner = _RecordingApp()
    guard = NonLoopbackServeGuard(inner)
    sent = _drive(guard, _http_scope(("203.0.113.9", 80)))  # no ledger yet → refuse
    assert _status(sent) == 503
    assert inner.called is False
    record_signoff(ledger, reviewer="counsel", notes="now approved", approved=True)
    sent2 = _drive(guard, _http_scope(("203.0.113.9", 80)))  # same instance → re-consults → serve
    assert inner.called is True
    assert _status(sent2) == 200


def test_websocket_non_loopback_without_signoff_closes_1008(monkeypatch, tmp_path):
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "no-signoff.jsonl"))
    inner = _RecordingApp()
    sent = _drive(NonLoopbackServeGuard(inner), _ws_scope(("203.0.113.9", 80)))
    assert {"type": "websocket.close", "code": 1008} in sent
    assert not any(m.get("type") == "websocket.accept" for m in sent), "must refuse without accepting"
    assert inner.called is False


def test_websocket_non_loopback_with_approved_signoff_is_served(monkeypatch, tmp_path):
    # A signed-off non-loopback websocket must reach the inner app and NOT be closed — guards a
    # refactor that hoists the ws refusal above the sign-off consult.
    ledger = tmp_path / "signoff.jsonl"
    record_signoff(ledger, reviewer="counsel", notes="approved for shared exposure", approved=True)
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    inner = _RecordingApp()
    sent = _drive(NonLoopbackServeGuard(inner), _ws_scope(("203.0.113.9", 80)))
    assert inner.called is True
    assert not any(m.get("type") == "websocket.close" for m in sent), "approved ws must not be closed"


def test_lifespan_scope_passes_through_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("non-http/websocket scope must not consult the sign-off ledger"),
    )
    inner = _RecordingApp()
    scope = {"type": "lifespan"}
    _drive(NonLoopbackServeGuard(inner), scope)
    assert inner.called is True
    assert inner.scopes[0] is scope, "lifespan scope must be passed through untouched"


def test_module_load_does_not_import_evals():
    """Importing src.compliance must NOT pull the evals stack — the ledger consult stays lazy."""
    import importlib
    import sys

    sys.modules.pop("src.compliance", None)
    sys.modules.pop("src.evals.reporting", None)
    importlib.import_module("src.compliance")
    assert "src.evals.reporting" not in sys.modules, "src.compliance must not import evals at module load"
    importlib.import_module("src.evals.reporting")  # restore for the rest of the session
