"""Issue #5: observing-pools API routes bound their limit and reject malformed
tickers (422) — a loopback tool still shouldn't accept unbounded result sets or
unvalidated path params.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.observing_pools import router


def _client() -> TestClient:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_reports_limit_is_bounded():
    c = _client()
    assert c.get("/opportunity-reports?limit=10000").status_code == 422  # over le=200
    assert c.get("/opportunity-reports?limit=0").status_code == 422  # under ge=1
    assert c.get("/opportunity-reports?limit=50").status_code == 200


def test_serenity_limit_is_bounded():
    c = _client()
    assert c.get("/serenity/research/NVDA?limit=10000").status_code == 422
    assert c.get("/serenity/research/NVDA?limit=50").status_code == 200


def test_serenity_rejects_malformed_ticker():
    c = _client()
    assert c.get("/serenity/research/TOOOOOOOOOOOOOOOOLONG").status_code == 422  # >16 chars
    assert c.get("/serenity/research/NVDA").status_code == 200  # valid → empty list
