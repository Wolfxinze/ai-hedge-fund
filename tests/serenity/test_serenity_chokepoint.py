"""serialize_serenity disclaimer chokepoint (PRD §9.9/§12, issue #23).

The serenity research GET projection now routes through serialize_serenity, so its
disclaimer is enforced at the serialization layer (parity with serialize_report),
not only by the DB NOT NULL + CHECK. Offline: in-memory SQLite + TestClient.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.observing_pools import router
from src.monitoring.serialize import DisclaimerError, serialize_serenity


# ── unit: the chokepoint refuses a blank/whitespace disclaimer ───────────────
def _rec(disclaimer, version):
    return m.SerenityResearchRecord(ticker="NVDA", platform_key="ai", theme="gallium nitride", evidence_grade="B", serenity_score=70.0, disclaimer=disclaimer, disclaimer_version=version)


@pytest.mark.parametrize("disclaimer,version", [("", "2026-06"), ("Research only.", ""), ("   ", "2026-06"), ("Research only.", "  "), ("\xa0", "2026-06")])
def test_serialize_serenity_refuses_blank(disclaimer, version):
    # The last case (\xa0 non-breaking space) PASSES the SQLite CHECK's ASCII trim() but
    # FAILS Python .strip() — proving the serialization + DB layers compose.
    with pytest.raises(DisclaimerError):
        serialize_serenity(_rec(disclaimer, version))


def test_serialize_serenity_projects_valid_record():
    out = serialize_serenity(_rec("Research and educational use only.", "2026-06"))
    assert out["disclaimer"] == "Research and educational use only." and out["disclaimer_version"] == "2026-06"
    assert out["ticker"] == "NVDA" and out["theme"] == "gallium nitride" and out["evidence_grade"] == "B"
    assert "scorecard" not in out  # shape matches the route's existing projection (no raw scorecard leak)


# ── route: GET /serenity/research/{ticker} goes through the chokepoint ────────
@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: Session()
    return TestClient(app), Session


def test_route_carries_disclaimer_through_chokepoint(client):
    tc, Session = client
    s = Session()
    s.add(m.SerenityResearchRecord(ticker="NVDA", platform_key="ai", theme="t", evidence_grade="A", serenity_score=90.0, disclaimer="Research only.", disclaimer_version="2026-06"))
    s.commit()
    s.close()
    body = tc.get("/serenity/research/NVDA").json()
    assert len(body) == 1 and body[0]["disclaimer"] == "Research only." and body[0]["disclaimer_version"] == "2026-06"


def test_route_refuses_record_with_nbsp_disclaimer(client):
    # A \xa0 disclaimer is admitted by the DB CHECK but must be refused by the route's
    # serialize_serenity chokepoint — the route raises (re-surfaced by TestClient).
    tc, Session = client
    s = Session()
    s.add(m.SerenityResearchRecord(ticker="NVDA", platform_key="ai", theme="t", disclaimer="\xa0", disclaimer_version="2026-06"))
    s.commit()
    s.close()
    with pytest.raises(DisclaimerError):
        tc.get("/serenity/research/NVDA")
