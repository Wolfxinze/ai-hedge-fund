"""§11.5 durability: the deterministic substantiation ``reason`` is now persisted on
``evidence_references.reason`` (previously observability-only, logged then dropped).

Two guarantees:
- ``build_record`` writes ``classify_reference``'s already-computed reason onto each
  EvidenceReference row (``figure_missing`` for a wrong-number claim, ``ok`` when it
  substantiates). Column is nullable Text — no API/serializer change (out of scope).
- The Alembic migration adds the column off the current head and round-trips clean on
  SQLite (upgrade → downgrade drops it → upgrade re-adds), proving batch-mode DROP works.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity.research import build_record

_REPO = Path(__file__).resolve().parents[2]

# A claim whose figure (40%) is contradicted by the excerpt (25%) → unsubstantiated,
# reason "figure_missing" (mirrors the §11.5 wrong-figure gate).
_CLAIM = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
_WRONG_FIG = "the filing notes gallium nitride wafer supply fell 25% on a capacity bottleneck this year"
_MATCH = "the filing notes gallium nitride wafer supply fell 40% on a capacity bottleneck this year"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _evidence(session, rec):
    return session.query(m.EvidenceReference).filter_by(record_id=rec.id).one()


def test_build_record_persists_withheld_reason(session):
    refs = [{"source_url": "https://sec.gov/doc", "claim_summary": _CLAIM, "excerpt": _WRONG_FIG}]
    rec = build_record(session, theme="t", references=refs, scorecard={})
    session.commit()
    ev = _evidence(session, rec)
    assert ev.substantiated is False
    assert ev.reason == "figure_missing", "the withheld-grade reason must be persisted, not just logged"


def test_build_record_persists_ok_reason_when_substantiated(session):
    refs = [{"source_url": "https://sec.gov/doc", "claim_summary": _CLAIM, "excerpt": _MATCH}]
    rec = build_record(session, theme="t", references=refs, scorecard={})
    session.commit()
    ev = _evidence(session, rec)
    assert ev.substantiated is True
    assert ev.reason == "ok", "a substantiated reference persists reason='ok'"


def test_reason_column_is_nullable_text():
    col = m.EvidenceReference.__table__.columns["reason"]
    assert col.nullable is True, "reason must be nullable (backfill-free add)"


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_REPO / "app" / "backend" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {c["name"] for c in inspect(engine).get_columns("evidence_references")}
    finally:
        engine.dispose()


def test_migration_reason_column_roundtrips_clean_on_sqlite(tmp_path):
    db = tmp_path / "roundtrip.db"
    cfg = _alembic_cfg(db)

    command.upgrade(cfg, "head")
    assert "reason" in _columns(db), "upgrade to head must add evidence_references.reason"

    command.downgrade(cfg, "-1")
    cols = _columns(db)
    assert "reason" not in cols, "downgrade must drop the column (batch-mode recreate on SQLite)"
    # the table itself and its other columns survive the batch recreate
    assert {"id", "record_id", "source_url", "substantiated"} <= cols

    command.upgrade(cfg, "+1")
    assert "reason" in _columns(db), "re-upgrade must re-add the column (idempotent round-trip)"
