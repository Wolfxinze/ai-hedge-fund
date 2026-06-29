"""Tests for the master-key rotation (PRD v4 §9.10 / issue #25, item 1b).

Rotation = decrypt every ``enc:v1:`` row under the OLD master key and re-encrypt it under a
NEW master key, in ONE all-or-nothing transaction. Decision (Woo, 2026-06-28): keep the
``enc:v1:`` tag (a pure key swap, not an algorithm/KDF change) and rely on transactional
atomicity instead of a distinguishing ``enc:v2:`` tag — a crash before commit rolls back, so
there is never a persisted mix of old-key and new-key rows. The new key is supplied out-of-band
via ``AHF_MASTER_KEY_NEW``.

Fully offline: in-memory SQLite + explicit old/new Fernets (no real keyring/env/network) for the
pure-function tests; the CLI tests provision keys via env with the OS keyring neutralised.

WHY the key tests matter (curated, not exhaustive):
- rotate test: proves a row encrypted under the OLD key is, after rotation, readable ONLY by the
  NEW key (and the OLD key can no longer decrypt it) — the rotation actually changed the key.
- skip test: rotation only re-keys already-encrypted (``enc:v1:``) rows; untagged plaintext is the
  SWEEP's domain (reencrypt_plaintext_api_keys) and must be left untouched, never silently encrypted.
- dry-run test: commit=False + rollback must leave the at-rest token byte-identical (preview only).
- wrong-old-key test: a wrong OLD key trips a loud CryptoError and rolls back — never leaves a row
  it could not decrypt, never returns ciphertext as plaintext.
- atomicity test: when a LATER row fails to decrypt, ``db.rollback()`` must revert rows already
  rotated earlier in the SAME pass. This pins the single-transaction decision: a per-row-commit
  implementation would leave the first row under the NEW key — this test would go RED.
- CLI guard tests: main() refuses (exit 2) when KEY_ENCRYPTION is off OR when AHF_MASTER_KEY_NEW is
  unset, so rotation can never silently no-op or run without an explicit new key.
- CLI behavior tests: with both keys present, main([]) commits the rotation (exit 0, row now under
  the new key) and prints repoint next-steps; --dry-run previews then rolls back (exit 0, row
  unchanged at rest); a malformed new key / unexpected error is caught loud (exit 1, typed error +
  "no rows committed" on stderr, nothing persisted) with --verbose adding a traceback.
- at-rest dry-run test: a --dry-run rotation leaves the row byte-identical AT REST as observed by a
  SECOND, committed-state-only session — the strongest no-persist statement for a security op.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backend.database.connection import Base
from app.backend.database.models import ApiKey
from app.backend.services.crypto import CryptoError, TAG, reset_crypto_cache


# ── three distinct keys: OLD (current master), NEW (rotation target), WRONG (mismatch) ──
_OLD_KEY = Fernet.generate_key()
_NEW_KEY = Fernet.generate_key()
_WRONG_KEY = Fernet.generate_key()


def _enc(key: bytes, plaintext: str) -> str:
    """Produce an ``enc:v1:`` at-rest token for ``plaintext`` under ``key``."""
    return TAG + Fernet(key).encrypt(plaintext.encode()).decode()


def _decrypts_to(key: bytes, stored: str) -> str:
    """Decrypt a stored ``enc:v1:`` token under ``key`` (raises InvalidToken on mismatch)."""
    return Fernet(key).decrypt(stored[len(TAG) :].encode()).decode()


# ── in-memory DB fixture (isolated per test) ─────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _raw(session, provider: str) -> str:
    """Fetch the raw stored key_value without any decryption."""
    return session.query(ApiKey).filter_by(provider=provider).first().key_value


def _insert_raw(session, provider: str, raw_value: str) -> None:
    """Directly insert a row bypassing the repository cipher (simulates an at-rest row)."""
    session.add(ApiKey(provider=provider, key_value=raw_value, is_active=True))
    session.commit()


# ── import the function under test (will fail until implementation exists) ──
from app.backend.services.key_migration import RotateResult, rotate_api_key_master


# ── PURE FUNCTION TESTS ──────────────────────────────────────────────────────
class TestRotateApiKeyMaster:
    def test_tagged_row_is_rotated_to_new_key_only(self, session):
        """A row encrypted under OLD is, after rotation, readable ONLY by NEW.

        Proves the rotation genuinely swapped the key: the stored bytes change, the NEW key
        round-trips back to the original secret, and the OLD key can no longer decrypt it.
        """
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")

        result = rotate_api_key_master(session, old_fernet=Fernet(_OLD_KEY), new_fernet=Fernet(_NEW_KEY))

        after = _raw(session, "OPENAI_API_KEY")
        assert after.startswith(TAG), "rotated row must still carry the enc:v1: tag"
        assert after != before, "stored token must change (re-encrypted under the new key)"
        assert _decrypts_to(_NEW_KEY, after) == "sk-secret", "new key must round-trip the secret"
        # Non-vacuity: the OLD key must NO LONGER decrypt the rotated token.
        with pytest.raises(InvalidToken):
            _decrypts_to(_OLD_KEY, after)
        assert result.rotated == 1

    def test_plaintext_and_empty_rows_are_skipped_not_encrypted(self, session):
        """Rotation re-keys only enc:v1: rows; untagged plaintext + empty are skipped untouched.

        Untagged plaintext is the SWEEP's domain — rotation must never silently encrypt it
        (that would conflate two operations and surprise an operator who only meant to rotate).
        """
        _insert_raw(session, "PLAIN_KEY", "sk-plain")  # untagged plaintext
        _insert_raw(session, "EMPTY_KEY", "")  # empty
        _insert_raw(session, "ENC_KEY", _enc(_OLD_KEY, "sk-enc"))  # tagged

        result = rotate_api_key_master(session, old_fernet=Fernet(_OLD_KEY), new_fernet=Fernet(_NEW_KEY))

        assert _raw(session, "PLAIN_KEY") == "sk-plain", "plaintext must NOT be encrypted by rotation"
        assert _raw(session, "EMPTY_KEY") == "", "empty value must be left untouched"
        assert _decrypts_to(_NEW_KEY, _raw(session, "ENC_KEY")) == "sk-enc"
        assert result.scanned == 3
        assert result.rotated == 1
        assert result.skipped_plaintext == 1
        assert result.skipped_empty == 1
        # Accounting identity.
        assert result.scanned == result.rotated + result.skipped_plaintext + result.skipped_empty

    def test_dry_run_commit_false_does_not_persist(self, session):
        """commit=False + rollback leaves the at-rest token byte-identical (preview only)."""
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")

        result = rotate_api_key_master(session, old_fernet=Fernet(_OLD_KEY), new_fernet=Fernet(_NEW_KEY), commit=False)
        session.rollback()

        assert _raw(session, "OPENAI_API_KEY") == before, "dry-run must not persist: old token must survive rollback"
        assert result.rotated == 1, "dry-run still reports what WOULD be rotated"

    def test_empty_table_returns_zero_counts(self, session):
        """No rows → all counts zero, no error."""
        result = rotate_api_key_master(session, old_fernet=Fernet(_OLD_KEY), new_fernet=Fernet(_NEW_KEY))
        assert result == RotateResult(scanned=0, rotated=0, skipped_plaintext=0, skipped_empty=0)

    def test_wrong_old_key_raises_loud_and_rolls_back(self, session):
        """A WRONG old key must raise CryptoError (never return ciphertext) and roll back.

        Without a loud failure, a mismatched key could corrupt every row by "re-encrypting"
        ciphertext-as-plaintext. The row must be byte-identical after the failed rotation.
        """
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")

        with pytest.raises(CryptoError):
            rotate_api_key_master(session, old_fernet=Fernet(_WRONG_KEY), new_fernet=Fernet(_NEW_KEY))

        assert _raw(session, "OPENAI_API_KEY") == before, "a wrong-key rotation must leave the row untouched"
        # And the real OLD key still decrypts it — the row was never damaged.
        assert _decrypts_to(_OLD_KEY, _raw(session, "OPENAI_API_KEY")) == "sk-secret"

    def test_rollback_reverts_earlier_rotation_when_a_later_row_fails(self, session):
        """ATOMICITY (the single-transaction decision): when a LATER row fails to decrypt,
        ``db.rollback()`` must revert rows already rotated earlier in the SAME pass.

        This is the load-bearing mutation-proof. The first row is rotated (mutated in-memory)
        before the second, corrupt row trips the loud CryptoError. With the single end-of-pass
        commit, the rollback reverts the first row back to its OLD-key token. A per-row-commit
        implementation would leave the first row under the NEW key — this assertion would go RED.
        Relies on insertion order: SQLite scans by rowid, so the good row (inserted first) is
        rotated before the corrupt row (inserted second) raises.
        """
        good_before = _enc(_OLD_KEY, "sk-good")
        _insert_raw(session, "GOOD_KEY", good_before)  # rotated first (in-memory)
        _insert_raw(session, "CORRUPT_KEY", TAG + "not-a-valid-fernet-token")  # trips decrypt second

        with pytest.raises(CryptoError):
            rotate_api_key_master(session, old_fernet=Fernet(_OLD_KEY), new_fernet=Fernet(_NEW_KEY))

        # The earlier rotation was rolled back: the good row is byte-identical to its OLD token,
        # NOT a NEW-key token. (Byte-identity is the cleanest proof the mutation was reverted.)
        assert _raw(session, "GOOD_KEY") == good_before, "earlier rotation must be rolled back, not left half-applied"
        assert _decrypts_to(_OLD_KEY, _raw(session, "GOOD_KEY")) == "sk-good", "good row must still decrypt under the OLD key"


# ── CLI GUARD TESTS ───────────────────────────────────────────────────────────
class TestRotateCliGuard:
    def test_main_returns_2_when_encryption_disabled(self, monkeypatch, capsys):
        """main() exits 2 (refuse) when KEY_ENCRYPTION is off — never a silent no-op."""
        monkeypatch.setenv("KEY_ENCRYPTION", "off")
        reset_crypto_cache()

        from app.backend.scripts.rotate_master_key import main

        exit_code = main([])

        assert exit_code == 2
        captured = capsys.readouterr()
        assert "KEY_ENCRYPTION" in captured.err
        assert captured.out == ""

    def test_main_returns_2_when_new_key_missing(self, monkeypatch, capsys):
        """main() exits 2 (refuse) when KEY_ENCRYPTION is on but AHF_MASTER_KEY_NEW is unset.

        The new key is a required, explicit input — rotation must never invent one or run
        without it (that would re-encrypt rows to an unknown key the operator can't restore).
        """
        monkeypatch.setenv("KEY_ENCRYPTION", "on")
        monkeypatch.delenv("AHF_MASTER_KEY_NEW", raising=False)
        reset_crypto_cache()

        from app.backend.scripts.rotate_master_key import main

        exit_code = main([])

        assert exit_code == 2
        captured = capsys.readouterr()
        assert "AHF_MASTER_KEY_NEW" in captured.err
        assert captured.out == ""


# ── CLI BEHAVIOR TESTS (full main() flow) ────────────────────────────────────
@pytest.fixture
def rot_on(monkeypatch):
    """Enable KEY_ENCRYPTION, provision the OLD master via AHF_MASTER_KEY and the NEW master via
    AHF_MASTER_KEY_NEW (both env, no OS keyring), and neutralise the host keyring.

    HERMETIC against a provisioned OS keyring (#62): resolve_master_fernet() resolves the keyring
    FIRST and only falls through to AHF_MASTER_KEY if it returns None. On a dev machine holding a
    ``master_key`` keyring entry the OLD cipher would resolve THAT key, not ``_OLD_KEY`` — the
    seeded rows would fail to decrypt (spurious CryptoError). Stubbing ``keyring.get_password``
    forces the deterministic env path regardless of host keyring state.
    """
    monkeypatch.setenv("KEY_ENCRYPTION", "on")
    monkeypatch.setenv("AHF_MASTER_KEY", _OLD_KEY.decode())
    monkeypatch.setenv("AHF_MASTER_KEY_NEW", _NEW_KEY.decode())
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None)
    reset_crypto_cache()
    yield
    reset_crypto_cache()


def _wire_session_local(monkeypatch, session):
    """Point the script's SessionLocal at a callable returning the test session."""
    import app.backend.scripts.rotate_master_key as cli

    monkeypatch.setattr(cli, "SessionLocal", lambda: session)
    return cli


class TestRotateCliBehavior:
    def test_main_happy_path_rotates_and_exits_0(self, monkeypatch, capsys, session, rot_on):
        """main([]) with both keys present rotates the row to the NEW key and exits 0.

        After the run the at-rest token decrypts under the NEW key only, and the CLI prints
        repoint next-steps so the operator knows to make the new key the resolved master.
        """
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        cli = _wire_session_local(monkeypatch, session)

        exit_code = cli.main([])

        assert exit_code == 0
        stored = _raw(session, "OPENAI_API_KEY")
        assert _decrypts_to(_NEW_KEY, stored) == "sk-secret", "row must be rotated to the new key"
        with pytest.raises(InvalidToken):
            _decrypts_to(_OLD_KEY, stored)
        out = capsys.readouterr().out
        assert "rotated=1" in out
        assert "[dry-run]" not in out
        # Operator next-steps: how to make the new key the resolved master.
        assert "repoint" in out.lower()
        assert "AHF_MASTER_KEY_NEW" in out

    def test_main_dry_run_previews_then_rolls_back_and_exits_0(self, monkeypatch, capsys, session, rot_on):
        """main(['--dry-run']) previews counts, rolls back, exits 0, leaves the token unchanged."""
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")
        cli = _wire_session_local(monkeypatch, session)

        exit_code = cli.main(["--dry-run"])

        assert exit_code == 0
        assert _raw(session, "OPENAI_API_KEY") == before, "dry-run must not persist the rotation"
        out = capsys.readouterr().out
        assert out.startswith("[dry-run]")
        assert "rotated=1" in out

    def test_main_dry_run_leaves_token_unchanged_at_rest_observed_by_a_second_session(self, monkeypatch, capsys, rot_on):
        """A --dry-run rotation must leave the token byte-identical AT REST, observed from a SECOND,
        independent session that sees only COMMITTED data (the strongest no-persist statement).

        WHY a second session: the test session's own ``db.close()`` implicitly rolls back, which can
        mask the CLI's explicit dry-run ``db.rollback()``. Sharing one connection (StaticPool) and
        re-querying via a separate session asserts against committed-vs-rolled-back bytes.
        """
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        before = _enc(_OLD_KEY, "sk-secret")
        seed = Session()
        seed.add(ApiKey(provider="OPENAI_API_KEY", key_value=before, is_active=True))
        seed.commit()
        seed.close()

        import app.backend.scripts.rotate_master_key as cli

        monkeypatch.setattr(cli, "SessionLocal", Session)

        exit_code = cli.main(["--dry-run"])
        assert exit_code == 0

        observer = Session()
        try:
            at_rest = observer.query(ApiKey).filter_by(provider="OPENAI_API_KEY").first().key_value
        finally:
            observer.close()
        assert at_rest == before, "dry-run must leave the token unchanged at rest (nothing committed)"

    def test_main_malformed_new_key_exits_1_commits_nothing(self, monkeypatch, capsys, session, rot_on):
        """A malformed AHF_MASTER_KEY_NEW surfaces a loud CryptoMasterKeyError: exit 1, nothing committed."""
        monkeypatch.setenv("AHF_MASTER_KEY_NEW", "not-a-fernet-key")
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")
        cli = _wire_session_local(monkeypatch, session)

        exit_code = cli.main([])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "CryptoMasterKeyError" in captured.err
        assert "no rows committed" in captured.err
        assert captured.out == ""
        assert _raw(session, "OPENAI_API_KEY") == before, "a malformed new key must commit nothing"

    def test_main_unexpected_error_exits_1_to_stderr_commits_nothing(self, monkeypatch, capsys, session, rot_on):
        """An unexpected error inside rotation is caught: exit 1, typed error + 'no rows committed'."""
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        before = _raw(session, "OPENAI_API_KEY")
        cli = _wire_session_local(monkeypatch, session)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom in rotation")

        monkeypatch.setattr(cli, "rotate_api_key_master", _boom)

        exit_code = cli.main([])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "RuntimeError" in captured.err
        assert "boom in rotation" in captured.err
        assert "no rows committed" in captured.err
        assert captured.out == ""
        assert _raw(session, "OPENAI_API_KEY") == before, "an unexpected error must commit nothing"

    def test_main_verbose_flag_prints_traceback_on_error(self, monkeypatch, capsys, session, rot_on):
        """--verbose adds a traceback to the typed error on the exit-1 path."""
        _insert_raw(session, "OPENAI_API_KEY", _enc(_OLD_KEY, "sk-secret"))
        cli = _wire_session_local(monkeypatch, session)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom in rotation")

        monkeypatch.setattr(cli, "rotate_api_key_master", _boom)

        exit_code = cli.main(["--verbose"])

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "RuntimeError" in err
        assert "Traceback (most recent call last)" in err
        assert "_boom" in err
