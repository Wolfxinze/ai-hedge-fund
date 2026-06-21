"""Read-back close (PRD v4 §9.10, X6): no route ever returns key_value; responses
carry is_set + masked_tail only. Fully offline (FastAPI TestClient + in-memory
StaticPool; KEY_ENCRYPTION forced off so the cipher is identity and no keyring is
touched). Tests encode WHY: a future route re-adding from_orm of a key-bearing schema
must fail, so we assert both 'no key_value' AND 'masked_tail populated'.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backend.database.connection import Base, get_db
from app.backend.routes.api_keys import router
from app.backend.services import crypto


@pytest.fixture(autouse=True)
def _force_plaintext(monkeypatch):
    # Deterministic + never touches the real OS keyring: KEY_ENCRYPTION off => identity cipher.
    monkeypatch.delenv("KEY_ENCRYPTION", raising=False)
    crypto.reset_crypto_cache()
    yield
    crypto.reset_crypto_cache()


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: Session()
    return TestClient(app)


def _post(client, provider, key):
    return client.post("/api-keys/", json={"provider": provider, "key_value": key, "is_active": True})


def test_post_returns_masked_not_key_value(client):
    r = _post(client, "OPENAI_API_KEY", "sk-secret-1234")
    assert r.status_code == 200
    body = r.json()
    assert "key_value" not in body  # the leak is closed
    assert body["is_set"] is True
    assert body["masked_tail"] == "1234"  # last 4 of the plaintext


def test_get_detail_never_leaks_key(client):
    _post(client, "OPENAI_API_KEY", "sk-secret-abcd")
    body = client.get("/api-keys/OPENAI_API_KEY").json()
    assert "key_value" not in body
    assert body["is_set"] is True and body["masked_tail"] == "abcd"


def test_list_carries_is_set_and_masked_tail(client):
    _post(client, "OPENAI_API_KEY", "sk-aaaa1111")
    _post(client, "ANTHROPIC_API_KEY", "sk-ant-2222")
    items = client.get("/api-keys/").json()
    assert {i["provider"] for i in items} == {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    for i in items:
        assert "key_value" not in i  # frontend drives off the list alone, no per-key readback
        assert i["is_set"] is True and len(i["masked_tail"]) == 4


def test_put_and_bulk_never_leak(client):
    _post(client, "XAI_API_KEY", "xai-old0")
    put = client.put("/api-keys/XAI_API_KEY", json={"key_value": "xai-new9"})
    assert put.status_code == 200 and "key_value" not in put.json()
    assert put.json()["masked_tail"] == "new9"
    bulk = client.post("/api-keys/bulk", json={"api_keys": [{"provider": "GROQ_API_KEY", "key_value": "gsk-z9z9", "is_active": True}]})
    assert bulk.status_code == 200
    assert all("key_value" not in item for item in bulk.json())


def test_short_key_is_fully_masked(client):
    body = _post(client, "OPENAI_API_KEY", "abc").json()  # len 3 < 4
    assert body["is_set"] is True and body["masked_tail"] == "***"  # never reveals a too-short key


def test_empty_key_value_rejected_cannot_blank_a_key(client):
    # min_length=1 on ApiKeyCreateRequest -> an empty write is a 422, so it can never
    # silently overwrite/blank a stored key.
    assert _post(client, "OPENAI_API_KEY", "").status_code == 422


def test_openapi_schema_has_no_key_value(client):
    schema = client.app.openapi()
    props = schema["components"]["schemas"]["ApiKeyResponse"]["properties"]
    assert "key_value" not in props  # regression guard if someone re-adds the field
    assert "is_set" in props and "masked_tail" in props
