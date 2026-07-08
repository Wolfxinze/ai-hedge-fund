"""Doc-guard: §19 bind-host README hardening (PR #68 deferred follow-up).

The §19 non-loopback gate keys on the ``SERVER_BIND_HOST`` env var, NOT uvicorn's
``--host`` flag — ``uvicorn --host 0.0.0.0`` with the env unset silently bypasses the
gate. ``app/run.sh`` closes that seam by deriving ``--host`` from the variable
(``--host "${SERVER_BIND_HOST:-127.0.0.1}"``); a bare README command must model the
same coupling. These pin both READMEs' uvicorn examples to set SERVER_BIND_HOST
explicitly and to explain why, so a doc edit cannot regress to the gate-blind command.

Pure text assertions — no import, no boot (sibling of test_prd_reconcile.py).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_READMES = (_REPO / "app" / "backend" / "README.md", _REPO / "app" / "README.md")


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
