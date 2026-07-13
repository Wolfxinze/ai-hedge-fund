"""CLI wiring tests for the local ``--formula-version`` rh1 experimentation flag.

The default (``v3-4comp``) path must stay byte-identical to pre-flag behavior:
``formula_version`` defaults to FORMULA_4COMP and ``fetch_closes`` is never wired.
An rh1 flag must construct a ``RefreshConfig`` with that version AND thread the
production ``price_bridge.fetch_closes_via_provider`` into the refresh call (proven
by stubbing that provider and asserting it is actually invoked). Everything below the
CLI (the analyst committee, the DB, the pool lock) is mocked so the test is hermetic.
"""

import sys
import types
from contextlib import contextmanager
from unittest import mock

import pytest

from src.observing_pools import cli
from src.observing_pools.scoring import (
    FORMULA_4COMP,
    FORMULA_4COMP_RH1,
    FORMULA_5COMP_RH1,
)
from src.storage.models import RefreshRunStatus

_PLATFORM = cli.PLATFORM_KEYS[0]
_COMPLETE = RefreshRunStatus.COMPLETE.value
_SUMMARY = {"ranked": 0, "data_unavailable": 0, "top_tickers": []}


@contextmanager
def _fake_scope():
    yield mock.Mock()


def _run_refresh(monkeypatch, argv, stub):
    """Invoke ``cli.main(argv)`` with every side-effecting collaborator mocked.

    ``refresh_pool`` / ``refresh_pool_locked`` record the ``RefreshConfig`` and the
    ``fetch_closes`` they receive, and consult ``fetch_closes`` when it is non-None
    (mirroring the real pipeline) so the stub records a genuine invocation.
    """
    rec: dict = {}

    def _fake_run(fetch_closes, end_date):
        if fetch_closes is not None:
            fetch_closes("AAPL", end_date)  # the pipeline consults the provider per ticker
        run = mock.Mock()
        run.status, run.error, run.summary = _COMPLETE, None, dict(_SUMMARY)
        return run

    def fake_refresh_pool(session, config, runner, *, end_date, provider_name="yfinance", fetch_closes=None):
        rec["config"] = config
        rec["dry_fetch_closes"] = fetch_closes
        return _fake_run(fetch_closes, end_date)

    def fake_refresh_pool_locked(config, runner, *, end_date, run_id, fetch_closes=None, **_kw):
        rec["config"] = config
        rec["locked_fetch_closes"] = fetch_closes
        return _fake_run(fetch_closes, end_date)

    fake_sg = types.ModuleType("src.observing_pools.scoring_graph")
    fake_sg.run_scoring_analysts = lambda *a, **k: ({}, {"calls": 0})
    monkeypatch.setitem(sys.modules, "src.observing_pools.scoring_graph", fake_sg)

    monkeypatch.setattr(cli, "refresh_pool", fake_refresh_pool)
    monkeypatch.setattr(cli, "refresh_pool_locked", fake_refresh_pool_locked)
    monkeypatch.setattr(cli, "session_scope", _fake_scope)
    monkeypatch.setattr(cli, "Base", mock.Mock())
    monkeypatch.setattr("src.observing_pools.price_bridge.fetch_closes_via_provider", stub)

    rc = cli.main(argv)
    return rc, rec


def test_default_formula_version_choice():
    args = cli.build_parser().parse_args(["refresh", "--platform", _PLATFORM])
    assert args.formula_version == FORMULA_4COMP


def test_no_flag_keeps_default_and_never_wires_fetch_closes(monkeypatch):
    # AC1: omitting --formula-version → default v3-4comp, fetch_closes never wired/invoked.
    stub = mock.MagicMock(return_value=[1.0, 2.0, 3.0])
    rc, rec = _run_refresh(monkeypatch, ["refresh", "--platform", _PLATFORM, "--dry-run"], stub)
    assert rc == 0
    assert rec["config"].formula_version == FORMULA_4COMP
    assert rec["dry_fetch_closes"] is None
    assert not stub.called


def test_rh1_flag_wires_and_invokes_fetch_closes_dry_run(monkeypatch):
    # AC2: rh1 flag → RefreshConfig(formula_version=rh1) AND a non-None fetch_closes
    # that is actually invoked (the stub records a call).
    stub = mock.MagicMock(return_value=[1.0, 2.0, 3.0])
    rc, rec = _run_refresh(
        monkeypatch, ["refresh", "--platform", _PLATFORM, "--dry-run", "--formula-version", FORMULA_4COMP_RH1], stub
    )
    assert rc == 0
    assert rec["config"].formula_version == FORMULA_4COMP_RH1
    assert rec["dry_fetch_closes"] is not None
    assert stub.called


def test_rh1_flag_wires_fetch_closes_locked_path(monkeypatch):
    # AC2 (non-dry-run): the same wiring reaches refresh_pool_locked.
    stub = mock.MagicMock(return_value=[1.0])
    rc, rec = _run_refresh(
        monkeypatch, ["refresh", "--platform", _PLATFORM, "--formula-version", FORMULA_5COMP_RH1], stub
    )
    assert rc == 0
    assert rec["config"].formula_version == FORMULA_5COMP_RH1
    assert rec["locked_fetch_closes"] is not None
    assert stub.called


def test_invalid_formula_version_rejected_by_argparse():
    # AC3: an unknown value is rejected with a non-zero exit, not silently defaulted.
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["refresh", "--platform", _PLATFORM, "--formula-version", "bogus"])
    assert exc.value.code != 0
