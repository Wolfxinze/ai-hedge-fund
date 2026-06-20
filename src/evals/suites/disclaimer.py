"""Disclaimer-invariant suite (PRD v4 §12/§20): serialize chokepoint refuses a
blank disclaimer; the DB CHECK + NOT NULL reject one at the DB layer; the
disclaimer survives a sqlite3 logical dump/restore; and the canonical text is
non-directional. All offline — pure in-memory/temp-file SQLite, stdlib iterdump
(no sqlite3 subprocess), no LLM (the one model-grader uses a deterministic stub
judge to demonstrate the model tier).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.compliance import DISCLAIMER, DISCLAIMER_VERSION, research_disclaimer
from src.evals.core import CodeGrader, EvalCase, ModelGrader, Recorder
from src.evals.registry import suite
from src.monitoring.serialize import DisclaimerError, serialize_report
from src.storage.models import Base, OpportunityReport, SerenityResearchRecord

_SUITE = "disclaimer"


def _serialize_refuses_blank(rec: Recorder) -> bool:
    """serialize_report raises on every blank/whitespace corner (no DB needed)."""
    corners = [("", "2026-06"), ("Research only.", ""), ("   ", "2026-06"), ("Research only.", "  ")]
    for disc, ver in corners:
        report = OpportunityReport(ticker="NVDA", disclaimer=disc, disclaimer_version=ver)
        try:
            serialize_report(report)
        except DisclaimerError:
            rec.record("serialize_report", disclaimer=repr(disc), version=repr(ver), raised="DisclaimerError")
            continue
        rec.record("serialize_report", disclaimer=repr(disc), version=repr(ver), raised=None)
        return False  # a blank disclaimer serialized — invariant broken
    return True


def _serialize_passes_canonical(rec: Recorder) -> bool:
    disc, ver = research_disclaimer()
    out = serialize_report(OpportunityReport(ticker="NVDA", label="mixed", disclaimer=disc, disclaimer_version=ver))
    rec.record("serialize_report", ticker="NVDA", disclaimer_present=bool(out["disclaimer"]))
    return out["disclaimer"] == DISCLAIMER and out["disclaimer_version"] == DISCLAIMER_VERSION


def _db_check_rejects_blank(rec: Recorder) -> bool:
    """create_all carries the CHECK; a blank disclaimer is rejected at the DB layer
    for BOTH product tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        for row in (
            OpportunityReport(ticker="X", disclaimer="", disclaimer_version=""),
            SerenityResearchRecord(theme="t", disclaimer="", disclaimer_version=""),
        ):
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                rec.record("db_insert", table=row.__tablename__, rejected=True)
                session.rollback()
                continue
            session.rollback()
            return False  # blank disclaimer accepted — CHECK missing
        return True
    finally:
        session.close()
        engine.dispose()


def _disclaimer_survives_dump(rec: Recorder) -> bool:
    """The disclaimer is real persisted column data: it survives a logical
    sqlite3 .dump (via stdlib iterdump) + restore. Offline, no subprocess."""
    disc, ver = research_disclaimer()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        session.add(OpportunityReport(ticker="NVDA", disclaimer=disc, disclaimer_version=ver))
        session.commit()
        session.close()
        engine.dispose()

        conn = sqlite3.connect(path)
        dump_sql = "\n".join(conn.iterdump())
        conn.close()
        present_in_dump = disc in dump_sql

        fresh = sqlite3.connect(":memory:")
        fresh.executescript(dump_sql)
        row = fresh.execute("SELECT disclaimer, disclaimer_version FROM opportunity_reports").fetchone()
        fresh.close()

    rec.record("sqlite_dump", in_dump=present_in_dump, reloaded=bool(row))
    return present_in_dump and row is not None and row[0] == disc and row[1] == ver


def _stub_nondirectional_judge(prompt: str) -> bool:
    """Deterministic offline stand-in for an LLM judge: the disclaimer is
    non-directional iff it disclaims advice and is missing buy/sell directives.
    Tests the model-grader WIRING (not real judgment)."""
    text = DISCLAIMER.lower()
    return "not investment advice" in text and "not a recommendation to buy or sell" in text


def _nondirectional_label(rec: Recorder, judge) -> bool:
    rec.record("model_judge", grader="nondirectional_disclaimer")
    return judge("Is the disclaimer non-directional (no buy/sell directive)?")


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("serialize_refuses_blank", _SUITE, CodeGrader("disclaimer.serialize_refuses_blank", _serialize_refuses_blank), inputs={"corners": 4}),
        EvalCase("serialize_passes_canonical", _SUITE, CodeGrader("disclaimer.serialize_passes_canonical", _serialize_passes_canonical)),
        EvalCase("db_check_rejects_blank", _SUITE, CodeGrader("disclaimer.db_check_rejects_blank", _db_check_rejects_blank)),
        EvalCase("disclaimer_survives_dump", _SUITE, CodeGrader("disclaimer.survives_dump", _disclaimer_survives_dump)),
        EvalCase("nondirectional_label", _SUITE, ModelGrader("disclaimer.nondirectional_label", _nondirectional_label, judge=_stub_nondirectional_judge)),
    ]
