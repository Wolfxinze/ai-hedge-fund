"""Research-only compliance primitives (PRD v4 §9.9, B5).

The disclaimer is carried as data and enforced as an invariant at serialization
(``serialize_report``) and at the DB layer (NOT NULL). This module is the single
source of the disclaimer text/version. Ranked, per-security output retains
regulatory character even with this disclaimer — counsel sign-off is a hard gate
before any non-loopback exposure (PRD §19).
"""

import ipaddress
import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

DISCLAIMER_VERSION = os.environ.get("DISCLAIMER_VERSION", "2026-06")

# §19 non-loopback exposure gate. The default sign-off ledger shares the NAME documented in
# docs/evals.md (gitignored ``evals_runs/``); override with COUNSEL_SIGNOFF_PATH. Note this
# constant is a CWD-relative path (unlike the evals run-dir, which is repo-root-anchored), so a
# non-loopback deploy should pass an absolute COUNSEL_SIGNOFF_PATH or run from the repo root.
DEFAULT_COUNSEL_SIGNOFF_PATH = "evals_runs/signoff.jsonl"
# Bind hosts treated as loopback by name (case-insensitive). Numeric loopback
# (127.0.0.0/8, ::1) is detected via ipaddress, so it need not be enumerated here.
_LOOPBACK_NAMES = frozenset({"", "localhost"})

DISCLAIMER = (
    "Research and educational use only. This output is not investment advice, not a "
    "recommendation to buy or sell any security, and carries no guarantee of accuracy or "
    "performance. It contains no trade-execution instructions. Descriptive labels and "
    "promote/hold/demote statuses describe research priority, not trading directives. "
    "Conduct your own due diligence; consult a licensed professional before investing."
)


def research_disclaimer() -> tuple[str, str]:
    """Return (disclaimer_text, disclaimer_version) for stamping records/reports."""
    return DISCLAIMER, DISCLAIMER_VERSION


def _is_loopback_host(host: str) -> bool:
    """True for loopback bind hosts (localhost, 127.0.0.0/8, ::1; the empty string counts as
    localhost). A host we cannot prove is loopback (an unparseable name, a public IP, 0.0.0.0)
    → False, so the gate fails closed. (The unset→127.0.0.1 default is resolved by the caller.)"""
    h = host.strip().lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def enforce_nonloopback_signoff(env: Mapping[str, str] | None = None) -> None:
    """§19 gate: refuse to bind a non-loopback host without an approved counsel sign-off.

    Loopback / unset / dev / CI bind hosts are a pure no-op — behaviour is byte-for-byte
    unchanged. For any other ``SERVER_BIND_HOST`` (0.0.0.0, a public IP, an unresolvable
    name), an approved sign-off must already be recorded at ``COUNSEL_SIGNOFF_PATH``
    (default ``evals_runs/signoff.jsonl``); otherwise raise ``RuntimeError`` so the
    process exits non-zero *before* binding. The legal act itself stays human — this only
    enforces the precondition at bind time. GUARANTEE SCOPE (two layers): this env gate is
    layer 1 — it keys on ``SERVER_BIND_HOST`` and fails *before* the socket binds (best UX:
    the process never comes up), covering deploys whose uvicorn ``--host`` derives from that
    env var (as ``app/run.sh`` does). A hand-rolled ``uvicorn --host 0.0.0.0`` that leaves
    ``SERVER_BIND_HOST`` unset slips past layer 1, but is caught by layer 2 —
    ``NonLoopbackServeGuard`` (wired in ``app/backend/main.py``), a per-connection runtime
    backstop that refuses every non-loopback arrival with no approved sign-off. On such a
    deploy, loopback arrivals are still served: the guard keys on the per-connection *local*
    address, so localhost traffic (which lands on 127.0.0.1 even under a 0.0.0.0 bind) passes
    while LAN/public arrivals are refused. Residual seam: a local reverse proxy or port-forward
    (nginx, ``ssh -L``, socat) fronting a loopback bind -- or a ``--uds`` deploy, whose
    ``scope['server']`` is None and is passed through as served -- arrives on loopback and so is
    outside BOTH layers, so a §19 sign-off is still required before fronting the app that way.
    ``env`` defaults to ``os.environ`` (injectable
    for tests). ``signoff_recorded`` is imported lazily to avoid pulling the evals stack at
    module load and to keep the reporting dependency one-directional (no import cycle)."""
    env = os.environ if env is None else env
    host = (env.get("SERVER_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if _is_loopback_host(host):
        return
    signoff_path = (env.get("COUNSEL_SIGNOFF_PATH") or DEFAULT_COUNSEL_SIGNOFF_PATH).strip() or DEFAULT_COUNSEL_SIGNOFF_PATH

    from src.evals.reporting import signoff_recorded  # lazy: no evals import at module load, no cycle

    if signoff_recorded(signoff_path):
        return
    raise RuntimeError(
        f"§19 counsel sign-off gate: refusing to bind non-loopback SERVER_BIND_HOST={host!r} "
        f"without an approved counsel sign-off at {signoff_path!r}. Record one with: "
        f"python -c 'from src.evals.reporting import record_signoff; "
        f'record_signoff("{signoff_path}", reviewer="counsel", notes="...", approved=True)\' '
        f"(PRD §19), or bind a loopback host (SERVER_BIND_HOST=127.0.0.1)."
    )


# Refusal body sent to external clients: it names the §19 gate but MUST NOT disclose the ledger
# filesystem path (no path leak off-box; the full detail goes to the server-side log instead).
_SERVE_REFUSAL_BODY = (
    "Service unavailable: this connection arrived on a non-loopback address and is refused by "
    "the §19 counsel sign-off gate. Non-loopback exposure requires a recorded, approved counsel "
    "sign-off (PRD §19)."
).encode("utf-8")


class NonLoopbackServeGuard:
    """§19 layer-2 runtime backstop: a pure-ASGI middleware that refuses non-loopback arrivals
    with no approved counsel sign-off (PRD §19). Pairs with ``enforce_nonloopback_signoff`` (the
    layer-1 env gate): that gate fails before bind on a ``SERVER_BIND_HOST`` deploy; this guard
    catches a hand-rolled ``uvicorn --host 0.0.0.0`` that left ``SERVER_BIND_HOST`` unset, per
    connection, at request time.

    Empirically (2026-07-08) real uvicorn reports ``scope['server'][0]`` as the NUMERIC
    per-connection LOCAL address via ``sockname``: bind 127.0.0.1 -> '127.0.0.1'; bind 0.0.0.0 +
    loopback arrival -> '127.0.0.1'; bind 0.0.0.0 + LAN arrival -> the LAN IP (e.g. '192.168.68.58').
    Hostname strings like 'testserver' come ONLY from test harnesses, never a real socket.

    Behaviour:
      * ``scope['type']`` not in ('http', 'websocket') -> passthrough untouched (e.g. lifespan).
      * no ``scope['server']`` -> passthrough (debug log; not a bound connection we can judge).
      * host = ``server[0]``; if it does not parse as an IP after ``.strip().strip('[]')`` ->
        passthrough (debug log; a hostname string cannot be a real uvicorn socket).
      * loopback IP -> passthrough; the sign-off ledger is NEVER consulted. An IPv4-mapped
        loopback address ('::ffff:127.0.0.1', which a local client lands on under a dual-stack
        ``uvicorn --host ::`` bind and which has is_loopback == False on py3.12) counts as loopback.
      * non-loopback IP -> lazily consult ``signoff_recorded(COUNSEL_SIGNOFF_PATH)``: approved ->
        serve; otherwise refuse (http -> 503; websocket -> close 1008) and do NOT call the inner app.

    Caching: ALLOW verdicts (loopback / unparseable / signed-off non-loopback) are cached per host
    string in an instance set (O(1) steady state; the local-address set is tiny). DENY is NEVER
    cached -- recording a sign-off must take effect on the very next request without a restart.
    Revocation is one-way at process granularity: deleting the ledger or appending an approved:false
    line does NOT affect an already-cached ALLOW host until process restart, and ``signoff_recorded``
    treats any approved line as permanent -- to revoke, delete the ledger AND restart.

    ``env`` defaults to ``os.environ`` (read at REQUEST time, so the live process env is honoured),
    matching this module's injectable-env style. ``signoff_recorded`` is imported lazily INSIDE the
    non-loopback branch so importing this module never pulls the evals stack and the loopback path
    never touches it."""

    def __init__(self, app, env: Mapping[str, str] | None = None):
        self.app = app
        self._env = env
        self._allow: set = set()

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        server = scope.get("server")
        if not server:
            logger.debug("NonLoopbackServeGuard: no server in scope; passthrough")
            return await self.app(scope, receive, send)
        host = server[0]
        if host in self._allow:
            return await self.app(scope, receive, send)
        try:
            ip = ipaddress.ip_address(str(host).strip().strip("[]"))
        except ValueError:
            logger.debug("NonLoopbackServeGuard: host %r is not an IP; passthrough (not a uvicorn socket)", host)
            self._allow.add(host)
            return await self.app(scope, receive, send)
        if ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_loopback):
            self._allow.add(host)
            return await self.app(scope, receive, send)

        env = os.environ if self._env is None else self._env
        signoff_path = (env.get("COUNSEL_SIGNOFF_PATH") or DEFAULT_COUNSEL_SIGNOFF_PATH).strip() or DEFAULT_COUNSEL_SIGNOFF_PATH

        from src.evals.reporting import signoff_recorded  # lazy: no evals import at module load, no cycle

        if signoff_recorded(signoff_path):
            self._allow.add(host)
            return await self.app(scope, receive, send)

        # Refuse. Full detail (incl. the ledger path + record command) goes to the server-side log
        # ONLY; the client body names §19 but never the path. DENY is not cached (see docstring).
        logger.error(
            "§19 NonLoopbackServeGuard: refusing non-loopback arrival on host=%r without an approved "
            "counsel sign-off at %r. Record one with: python -c 'from src.evals.reporting import "
            'record_signoff; record_signoff(%r, reviewer="counsel", notes="...", approved=True)\' (PRD §19).',
            host,
            signoff_path,
            signoff_path,
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(_SERVE_REFUSAL_BODY)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 503, "headers": headers})
        await send({"type": "http.response.body", "body": _SERVE_REFUSAL_BODY})
