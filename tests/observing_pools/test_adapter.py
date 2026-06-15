"""Tests for ``run_analyzing_flow`` — every failure mode must degrade to an
``insufficient-evidence`` label at confidence 0.0, never a fabricated signal.

subprocess.run and os.path.exists are monkeypatched so NO process is ever spawned.
"""

import subprocess
import types

import pytest

import src.integrations.tradingagents_adapter as adapter
from src.integrations.tradingagents_adapter import run_analyzing_flow
from src.storage.models import ReportLabel

TICKER = "NVDA"
TRADE_DATE = "2026-06-12"


def _assert_degraded(result, *, needle: str):
    assert result.degraded is True
    assert result.label == ReportLabel.INSUFFICIENT_EVIDENCE
    assert result.confidence == 0.0
    assert needle in (result.error or "")


def _fake_completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_runner_missing_degrades(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: False)
    result = run_analyzing_flow(TICKER, TRADE_DATE)
    _assert_degraded(result, needle="runner not found")


def test_timeout_degrades(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: True)

    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="runner", timeout=kwargs.get("timeout", 900))

    monkeypatch.setattr(adapter.subprocess, "run", _raise)
    result = run_analyzing_flow(TICKER, TRADE_DATE, timeout=900)
    _assert_degraded(result, needle="timed out")


def test_nonzero_exit_includes_stderr_tail(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: True)
    stderr = "Traceback: boom the runner crashed badly"
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **k: _fake_completed(returncode=1, stderr=stderr),
    )
    result = run_analyzing_flow(TICKER, TRADE_DATE)
    _assert_degraded(result, needle="crashed badly")
    assert "exited 1" in result.error


def test_last_line_json_maps_to_result(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: True)
    # Leading log noise then a final valid JSON line — only the last line is parsed.
    stdout = "INFO loading graph...\n" "DEBUG running debate...\n" '{"ok": true, "decision": "buy", "reports": {"final_trade_decision": "Strong thesis support."}}\n'
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **k: _fake_completed(returncode=0, stdout=stdout),
    )
    result = run_analyzing_flow(TICKER, TRADE_DATE)
    assert result.degraded is False
    assert result.label == ReportLabel.THESIS_SUPPORTIVE
    assert result.raw_decision == "buy"
    assert result.summary == "Strong thesis support."


def test_unparseable_stdout_degrades(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **k: _fake_completed(returncode=0, stdout="not json at all"),
    )
    result = run_analyzing_flow(TICKER, TRADE_DATE)
    _assert_degraded(result, needle="unparseable")


def test_empty_stdout_degrades(monkeypatch):
    monkeypatch.setattr(adapter.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **k: _fake_completed(returncode=0, stdout=""),
    )
    result = run_analyzing_flow(TICKER, TRADE_DATE)
    _assert_degraded(result, needle="unparseable")
