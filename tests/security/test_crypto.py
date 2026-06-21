"""API-key crypto: tagged Fernet codec + master-key resolution (PRD v4 §9.10, X5).

Fully offline — every keyring/env/DB seam is injected, so NO real OS keyring (the
macOS Keychain on a dev box) is ever touched and nothing is non-deterministic. Tests
encode WHY: the asymmetric flag-gated-encrypt / tag-gated-decrypt is the property that
makes toggling encryption safe, and the has_encrypted_rows gate is the only guard
against catastrophic key regeneration.
"""

import pytest
from cryptography.fernet import Fernet

from app.backend.services import crypto
from app.backend.services.crypto import (
    _resolve_fernet,
    CryptoError,
    CryptoMasterKeyError,
    is_encryption_enabled,
    KeyCipher,
    TAG,
)

_KEY_A = Fernet.generate_key()
_KEY_B = Fernet.generate_key()


@pytest.fixture(autouse=True)
def _clear_cache():
    crypto.reset_crypto_cache()
    yield
    crypto.reset_crypto_cache()


def _cipher(enabled, fernet=None):
    return KeyCipher(enabled=enabled, fernet_provider=lambda: fernet or pytest.fail("fernet must not be resolved on this path"))


# ── is_encryption_enabled ────────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [("on", True), ("true", True), ("1", True), ("YES", True), ("off", False), ("", False), ("no", False), (None, False)])
def test_is_encryption_enabled(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("KEY_ENCRYPTION", raising=False)
    else:
        monkeypatch.setenv("KEY_ENCRYPTION", val)
    assert is_encryption_enabled() is expected


# ── KeyCipher: flag-gated encrypt ────────────────────────────────────────────
def test_encrypt_off_is_identity():
    # Off path never resolves a key (fernet_provider would fail the test if called).
    assert _cipher(enabled=False).encrypt("sk-secret") == "sk-secret"


def test_encrypt_on_tags_and_round_trips():
    c = _cipher(enabled=True, fernet=Fernet(_KEY_A))
    stored = c.encrypt("sk-secret")
    assert stored.startswith(TAG) and stored != "sk-secret"
    assert c.decrypt(stored) == "sk-secret"  # round-trip


def test_encrypt_is_idempotent_no_double_wrap():
    c = _cipher(enabled=True, fernet=Fernet(_KEY_A))
    once = c.encrypt("sk-secret")
    assert c.encrypt(once) == once  # already-tagged -> returned unchanged (bulk-safety)


# ── KeyCipher: tag-gated decrypt (the safety property) ───────────────────────
def test_decrypt_legacy_plaintext_returns_verbatim_even_when_enabled():
    # The core no-migration guarantee: an untagged (legacy plaintext) row written
    # before encryption was on still reads back verbatim AFTER turning it on — it is
    # NEVER fed to Fernet.decrypt, so there is no InvalidToken.
    c = _cipher(enabled=True, fernet=Fernet(_KEY_A))
    assert c.decrypt("sk-legacy-plaintext") == "sk-legacy-plaintext"


def test_decrypt_tagged_still_works_when_flag_off():
    # Toggle-off-with-ciphertext: decrypt is tag-gated, not flag-gated, so existing
    # ciphertext still decrypts when KEY_ENCRYPTION is off (as long as the key resolves).
    stored = TAG + Fernet(_KEY_A).encrypt(b"sk-secret").decode()
    c = _cipher(enabled=False, fernet=Fernet(_KEY_A))  # flag off, but key available
    assert c.decrypt(stored) == "sk-secret"


def test_decrypt_wrong_key_raises_loud_never_returns_ciphertext():
    stored = TAG + Fernet(_KEY_A).encrypt(b"sk-secret").decode()
    c = _cipher(enabled=True, fernet=Fernet(_KEY_B))  # rotated/wrong key
    with pytest.raises(CryptoError):
        c.decrypt(stored)


# ── master-key resolution order ──────────────────────────────────────────────
def _resolve(**over):
    base = dict(
        keyring_get=lambda ns, item: None,
        keyring_set=lambda ns, item, val: pytest.fail("keyring_set must not be called on this path"),
        env_get=lambda name: None,
        has_encrypted_rows=lambda: False,
        generate_key=Fernet.generate_key,
    )
    base.update(over)
    return _resolve_fernet(**base)


def test_resolution_step1_keyring_hit(monkeypatch):
    f = _resolve(keyring_get=lambda ns, item: _KEY_A.decode())
    assert f.decrypt(f.encrypt(b"x")) == b"x"


def test_resolution_step2_env_hit():
    f = _resolve(env_get=lambda name: _KEY_A.decode() if name == "AHF_MASTER_KEY" else None)
    assert f.decrypt(Fernet(_KEY_A).encrypt(b"x")) == b"x"  # same key resolved from env


def test_resolution_env_malformed_is_loud():
    with pytest.raises(CryptoMasterKeyError, match="AHF_MASTER_KEY"):
        _resolve(env_get=lambda name: "not-a-valid-fernet-key" if name == "AHF_MASTER_KEY" else None)


def test_resolution_step3_first_run_with_keyring_provisions():
    stored = {}
    f = _resolve(
        keyring_get=lambda ns, item: None,
        keyring_set=lambda ns, item, val: stored.update({(ns, item): val}),
        has_encrypted_rows=lambda: False,  # genuine first run
    )
    assert stored, "first run must persist the generated key to the keyring"
    (ns, item), val = next(iter(stored.items()))
    assert item == "master_key"
    assert f.decrypt(f.encrypt(b"x")) == b"x"  # the provisioned key works


def test_resolution_step3_headless_first_run_hard_fails_with_key():
    # No keyring backend (lookup raises) + no env + no rows -> must NOT mint a silent
    # ephemeral key; instead raise loud with the generated value to pin into env.
    def _no_backend(ns, item):
        raise RuntimeError("No recommended backend was available")

    with pytest.raises(CryptoMasterKeyError, match="AHF_MASTER_KEY"):
        _resolve(keyring_get=_no_backend, has_encrypted_rows=lambda: False)


def test_resolution_step4_lost_key_never_regenerates():
    # Encrypted rows EXIST but no key resolves -> terminal loud error, NEVER generate
    # (a fresh key would orphan the data forever).
    def _generate_must_not_run():
        pytest.fail("must not generate a key when encrypted rows already exist")

    with pytest.raises(CryptoMasterKeyError, match="unrecoverable"):
        _resolve(has_encrypted_rows=lambda: True, generate_key=_generate_must_not_run)


def test_resolution_probe_error_fails_closed():
    # If the has_encrypted_rows probe errors, assume rows exist (do NOT generate).
    def _generate_must_not_run():
        pytest.fail("probe error must be treated as rows-exist (fail closed), not first-run")

    def _boom():
        raise RuntimeError("db down")

    with pytest.raises(CryptoMasterKeyError):
        _resolve(has_encrypted_rows=_boom, generate_key=_generate_must_not_run)


def test_resolution_is_cached():
    calls = {"n": 0}

    def _count(ns, item):
        calls["n"] += 1
        return _KEY_A.decode()

    _resolve(keyring_get=_count)
    _resolve(keyring_get=_count)  # second call should hit the module cache, not keyring
    assert calls["n"] == 1
