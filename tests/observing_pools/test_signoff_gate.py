"""§19 non-loopback startup guard (PRD §19): the process refuses to bind a non-loopback
host unless an approved counsel sign-off is recorded. The legal act itself stays human —
this only makes the *precondition* enforced-at-bind instead of documentation-only.

Pure-function tests (no server boot): ``enforce_nonloopback_signoff`` reads SERVER_BIND_HOST
+ COUNSEL_SIGNOFF_PATH from an injected env mapping and reuses evals ``signoff_recorded``.
Loopback / unset / localhost / ::1 / 127.0.0.0/8 are no-ops (dev + CI unchanged); any other
bind host with no approved sign-off fails loud.
"""

import pytest

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
def test_loopback_and_unset_never_raise(host, tmp_path):
    # No sign-off file exists, yet a loopback/unset bind is a pure no-op (byte-for-byte unchanged).
    enforce_nonloopback_signoff(_env(host=host, signoff_path=tmp_path / "nope.jsonl"))


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
