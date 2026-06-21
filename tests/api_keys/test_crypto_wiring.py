"""Encrypt-at-repo / decrypt-at-service wiring (PRD v4 §9.10, X5). Fully offline:
in-memory SQLite + an injected KeyCipher (no real keyring/env/network). Proves the
stored DB value is ciphertext when on, the service read decrypts, and the tag-gated
codec makes toggling safe end to end.
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backend.database.connection import Base
from app.backend.database.models import ApiKey
from app.backend.repositories.api_key_repository import ApiKeyRepository
from app.backend.services.api_key_service import ApiKeyService
from app.backend.services.crypto import CryptoError, KeyCipher, TAG

_KEY_A = Fernet.generate_key()
_KEY_B = Fernet.generate_key()


def _on(key=_KEY_A):
    return KeyCipher(enabled=True, fernet_provider=lambda: Fernet(key))


def _off():
    return KeyCipher(enabled=False, fernet_provider=lambda: pytest.fail("off cipher must not resolve a key"))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _raw_stored(session, provider):
    return session.query(ApiKey).filter_by(provider=provider).first().key_value


def test_write_encrypts_and_service_read_decrypts(session):
    ApiKeyRepository(session, cipher=_on()).create_or_update_api_key("OPENAI_API_KEY", "sk-secret-1234")
    stored = _raw_stored(session, "OPENAI_API_KEY")
    assert stored.startswith(TAG) and "sk-secret-1234" not in stored  # at-rest ciphertext
    svc = ApiKeyService(session, cipher=_on())
    assert svc.get_api_key("OPENAI_API_KEY") == "sk-secret-1234"
    assert svc.get_api_keys_dict()["OPENAI_API_KEY"] == "sk-secret-1234"


def test_off_stores_plaintext_backward_compatible(session):
    ApiKeyRepository(session, cipher=_off()).create_or_update_api_key("OPENAI_API_KEY", "sk-plain")
    assert _raw_stored(session, "OPENAI_API_KEY") == "sk-plain"  # untouched plaintext
    assert ApiKeyService(session, cipher=_off()).get_api_key("OPENAI_API_KEY") == "sk-plain"


def test_toggle_on_reads_existing_plaintext_verbatim(session):
    # Wrote while OFF (plaintext), then read through an ON service -> tag-gated decrypt
    # returns it verbatim, NO InvalidToken. This is the no-migration safety guarantee.
    ApiKeyRepository(session, cipher=_off()).create_or_update_api_key("GROQ_API_KEY", "gsk-legacy")
    assert ApiKeyService(session, cipher=_on()).get_api_key("GROQ_API_KEY") == "gsk-legacy"


def test_wrong_master_key_fails_loud_not_silent(session):
    ApiKeyRepository(session, cipher=_on(_KEY_A)).create_or_update_api_key("OPENAI_API_KEY", "sk-secret")
    svc = ApiKeyService(session, cipher=_on(_KEY_B))  # rotated/wrong key
    with pytest.raises(CryptoError):
        svc.get_api_key("OPENAI_API_KEY")
    with pytest.raises(CryptoError):
        svc.get_api_keys_dict()  # fail-closed: a bad row fails the whole dict, never a wrong value


def test_update_api_key_encrypts(session):
    repo = ApiKeyRepository(session, cipher=_on())
    repo.create_or_update_api_key("XAI_API_KEY", "xai-old")
    repo.update_api_key("XAI_API_KEY", key_value="xai-new")
    assert _raw_stored(session, "XAI_API_KEY").startswith(TAG)
    assert ApiKeyService(session, cipher=_on()).get_api_key("XAI_API_KEY") == "xai-new"


def test_bulk_no_double_encrypt(session):
    repo = ApiKeyRepository(session, cipher=_on())
    repo.bulk_create_or_update([
        {"provider": "OPENAI_API_KEY", "key_value": "sk-a"},
        {"provider": "ANTHROPIC_API_KEY", "key_value": "sk-ant-b"},
    ])
    svc = ApiKeyService(session, cipher=_on())
    # Each decrypts ONCE back to the original — a double-encrypt would yield ciphertext.
    assert svc.get_api_key("OPENAI_API_KEY") == "sk-a"
    assert svc.get_api_key("ANTHROPIC_API_KEY") == "sk-ant-b"
    for p in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert not _raw_stored(session, p)[len(TAG):].startswith(TAG)  # not nested


def test_has_encrypted_rows_probe(session):
    repo_on = ApiKeyRepository(session, cipher=_on())
    assert repo_on.has_encrypted_rows() is False  # empty
    ApiKeyRepository(session, cipher=_off()).create_or_update_api_key("GROQ_API_KEY", "gsk-plain")
    assert repo_on.has_encrypted_rows() is False  # plaintext row is not "encrypted"
    repo_on.create_or_update_api_key("OPENAI_API_KEY", "sk-x")
    assert repo_on.has_encrypted_rows() is True  # now a tagged row exists
