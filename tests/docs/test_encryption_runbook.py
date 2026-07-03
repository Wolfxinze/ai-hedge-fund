"""Doc-guard: the API-key encryption runbook (issue #66-C) exists, is complete, and is cross-linked.

These pin the runbook against silent drift: the ordered rotation procedure, the exit-code table, and
the mid-rotation data-loss warning (#66-A) must be present, and both scripts + PRD §9.10 must point
to it. Pure text assertions — no import, no boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RUNBOOK = _REPO / "docs" / "api-key-encryption-runbook.md"
_ROTATE = _REPO / "app" / "backend" / "scripts" / "rotate_master_key.py"
_REENCRYPT = _REPO / "app" / "backend" / "scripts" / "reencrypt_api_keys.py"
_PRD = _REPO / "docs" / "prd-observing-pools.md"

_RUNBOOK_NAME = "api-key-encryption-runbook.md"


def test_runbook_exists():
    assert _RUNBOOK.is_file(), "docs/api-key-encryption-runbook.md must exist (#66-C)"


def test_runbook_covers_both_scripts():
    text = _RUNBOOK.read_text()
    assert "reencrypt_api_keys" in text, "runbook must cover the sweep script"
    assert "rotate_master_key" in text, "runbook must cover the rotation script"


@pytest.mark.parametrize(
    "needle",
    [
        "AHF_MASTER_KEY_NEW",  # set new key (env)
        "--dry-run",  # preview
        "quiesce",  # stop the backend
        "repoint",  # repoint the master key
        "restart",  # restart the backend
        "verify",  # verify
        "retire",  # retire the old key
    ],
)
def test_runbook_documents_each_ordered_step(needle):
    assert needle.lower() in _RUNBOOK.read_text().lower(), f"runbook must document the {needle!r} step"


def test_runbook_lists_all_three_exit_codes():
    text = _RUNBOOK.read_text()
    assert "Exit codes" in text
    for code in ("`0`", "`1`", "`2`"):
        assert code in text, f"runbook exit-code table must document {code}"


def test_runbook_states_mid_rotation_warning_and_why():
    text = _RUNBOOK.read_text().lower()
    assert "cached in memory" in text, "runbook must state WHY (running backend caches the old key)"
    assert "#66-a" in text, "runbook must reference the #66-A data-loss issue"
    assert "no cross-process lock" in text, "runbook must be explicit there is no locking"


def test_runbook_never_puts_the_new_key_on_the_cli():
    # The invariant is env-only key material; the runbook must say so, not model a CLI arg for the key.
    text = _RUNBOOK.read_text().lower()
    assert "never on the cli" in text or "never on the command line" in text or "never pass it on the cli" in text


def test_both_scripts_link_the_runbook():
    assert _RUNBOOK_NAME in _ROTATE.read_text(), "rotate_master_key docstring must link the runbook"
    assert _RUNBOOK_NAME in _REENCRYPT.read_text(), "reencrypt_api_keys docstring must link the runbook"


def test_prd_9_10_links_the_runbook():
    prd = _PRD.read_text()
    assert _RUNBOOK_NAME in prd, "PRD §9.10 must link the encryption runbook"
