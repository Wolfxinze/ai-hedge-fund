"""§19 non-loopback startup guard (PRD §19): the process refuses to bind a non-loopback
host unless an approved counsel sign-off is recorded. The legal act itself stays human —
this only makes the *precondition* enforced-at-bind instead of documentation-only.

Pure-function tests (no server boot): ``enforce_nonloopback_signoff`` reads SERVER_BIND_HOST
+ COUNSEL_SIGNOFF_PATH from an injected env mapping and reuses evals ``signoff_recorded``.
Loopback / unset / localhost / ::1 / 127.0.0.0/8 are no-ops (dev + CI unchanged); any other
bind host with no approved sign-off fails loud.
"""

import pytest
from fastapi.testclient import TestClient

from src.compliance import enforce_nonloopback_signoff
from src.evals.reporting import record_signoff


def _env(host=None, signoff_path=None):
    env = {}
    if host is not None:
        env["SERVER_BIND_HOST"] = host
    if signoff_path is not None:
        env["COUNSEL_SIGNOFF_PATH"] = str(signoff_path)
    return env


@pytest.mark.parametrize("host", [None, "", "127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.5", "  127.0.0.1  "])
def test_loopback_and_unset_never_raise(host, tmp_path, monkeypatch):
    """Loopback/unset is a PURE no-op — proven by the absence of side effects, not merely the
    absence of a raise: the caller's env mapping is unmutated, no sign-off file is created at the
    configured path, the filesystem around it stays untouched, and the sign-off ledger is never
    consulted (no read-through). The last property closes a read-through escape: a refactor that
    hoists the lazy ``from src.evals.reporting import signoff_recorded`` + its call above the
    loopback early-return would keep the other assertions green while breaking the
    lazy-import/no-consultation contract — so we monkeypatch ``signoff_recorded`` to fail loud."""
    monkeypatch.setattr(
        "src.evals.reporting.signoff_recorded",
        lambda p: pytest.fail("loopback path must not consult the sign-off ledger"),
    )
    signoff_path = tmp_path / "nope.jsonl"
    env = _env(host=host, signoff_path=signoff_path)
    before = dict(env)
    assert enforce_nonloopback_signoff(env) is None
    assert env == before, "the no-op path must not mutate the caller's env mapping"
    assert not signoff_path.exists(), "the no-op path must never create the sign-off file"
    assert list(tmp_path.iterdir()) == [], "the no-op path must leave the filesystem untouched"


def test_non_loopback_without_signoff_raises_naming_19_path_and_command(tmp_path):
    path = tmp_path / "signoff.jsonl"
    with pytest.raises(RuntimeError) as exc:
        enforce_nonloopback_signoff(_env(host="0.0.0.0", signoff_path=path))
    msg = str(exc.value)
    assert "§19" in msg, "message must name the §19 gate"
    assert str(path) in msg, "message must name the expected sign-off path"
    assert "record_signoff" in msg, "message must name the record command"


def test_non_loopback_with_unapproved_signoff_still_raises(tmp_path):
    path = tmp_path / "signoff.jsonl"
    record_signoff(path, reviewer="counsel", notes="reviewed, not approved", approved=False)
    with pytest.raises(RuntimeError):
        enforce_nonloopback_signoff(_env(host="0.0.0.0", signoff_path=path))


def test_non_loopback_with_approved_signoff_passes(tmp_path):
    path = tmp_path / "signoff.jsonl"
    record_signoff(path, reviewer="counsel", notes="approved for shared exposure", approved=True)
    enforce_nonloopback_signoff(_env(host="0.0.0.0", signoff_path=path))  # no raise


def test_unresolvable_hostname_fails_closed(tmp_path):
    # A bind host we cannot prove is loopback is treated as non-loopback (fail-closed).
    with pytest.raises(RuntimeError):
        enforce_nonloopback_signoff(_env(host="example.com", signoff_path=tmp_path / "signoff.jsonl"))


def test_gate_is_wired_into_main_at_import(monkeypatch, tmp_path):
    """The gate must be CALLED at app.backend.main import — not merely defined in compliance.

    WHY: every test above exercises the pure function in isolation, so a refactor that deletes or
    relocates the ``enforce_nonloopback_signoff()`` call in ``app/backend/main.py`` would leave the
    whole suite green while silently re-opening the exact non-loopback exposure §19 closes. This pins
    the call SITE: importing the app under a non-loopback ``SERVER_BIND_HOST`` with no approved
    sign-off must raise ``RuntimeError`` before the FastAPI app (and any bind) is built."""
    import importlib
    import sys

    monkeypatch.setenv("SERVER_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "no-signoff.jsonl"))
    sys.modules.pop("app.backend.main", None)  # force the module body (and the gate) to re-run
    try:
        with pytest.raises(RuntimeError, match="§19"):
            importlib.import_module("app.backend.main")
    finally:
        # A raised import never caches; pop again so a later clean import re-runs from a fresh state.
        sys.modules.pop("app.backend.main", None)


def test_serve_guard_refuses_non_loopback_arrival_wired_into_main(monkeypatch, tmp_path):
    """The §19 layer-2 serve guard must be REGISTERED on ``app.backend.main.app`` — not merely
    defined in compliance and exercised in isolation.

    WHY: every ``NonLoopbackServeGuard`` unit test drives the class directly with hand-built
    scopes, so deleting the ``app.add_middleware(NonLoopbackServeGuard)`` line in main.py would
    leave the whole suite green while silently re-opening the hand-rolled ``uvicorn --host 0.0.0.0``
    seam layer 2 exists to close. This pins the call SITE: a request ARRIVING on a non-loopback
    local address with no approved sign-off must be refused (503) by the assembled app. It MUST go
    RED if that add_middleware line is removed."""
    # Imported plainly under the default (loopback) env and used via its ``app`` object — never
    # reloaded with a non-loopback SERVER_BIND_HOST set (that raises at import by design).
    import app.backend.main as main

    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "no-signoff.jsonl"))
    # Starlette TestClient derives scope['server'] from base_url: 'http://203.0.113.9' -> ('203.0.113.9', 80),
    # a non-loopback local address. DENY is never cached, so this host stays deny-able for later tests.
    client = TestClient(main.app, base_url="http://203.0.113.9")
    # Instant root health route (not the multi-second SSE ``/ping`` stream — fast RED feedback); the
    # guard's 503 verdict is endpoint-agnostic, so any route the assembled app exposes proves the wire.
    resp = client.get("/")
    assert resp.status_code == 503
    # The assembled app's refusal body names the §19 gate (excludes a coincidental 503 from a future
    # layer) yet never leaks the sign-off ledger path — pinned on the ASSEMBLED app, not the bare class.
    assert "§19" in resp.text
    assert str(tmp_path) not in resp.text


def test_serve_guard_does_not_refuse_the_default_testclient_suite(monkeypatch, tmp_path):
    """The default-base_url TestClient host ('testserver') is not an IP, so the guard passes it
    through and the endpoint serves normally.

    WHY: registering the guard as the outermost middleware must be invisible to the hundreds of
    existing ``TestClient(main.app)`` tests (whose scope['server'] is ('testserver', 80)) — the
    guard can only 503 a real, parseable, non-loopback socket address, never a harness hostname.
    (Uses the instant root health route rather than the multi-second SSE ``/ping`` stream — either
    proves passthrough, and the guard's verdict is endpoint-agnostic.)"""
    import app.backend.main as main

    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(tmp_path / "no-signoff.jsonl"))
    client = TestClient(main.app)  # default base_url 'http://testserver' -> unparseable host -> allowed
    resp = client.get("/")
    assert resp.status_code // 100 == 2


def test_serve_guard_serves_non_loopback_arrival_with_approved_signoff(monkeypatch, tmp_path):
    """With an approved counsel sign-off recorded, a non-loopback arrival is served — proving the
    guard's ALLOW branch is wired, not only its refusal.

    WHY: a mis-wire that always refused (or always served) would still pass the deny test above;
    this pins that the assembled app honours a recorded sign-off. Uses a DISTINCT non-loopback host
    (203.0.113.10) and a UNIQUE ledger path so the guard's per-host ALLOW cache (module-cached on
    the shared ``main.app``) cannot leak an ALLOW verdict onto the deny test's host."""
    import app.backend.main as main

    ledger = tmp_path / "signoff.jsonl"
    record_signoff(ledger, reviewer="counsel", notes="approved for shared exposure", approved=True)
    monkeypatch.setenv("COUNSEL_SIGNOFF_PATH", str(ledger))
    client = TestClient(main.app, base_url="http://203.0.113.10")
    resp = client.get("/")  # instant root health route (not the multi-second SSE /ping); guard is endpoint-agnostic
    assert resp.status_code == 200
