"""Doc-guard: §19 bind-host README hardening (PR #68 deferred follow-up).

The §19 non-loopback gate keys on the ``SERVER_BIND_HOST`` env var, NOT uvicorn's
``--host`` flag — ``uvicorn --host 0.0.0.0`` with the env unset bypasses the import-time
env gate, but the ``NonLoopbackServeGuard`` runtime backstop (wired in
``app/backend/main.py``) still refuses every non-loopback arrival until an approved counsel
sign-off is recorded. ``app/run.sh`` closes the launch-command seam by deriving ``--host`` from the variable
(``--host "${SERVER_BIND_HOST:-127.0.0.1}"``); a bare README command must model the
same coupling. These pin both READMEs' uvicorn examples to set SERVER_BIND_HOST
explicitly and to explain why, so a doc edit cannot regress to the gate-blind command.

A per-line negative scan closes the confirmed HIGH seam: EVERY line that invokes
``uvicorn main:app`` must carry ``SERVER_BIND_HOST`` on that same line, so a future
edit adding a second, gate-blind example (e.g. ``poetry run uvicorn main:app --host
0.0.0.0``) fails the guard instead of slipping through a "≥1 compliant example" check.
The scan covers both READMEs plus ``docs/observing-pools-v0-usage.md`` (issue #70 —
the usage doc's launch command was left gate-blind by the 2026-07-04 two-README
scope-lock); the README-specific prose assertions stay scoped to the READMEs.
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
# Per-line gate-blind scan covers every doc with a runnable launch command (#70);
# the coupling-explanation prose tests stay scoped to _READMES.
_SCANNED_DOCS = _READMES + (_REPO / "docs" / "observing-pools-v0-usage.md",)
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


def test_no_uvicorn_mainapp_line_in_any_scanned_doc_is_gate_blind():
    """Per-line scan: no `uvicorn main:app` command may omit SERVER_BIND_HOST.

    Stronger than "≥1 compliant example exists" — a future edit that adds a second,
    gate-blind invocation (``poetry run uvicorn main:app --host 0.0.0.0``) turns this
    RED, whereas the existence check above would stay green. Scans _SCANNED_DOCS
    (both READMEs + the usage doc, #70), not just _READMES.
    """
    for doc in _SCANNED_DOCS:
        offenders = _gate_blind_uvicorn_lines(doc.read_text())
        assert not offenders, (
            f"{doc.relative_to(_REPO)} has `uvicorn main:app` command(s) that do not set "
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


def test_both_readmes_name_the_runtime_backstop():
    """Pin the runtime-backstop claim this branch added to both §19 blockquotes.

    This branch rewrote both READMEs' §19 blockquotes to claim the ``NonLoopbackServeGuard``
    runtime backstop catches a gate-blind ``uvicorn --host 0.0.0.0`` at request time — the
    layer that survives when someone binds non-loopback without ``SERVER_BIND_HOST`` set.
    Deleting those paragraphs would silently regress the docs to the false pre-branch claim
    that the import-time env gate is the only non-loopback defence, while every existing
    doc-guard test above stays green (they assert only the env-var/flag coupling, never the
    backstop). This is the same existence-check blindness PR #69's HIGH finding closed for the
    per-line ``uvicorn main:app`` scan.
    """
    for readme in _READMES:
        assert "NonLoopbackServeGuard" in readme.read_text(), (
            f"{readme.relative_to(_REPO)} §19 blockquote must name the `NonLoopbackServeGuard` "
            "runtime backstop — without it the docs regress to claiming the import-time env gate "
            "is the only non-loopback defence, which a gate-blind `uvicorn --host 0.0.0.0` defeats"
        )
