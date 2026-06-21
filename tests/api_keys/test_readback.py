"""Read-back close (PRD v4 §9.10, X6): no route ever returns key_value; responses
carry is_set + masked_tail only. Fully offline (FastAPI TestClient + in-memory
StaticPool; KEY_ENCRYPTION forced off so the cipher is identity and no keyring is
touched). Tests encode WHY: a future route re-adding from_orm of a key-bearing schema
must fail, so we assert both 'no key_value' AND 'masked_tail populated'.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.backend.repositories.api_key_repository as repo_mod
from app.backend.database.connection import Base, get_db
from app.backend.database.models import ApiKey
from app.backend.routes.api_keys import router
from app.backend.services import crypto
from app.backend.services.crypto import CryptoMasterKeyError, KeyCipher, TAG


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


def test_short_key_masked_to_constant_no_length_leak(client):
    # A constant-length sentinel for keys < 4 chars: the response must NOT reveal the
    # exact length, so a 3-char and a 1-char key both mask to the same "****".
    three = _post(client, "OPENAI_API_KEY", "abc").json()
    one = _post(client, "ANTHROPIC_API_KEY", "x").json()
    assert three["is_set"] is True and three["masked_tail"] == "****"
    assert one["masked_tail"] == "****"  # length not leaked


def test_empty_key_value_rejected_cannot_blank_a_key(client):
    # min_length=1 on ApiKeyCreateRequest -> an empty write is a 422, so it can never
    # silently overwrite/blank a stored key.
    assert _post(client, "OPENAI_API_KEY", "").status_code == 422


def test_openapi_schema_has_no_key_value(client):
    schema = client.app.openapi()
    props = schema["components"]["schemas"]["ApiKeyResponse"]["properties"]
    assert "key_value" not in props  # regression guard if someone re-adds the field
    assert "is_set" in props and "masked_tail" in props


def test_deactivate_projects_through_service(client):
    client.post("/api-keys/", json={"provider": "OPENAI_API_KEY", "key_value": "sk-secret-zzzz", "is_active": True})
    r = client.patch("/api-keys/OPENAI_API_KEY/deactivate")
    assert r.status_code == 200
    body = r.json()
    assert "key_value" not in body
    # The deactivated row is found (no is_active filter) and projected through the service,
    # so is_set/masked_tail are correct — not the schema defaults a from_orm(None) would force.
    assert body["is_set"] is True and body["masked_tail"] == "zzzz"


def _client_with_engine(monkeypatch, cipher):
    """A TestClient whose api-key repository uses an injected cipher, plus the engine so
    a test can inspect the raw stored ciphertext."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(repo_mod, "get_cipher", lambda db: cipher)  # the real repo/service wiring path
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: Session()
    return TestClient(app), engine


def test_encryption_on_stores_ciphertext_but_response_shows_plaintext_tail(monkeypatch):
    # The seam where both halves of the PR meet: with encryption ON the DB holds
    # ciphertext, yet masked_tail is the PLAINTEXT last-4 (not the ciphertext tail).
    key = Fernet.generate_key()
    on_cipher = KeyCipher(enabled=True, fernet_provider=lambda: Fernet(key))
    client, engine = _client_with_engine(monkeypatch, on_cipher)

    body = client.post("/api-keys/", json={"provider": "OPENAI_API_KEY", "key_value": "sk-secret-1234", "is_active": True}).json()
    assert "key_value" not in body and body["masked_tail"] == "1234"

    stored = sessionmaker(bind=engine)().query(ApiKey).filter_by(provider="OPENAI_API_KEY").first().key_value
    assert stored.startswith(TAG) and "sk-secret-1234" not in stored  # at-rest ciphertext, real get_cipher path


def test_crypto_master_key_error_never_leaks_key_in_500(monkeypatch):
    # The C1 fix: a CryptoMasterKeyError (whose message could carry sensitive text) must
    # be caught and replaced with a generic 500 — its detail must never reach the body.
    class _RaisingCipher:
        def encrypt(self, raw):
            raise CryptoMasterKeyError("set AHF_MASTER_KEY to this freshly generated key: SUPERSECRETKEY123456")

        def decrypt(self, stored):
            raise CryptoMasterKeyError("SUPERSECRETKEY123456")

    client, _ = _client_with_engine(monkeypatch, _RaisingCipher())
    r = client.post("/api-keys/", json={"provider": "OPENAI_API_KEY", "key_value": "sk-x", "is_active": True})
    assert r.status_code == 500
    assert "SUPERSECRETKEY123456" not in r.text  # no key material in the response body
    assert "See server logs" in r.json()["detail"]
