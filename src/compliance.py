"""Research-only compliance primitives (PRD v4 §9.9, B5).

The disclaimer is carried as data and enforced as an invariant at serialization
(``serialize_report``) and at the DB layer (NOT NULL). This module is the single
source of the disclaimer text/version. Ranked, per-security output retains
regulatory character even with this disclaimer — counsel sign-off is a hard gate
before any non-loopback exposure (PRD §19).
"""

import ipaddress
import os
from collections.abc import Mapping

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
    enforces the precondition at bind time. GUARANTEE SCOPE: the gate keys on ``SERVER_BIND_HOST``,
    so it protects deploys whose uvicorn ``--host`` derives from that env var (as ``app/run.sh``
    does); a hand-rolled ``uvicorn --host 0.0.0.0`` that leaves ``SERVER_BIND_HOST`` unset would
    bypass it — non-loopback deploys MUST set ``SERVER_BIND_HOST``. ``env`` defaults to ``os.environ`` (injectable
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
