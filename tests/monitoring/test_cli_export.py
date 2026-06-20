"""The `monitoring export` CLI must re-project persisted reports through the
disclaimer chokepoint (PRD §13/§20 export). Fully offline: in-process, an
in-memory StaticPool engine, no analyzing flow / LLM / subprocess.
"""

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.monitoring.cli as cli
import src.storage.models as m
from src.compliance import research_disclaimer


@pytest.fixture
def db(monkeypatch):
    """In-memory StaticPool engine wired into the CLI's engine + session_scope."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(cli, "engine", engine)
    monkeypatch.setattr(cli, "session_scope", _scope)
    return Session


def _seed(Session, ticker, disclaimer, version, monitor_id=None):
    s = Session()
    try:
        s.add(m.OpportunityReport(ticker=ticker, monitor_id=monitor_id, disclaimer=disclaimer, disclaimer_version=version))
        s.commit()
    finally:
        s.close()


def test_export_emits_disclaimer_for_every_report(db, capsys):
    disc, ver = research_disclaimer()
    _seed(db, "NVDA", disc, ver)
    _seed(db, "MSFT", disc, ver)

    rc = cli.main(["export"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {r["ticker"] for r in payload} == {"NVDA", "MSFT"}
    for r in payload:
        assert r["disclaimer"].strip() and r["disclaimer_version"].strip()
        assert r["disclaimer"] == disc  # canonical text, not just any truthy string


def test_export_filters_by_ticker(db, capsys):
    disc, ver = research_disclaimer()
    _seed(db, "NVDA", disc, ver)
    _seed(db, "MSFT", disc, ver)

    rc = cli.main(["export", "--ticker", "nvda"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["ticker"] for r in payload] == ["NVDA"]


def test_export_refuses_whitespace_only_disclaimer(db, capsys):
    """A '\\t\\n  ' disclaimer PASSES the SQLite CHECK's trim() (which strips only
    spaces) but FAILS serialize_report's .strip() — proving export routes through
    the chokepoint AND that the DB-CHECK + serialize layers compose."""
    _seed(db, "NVDA", "\t\n  ", "2026-06")  # admitted by the DB CHECK
    rc = cli.main(["export"])
    assert rc == 2
    assert "refusing to export" in capsys.readouterr().err


def test_export_empty_db_is_ok(db, capsys):
    rc = cli.main(["export"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
