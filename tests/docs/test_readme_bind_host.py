"""Doc-guard: §19 bind-host README hardening (PR #68 deferred follow-up).

The §19 non-loopback gate keys on the ``SERVER_BIND_HOST`` env var, NOT uvicorn's
``--host`` flag — ``uvicorn --host 0.0.0.0`` with the env unset silently bypasses the
gate. ``app/run.sh`` closes that seam by deriving ``--host`` from the variable
(``--host "${SERVER_BIND_HOST:-127.0.0.1}"``); a bare README command must model the
same coupling. These pin both READMEs' uvicorn examples to set SERVER_BIND_HOST
explicitly and to explain why, so a doc edit cannot regress to the gate-blind command.

A per-line negative scan closes the confirmed HIGH seam: EVERY line that invokes
``uvicorn main:app`` must carry ``SERVER_BIND_HOST`` on that same line, so a future
edit adding a second, gate-blind example (e.g. ``poetry run uvicorn main:app --host
0.0.0.0``) fails the guard instead of slipping through a "≥1 compliant example" check.
A companion test pins ``app/run.sh``'s derivation byte-exact
(``--host "${SERVER_BIND_HOST:-127.0.0.1}"``) — the fastest-rotting claim this
docstring makes — so every claim here is now enforced.

Pure text assertions — no import, no boot (sibling of test_prd_reconcile.py).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_READMES = (_REPO / "app" / "backend" / "README.md", _REPO / "app" / "README.md")
_RUN_SH = _REPO / "app" / "run.sh"

_UVICORN_MAINAPP = "uvicorn main:app"
_BIND_HOST_ENV = "SERVER_BIND_HOST"
# Byte-exact derivation the §19 docstring/blockquotes promise app/run.sh performs.
_RUN_SH_DERIVATION = '--host "${SERVER_BIND_HOST'


def _gate_blind_uvicorn_lines(text: str) -> list[str]:
    """Every line invoking ``uvicorn main:app`` that omits SERVER_BIND_HOST.

    A non-empty result IS the seam: a runnable command that starts the server
    without setting the env var the §19 gate keys on. Blockquotes that mention
    ``uvicorn --host 0.0.0.0`` without the ``main:app`` token do not match.
    """
    return [
        line
        for line in text.splitlines()
        if _UVICORN_MAINAPP in line and _BIND_HOST_ENV not in line
    ]


def test_both_readmes_set_server_bind_host_in_their_uvicorn_examples():
    for readme in _READMES:
        text = readme.read_text()
        assert re.search(r"SERVER_BIND_HOST=127\.0\.0\.1 .*uvicorn main:app", text), (
            f"{readme.relative_to(_REPO)} must prefix its uvicorn example with SERVER_BIND_HOST=127.0.0.1 "
            "— the §19 gate reads the env var, not the uvicorn --host flag"
        )


def test_both_readmes_explain_the_env_var_not_flag_coupling():
    for readme in _READMES:
        text = readme.read_text()
        assert "SERVER_BIND_HOST" in text and "--host" in text, (
            f"{readme.relative_to(_REPO)} must explain that the §19 gate checks SERVER_BIND_HOST, "
            "not the uvicorn --host flag, and that the two must stay in sync"
        )
        assert "sign-off" in text, (
            f"{readme.relative_to(_REPO)} must mention the counsel sign-off required for a "
            "non-loopback bind (§19)"
        )


def test_no_uvicorn_mainapp_line_in_either_readme_is_gate_blind():
    """Per-line scan: no `uvicorn main:app` command may omit SERVER_BIND_HOST.

    Stronger than "≥1 compliant example exists" — a future edit that adds a second,
    gate-blind invocation (``poetry run uvicorn main:app --host 0.0.0.0``) turns this
    RED, whereas the existence check above would stay green.
    """
    for readme in _READMES:
        offenders = _gate_blind_uvicorn_lines(readme.read_text())
        assert not offenders, (
            f"{readme.relative_to(_REPO)} has `uvicorn main:app` command(s) that do not set "
            f"SERVER_BIND_HOST on the same line — the §19 gate would silently not fire: {offenders}"
        )


def test_run_sh_derives_uvicorn_host_from_server_bind_host():
    """Pin app/run.sh's byte-exact derivation the §19 docstring quotes.

    ``app/run.sh`` is the launch path this module claims keeps ``--host`` coupled to
    ``SERVER_BIND_HOST``; without this assertion that claim could rot silently. Raw
    text only — run.sh is neither executed nor edited here.
    """
    text = _RUN_SH.read_text()
    assert _RUN_SH_DERIVATION in text, (
        "app/run.sh must derive uvicorn's --host from SERVER_BIND_HOST "
        '(expected `--host "${SERVER_BIND_HOST:-127.0.0.1}"`) so the launch path cannot bind '
        "non-loopback without the §19 gate seeing it — the module docstring quotes this line"
    )
