"""The Phase-11 disclaimer CHECK constraint must apply, enforce, and round-trip.

Proves the §12/§20 "DB CHECK" half: a blank disclaimer is rejected at the DB
layer (not only by serialize_report). Runs on a TEMP db (never the dev db) and
verifies the upgrade->enforced, downgrade->gone, upgrade->enforced cycle, plus
that create_all (model __table_args__) carries the same CHECK so the two agree.
"""

import os
import tempfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import src.storage.models as m

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _cfg(db_url: str) -> Config:
    cfg = Config(os.path.join(_REPO, "app/backend/alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO, "app/backend/alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _insert_blank_disclaimer(url: str, model: str) -> None:
    """Attempt to persist a blank-disclaimer row on the db at ``url`` (fresh engine)."""
    engine = create_engine(url)
    try:
        session = sessionmaker(bind=engine)()
        try:
            if model == "opportunity":
                row = m.OpportunityReport(ticker="X", disclaimer="", disclaimer_version="")
            else:
                row = m.SerenityResearchRecord(theme="t", disclaimer="", disclaimer_version="")
            session.add(row)
            session.flush()
        finally:
            session.rollback()
            session.close()
    finally:
        engine.dispose()


def test_disclaimer_check_applies_enforces_and_roundtrips():
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{os.path.join(d, 't.db')}"
        cfg = _cfg(url)

        command.upgrade(cfg, "head")

        # All product indexes AND the FK survive the batch table-recreate (a
        # batch-mode regression that drops an index/FK is the classic failure).
        insp = inspect(create_engine(url))
        opp_idx = {ix["name"] for ix in insp.get_indexes("opportunity_reports")}
        ser_idx = {ix["name"] for ix in insp.get_indexes("serenity_research_records")}
        assert {"ix_opportunity_reports_id", "ix_opportunity_reports_monitor_id", "ix_opportunity_reports_ticker"} <= opp_idx
        assert {"ix_serenity_research_records_id", "ix_serenity_research_records_platform_key", "ix_serenity_research_records_ticker"} <= ser_idx
        opp_fks = insp.get_foreign_keys("opportunity_reports")
        assert any(fk["referred_table"] == "monitor_configs" and fk["constrained_columns"] == ["monitor_id"] for fk in opp_fks), "monitor_id FK dropped by batch recreate"

        # After upgrade the CHECK is enforced: a blank disclaimer is rejected.
        for model in ("opportunity", "serenity"):
            with pytest.raises(IntegrityError):
                _insert_blank_disclaimer(url, model)

        # Downgrade removes the CHECK — a blank disclaimer now slips past NOT NULL
        # (this is the pre-Phase-11 gap; proving downgrade truly drops the constraint).
        command.downgrade(cfg, "b8f3c1a92d04")
        for model in ("opportunity", "serenity"):
            _insert_blank_disclaimer(url, model)  # must NOT raise

        # Upgrade again re-enforces (idempotent round-trip).
        command.upgrade(cfg, "head")
        for model in ("opportunity", "serenity"):
            with pytest.raises(IntegrityError):
                _insert_blank_disclaimer(url, model)


def test_create_all_model_carries_check():
    """create_all (model __table_args__) enforces the same CHECK, so the suite's
    in-memory tests and production migrations agree."""
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        for row in (
            m.OpportunityReport(ticker="X", disclaimer="", disclaimer_version=""),
            m.SerenityResearchRecord(theme="t", disclaimer="", disclaimer_version=""),
        ):
            session.add(row)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_valid_disclaimer_still_persists():
    """A real (non-blank) disclaimer is unaffected by the CHECK."""
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add(m.OpportunityReport(ticker="NVDA", disclaimer="Research only.", disclaimer_version="2026-06"))
        session.flush()  # must NOT raise
    finally:
        session.rollback()
        session.close()
        engine.dispose()
