"""Tests for the idempotent re-encrypt sweep (PRD v4 §9.10 / issue #25).

Fully offline: in-memory SQLite + an injected KeyCipher (no real keyring/env/network).
Follows the pattern of test_crypto_wiring.py.

WHY the key tests matter (curated, not exhaustive):
- upgrade test: proves plaintext rows are reachable at-rest (after sweep, stored bytes
  change and round-trip back to the original secret).
- no-churn test: Fernet is non-deterministic, so re-encrypting an already-encrypted row
  would silently rotate its token and invalidate cached/compared values. Skip-by-TAG
  prevents this: the stored bytes are byte-identical after a second pass.
- dry-run test: commit=False + rollback must leave the DB untouched so operators can
  preview without side effects.
- idempotent test: second sweep must report upgraded=0 and leave all rows unchanged.
- mixed batch test: exact per-bucket counts prove the branching logic is correct.
- fail-loud test: a disabled/misconfigured cipher returns plaintext unchanged; without the
  post-condition guard ``upgraded`` would be incremented while the row stays plaintext at
  rest (false operator confidence). The guard raises CryptoError and the sweep rolls back.
- rollback test: when a LATER row trips the post-condition, ``db.rollback()`` must revert
  rows already upgraded earlier in the SAME sweep — the "never a partial mutation" guarantee.
- CLI guard test: main() must refuse (exit 2, stderr message) when KEY_ENCRYPTION is off
  so the sweep can never silently no-op on a plaintext-mode deployment.
- CLI behavior tests: with encryption on, main([]) commits the upgrade (exit 0, row tagged
  at rest); --dry-run previews then rolls back (exit 0, row stays plaintext); an unexpected
  sweep error is caught loud (exit 1, typed error + "no rows committed" on stderr, nothing
  persisted) with --verbose adding a traceback.
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backend.database.connection import Base
from app.backend.database.models import ApiKey
from app.backend.services.crypto import CryptoError, KeyCipher, TAG, reset_crypto_cache


# ── shared test cipher factory (same pattern as test_crypto_wiring.py) ──────
_KEY = Fernet.generate_key()


def _on(key: bytes = _KEY) -> KeyCipher:
    return KeyCipher(enabled=True, fernet_provider=lambda: Fernet(key))


class _PoisonCipher:
    """Enabled cipher that tags real plaintext but returns ONE 'poison' value untagged.

    Encrypting the poison plaintext returns it unchanged (untagged), which trips the
    function's post-condition guard AFTER an earlier row has already been mutated in the
    session — exactly the mid-sweep failure that ``db.rollback()`` exists to undo. Duck-typed
    (only ``.encrypt`` is used by the function under test), matching the ``_FakeDB`` pattern.
    """

    def __init__(self, real: KeyCipher, poison: str) -> None:
        self._real = real
        self._poison = poison

    def encrypt(self, raw: str) -> str:
        if raw == self._poison:
            return raw  # untagged → post-condition CryptoError
        return self._real.encrypt(raw)


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
    """Directly insert a row bypassing the repository cipher (simulates a pre-encryption row)."""
    session.add(ApiKey(provider=provider, key_value=raw_value, is_active=True))
    session.commit()


# ── import the function under test (will fail until implementation exists) ──
from app.backend.services.key_migration import ReencryptResult, reencrypt_plaintext_api_keys


# ── PURE FUNCTION TESTS ──────────────────────────────────────────────────────


class TestReencryptPlaintextApiKeys:
    def test_plaintext_row_is_upgraded(self, session):
        """A plaintext row is re-encrypted: stored value changes and round-trips back."""
        _insert_raw(session, "OPENAI_API_KEY", "sk-secret")
        cipher = _on()

        result = reencrypt_plaintext_api_keys(session, cipher)

        stored = _raw(session, "OPENAI_API_KEY")
        # Stored value now carries the encryption tag — was plaintext
        assert stored.startswith(TAG), "upgraded row must carry the enc:v1: tag"
        # Non-vacuity: the stored bytes actually changed
        assert stored != "sk-secret", "stored value must differ from the original plaintext"
        # Round-trip: decrypt back to the original secret
        assert cipher.decrypt(stored) == "sk-secret"
        assert result.upgraded == 1

    def test_already_encrypted_row_is_untouched_byte_identical(self, session):
        """An already-encrypted row is left byte-identical — no churn.

        Fernet is non-deterministic: re-encrypting the same plaintext always produces a
        different token. Skip-by-TAG prevents churn so callers (caches, comparisons) that
        hold on to a specific Fernet token are not silently invalidated.
        """
        cipher = _on()
        # Encrypt once and store the resulting token directly
        pre_encrypted = cipher.encrypt("sk-secret")
        _insert_raw(session, "OPENAI_API_KEY", pre_encrypted)
        captured_token = _raw(session, "OPENAI_API_KEY")
        assert captured_token.startswith(TAG)

        result = reencrypt_plaintext_api_keys(session, cipher)

        after_token = _raw(session, "OPENAI_API_KEY")
        # Byte-identical: proves skip-by-TAG, not just "still encrypted"
        assert after_token == captured_token, "already-encrypted row must be byte-identical after sweep (no churn)"
        assert result.skipped_encrypted == 1
        assert result.upgraded == 0

    def test_counts_are_exact_and_sum_to_scanned(self, session):
        """scanned == upgraded + skipped_encrypted + skipped_empty (accounting identity)."""
        cipher = _on()
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        pre_enc = cipher.encrypt("sk-enc")
        _insert_raw(session, "ANTHROPIC_API_KEY", pre_enc)

        result = reencrypt_plaintext_api_keys(session, cipher)

        assert result.scanned == 2
        assert result.upgraded == 1
        assert result.skipped_encrypted == 1
        assert result.skipped_empty == 0
        assert result.scanned == result.upgraded + result.skipped_encrypted + result.skipped_empty

    def test_idempotent_second_sweep_upgrades_zero(self, session):
        """Running the sweep a second time upgrades nothing — all rows already tagged."""
        cipher = _on()
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")

        first = reencrypt_plaintext_api_keys(session, cipher)
        assert first.upgraded == 1

        second = reencrypt_plaintext_api_keys(session, cipher)
        assert second.upgraded == 0
        assert second.skipped_encrypted == 1

    def test_mixed_batch_correct_per_bucket_counts(self, session):
        """Mixed batch (2 plaintext + 2 encrypted) yields correct per-bucket counts."""
        cipher = _on()
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain-1")
        _insert_raw(session, "GROQ_API_KEY", "sk-plain-2")
        _insert_raw(session, "ANTHROPIC_API_KEY", cipher.encrypt("sk-enc-1"))
        _insert_raw(session, "XAI_API_KEY", cipher.encrypt("sk-enc-2"))

        result = reencrypt_plaintext_api_keys(session, cipher)

        assert result.scanned == 4
        assert result.upgraded == 2
        assert result.skipped_encrypted == 2
        assert result.skipped_empty == 0

    def test_dry_run_commit_false_does_not_persist(self, session):
        """commit=False + rollback leaves the original plaintext unchanged.

        This is the dry-run contract: preview without side effects.
        """
        cipher = _on()
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")

        result = reencrypt_plaintext_api_keys(session, cipher, commit=False)
        session.rollback()

        # After rollback the row must still be plaintext
        stored = _raw(session, "OPENAI_API_KEY")
        assert stored == "sk-plain", "dry-run must not persist: plaintext must survive rollback"
        # The function still reported what WOULD happen
        assert result.upgraded == 1

    def test_empty_table_returns_zero_counts(self, session):
        """No rows → all counts zero, no error."""
        result = reencrypt_plaintext_api_keys(session, _on())

        assert result == ReencryptResult(scanned=0, upgraded=0, skipped_encrypted=0, skipped_empty=0)

    def test_inactive_rows_are_also_upgraded(self, session):
        """Inactive (is_active=False) rows must be upgraded — every at-rest secret counts."""
        cipher = _on()
        session.add(ApiKey(provider="OLD_KEY", key_value="sk-inactive", is_active=False))
        session.commit()

        result = reencrypt_plaintext_api_keys(session, cipher)

        stored = _raw(session, "OLD_KEY")
        assert stored.startswith(TAG), "inactive rows must be upgraded too"
        assert result.upgraded == 1

    def test_disabled_cipher_fails_loud_no_phantom_upgrade(self, session):
        """A disabled/misconfigured cipher must FAIL LOUD, never report a phantom upgrade.

        ``KeyCipher.encrypt`` returns the input UNCHANGED when ``enabled=False``, so without
        the post-condition guard the row would stay plaintext at rest while ``upgraded`` was
        still incremented — giving the operator false confidence. The guard catches the
        untagged result, raises CryptoError, and the sweep rolls back so the row is never
        half-mutated.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        disabled = KeyCipher(enabled=False, fernet_provider=lambda: pytest.fail("disabled cipher must not resolve a key"))

        with pytest.raises(CryptoError):
            reencrypt_plaintext_api_keys(session, disabled)

        # Rolled back by the sweep: the row is still the original plaintext, not silently damaged.
        assert _raw(session, "OPENAI_API_KEY") == "sk-plain"

    def test_rollback_reverts_earlier_upgrades_when_a_later_row_fails(self, session):
        """The "never a partial mutation" guarantee: when a LATER row trips the post-condition,
        ``db.rollback()`` must revert rows already upgraded earlier in the SAME sweep.

        This pins ``db.rollback()`` as load-bearing. The fail-loud test above raises BEFORE any
        ``row.key_value =`` assignment, so it never exercises the rollback's real job (undoing an
        earlier in-memory mutation). Here the first row IS mutated (tagged) before the second row
        trips the guard; without the rollback the first row would stay tagged in the session's
        identity map though nothing was committed — a partial mutation. Relies on insertion order:
        SQLite scans rows by rowid, so the good row (inserted first) is upgraded before the poison
        row (inserted second) raises.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-good")  # processed first → gets mutated
        _insert_raw(session, "ANTHROPIC_API_KEY", "sk-poison")  # processed second → trips the guard
        cipher = _PoisonCipher(_on(), poison="sk-poison")

        with pytest.raises(CryptoError):
            reencrypt_plaintext_api_keys(session, cipher)

        # Rollback reverted the earlier in-memory upgrade: BOTH rows are still original plaintext.
        assert _raw(session, "OPENAI_API_KEY") == "sk-good", "earlier upgrade must be rolled back, not left half-applied in the session"
        assert _raw(session, "ANTHROPIC_API_KEY") == "sk-poison"


# ── EMPTY key_value BRANCH ───────────────────────────────────────────────────
class TestEmptyKeyValueBranch:
    def test_empty_string_key_value_counted_as_skipped_empty(self, session):
        """Empty key_value is counted as skipped_empty (no encryption attempted).

        Note on schema: ApiKey.key_value is Column(Text, nullable=False) — SQLAlchemy
        enforces non-null at the Python ORM layer, but does NOT enforce non-empty string.
        An empty string ("") passes ORM validation. Driven via a REAL in-memory SQLite
        insert (mirroring the other function tests) so the row goes through the same
        ORM/query path production uses — not a hand-rolled mock.
        """
        _insert_raw(session, "EMPTY_KEY", "")
        cipher = _on()

        result = reencrypt_plaintext_api_keys(session, cipher)

        assert result.scanned == 1
        assert result.skipped_empty == 1
        assert result.upgraded == 0
        # Not mutated: the stored value is still the empty string after the sweep.
        assert _raw(session, "EMPTY_KEY") == ""


# ── CLI GUARD TESTS ───────────────────────────────────────────────────────────
class TestCliGuard:
    def test_main_returns_2_when_encryption_disabled(self, monkeypatch, capsys):
        """main() must exit 2 and write to stderr when KEY_ENCRYPTION is not set.

        WHY: a disabled-encryption deployment should never silently no-op the sweep
        (which would give false confidence). Exit 2 with an actionable message forces
        the operator to explicitly enable encryption before running.
        """
        monkeypatch.delenv("KEY_ENCRYPTION", raising=False)
        reset_crypto_cache()

        from app.backend.scripts.reencrypt_api_keys import main

        exit_code = main([])

        assert exit_code == 2
        captured = capsys.readouterr()
        # Error goes to stderr, not stdout
        assert "KEY_ENCRYPTION" in captured.err
        assert captured.out == ""

    def test_main_returns_2_when_encryption_explicitly_off(self, monkeypatch, capsys):
        """main() exits 2 even when KEY_ENCRYPTION is explicitly set to 'off'."""
        monkeypatch.setenv("KEY_ENCRYPTION", "off")
        reset_crypto_cache()

        from app.backend.scripts.reencrypt_api_keys import main

        exit_code = main([])

        assert exit_code == 2
        captured = capsys.readouterr()
        assert "KEY_ENCRYPTION" in captured.err


# ── CLI BEHAVIOR TESTS (full main() flow) ────────────────────────────────────
@pytest.fixture
def enc_on(monkeypatch):
    """Enable KEY_ENCRYPTION and provision a master key via AHF_MASTER_KEY (env path,
    no OS keyring) so the REAL get_cipher() resolves a working Fernet. Clears the
    process-level Fernet cache before and after so tests don't leak a resolved key.
    """
    monkeypatch.setenv("KEY_ENCRYPTION", "on")
    monkeypatch.setenv("AHF_MASTER_KEY", _KEY.decode())
    reset_crypto_cache()
    yield
    reset_crypto_cache()


def _wire_session_local(monkeypatch, session):
    """Point the script's SessionLocal at a callable returning the test session.

    main() calls SessionLocal() once and db.close() in finally; closing an in-memory
    session is harmless and the assertions re-query through the same engine binding.
    """
    import app.backend.scripts.reencrypt_api_keys as cli

    monkeypatch.setattr(cli, "SessionLocal", lambda: session)
    return cli


class TestCliBehavior:
    def test_main_happy_path_commits_upgrade_and_exits_0(self, monkeypatch, capsys, session, enc_on):
        """main([]) with encryption on + a plaintext row commits the upgrade and exits 0.

        WHY: the default (commit) path must actually persist the re-encryption — after the
        run the at-rest value is tag-encrypted and round-trips back to the original secret.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        cli = _wire_session_local(monkeypatch, session)

        exit_code = cli.main([])

        assert exit_code == 0
        # Persisted: the row is now tag-encrypted at rest (commit happened).
        stored = _raw(session, "OPENAI_API_KEY")
        assert stored.startswith(TAG), "happy path must persist the tag-encrypted upgrade"
        assert _on().decrypt(stored) == "sk-plain"
        out = capsys.readouterr().out
        assert "upgraded=1" in out
        assert "[dry-run]" not in out

    def test_main_dry_run_previews_then_rolls_back_and_exits_0(self, monkeypatch, capsys, session, enc_on):
        """main(['--dry-run']) previews counts, rolls back, exits 0, leaves rows plaintext.

        WHY: operators must be able to preview the sweep with zero side effects; the row
        stays plaintext at rest even though the report says it WOULD be upgraded.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        cli = _wire_session_local(monkeypatch, session)

        exit_code = cli.main(["--dry-run"])

        assert exit_code == 0
        # Rolled back: the row is still the original plaintext at rest.
        assert _raw(session, "OPENAI_API_KEY") == "sk-plain", "dry-run must not persist"
        out = capsys.readouterr().out
        assert out.startswith("[dry-run]")
        assert "upgraded=1" in out

    def test_main_unexpected_error_exits_1_to_stderr_commits_nothing(self, monkeypatch, capsys, session, enc_on):
        """An unexpected error inside the sweep is caught: exit 1, stderr, nothing committed.

        WHY: a runtime failure must surface loudly (typed error on stderr + a visible
        "no rows committed" line so the rollback is explicit) and never silently exit 0 or
        leave a partially-applied mutation.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        cli = _wire_session_local(monkeypatch, session)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom in sweep")

        monkeypatch.setattr(cli, "reencrypt_plaintext_api_keys", _boom)

        exit_code = cli.main([])

        assert exit_code == 1
        captured = capsys.readouterr()
        # Typed error name + message on stderr (the ergonomics upgrade), not bare stdout.
        assert "RuntimeError" in captured.err
        assert "boom in sweep" in captured.err
        # The rollback is made visible so the operator knows nothing was persisted.
        assert "no rows committed" in captured.err
        assert captured.out == ""
        # Nothing committed: the row is still the original plaintext at rest.
        assert _raw(session, "OPENAI_API_KEY") == "sk-plain"

    def test_main_verbose_flag_prints_traceback_on_error(self, monkeypatch, capsys, session, enc_on):
        """--verbose adds a traceback to the typed error on the exit-1 path.

        WHY: operators debugging a failed sweep need the stack, but the default output stays
        terse (a one-line typed error); --verbose is the opt-in for the full traceback.
        """
        _insert_raw(session, "OPENAI_API_KEY", "sk-plain")
        cli = _wire_session_local(monkeypatch, session)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom in sweep")

        monkeypatch.setattr(cli, "reencrypt_plaintext_api_keys", _boom)

        exit_code = cli.main(["--verbose"])

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "RuntimeError" in err
        # A traceback names the function frame where the error was raised.
        assert "Traceback (most recent call last)" in err
        assert "_boom" in err
