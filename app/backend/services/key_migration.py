"""Bulk re-encrypt sweep for API keys at rest (PRD v4 §9.10 / issue #25).

Provides a pure, injectable function that upgrades plaintext ``ApiKey.key_value`` rows
to ciphertext under the current master key.  Meant to be called by the CLI after a
deployment switches ``KEY_ENCRYPTION`` from OFF to ON.

Design choices
--------------
* **Skip-by-TAG, not encrypt-and-compare**: Fernet is non-deterministic — encrypting the
  same plaintext always produces a different token.  Re-encrypting an already-tagged row
  would silently rotate its token and invalidate any callers holding the old ciphertext.
  We skip by checking ``row.key_value.startswith(TAG)`` instead.
* **Single transaction, rollback-on-any-error**: one ``db.commit()`` at the end (when
  ``commit=True``).  A partial commit mid-sweep is worse than a failed sweep, so any error
  rolls the whole sweep back and re-raises — the session is never left partially mutated.
* **Flag-check belongs to the CLI; this function fails loud on misuse**: ``cipher.encrypt()``
  is flag-gated (a disabled cipher returns its input unchanged).  The CLI
  (``reencrypt_api_keys.py``) guards with ``is_encryption_enabled()`` *before* calling, so
  the guard is visible and testable.  As defense-in-depth, this function also asserts every
  upgrade actually produced a tagged value and raises ``CryptoError`` otherwise — so a
  disabled/misconfigured cipher can never yield a FALSE ``upgraded`` count (which would give
  an operator false confidence that secrets are encrypted at rest when they are not).  Tests
  inject an explicitly enabled cipher to exercise the happy path.
* **Scans ALL rows** (active AND inactive): every at-rest secret deserves encryption
  regardless of whether the key is currently in use.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.backend.database.models import ApiKey
from app.backend.services.crypto import TAG, CryptoError, KeyCipher


@dataclass(frozen=True)
class ReencryptResult:
    """Immutable summary of a re-encrypt sweep run."""

    scanned: int
    upgraded: int
    skipped_encrypted: int
    skipped_empty: int


def reencrypt_plaintext_api_keys(
    db: Session,
    cipher: KeyCipher,
    *,
    commit: bool = True,
) -> ReencryptResult:
    """Re-encrypt every plaintext ``ApiKey.key_value`` row under ``cipher``.

    Parameters
    ----------
    db:
        A SQLAlchemy session.  The caller owns its lifecycle.
    cipher:
        A ``KeyCipher`` that must have ``enabled=True``.  A disabled/misconfigured cipher
        is not pre-checked here, but any upgrade it fails to encrypt is caught by the
        post-condition guard and raises ``CryptoError`` — never a silent no-op.
    commit:
        When ``True`` (default) a single ``db.commit()`` is issued at the end.
        When ``False`` the session is left dirty so the caller can inspect (or rollback)
        without side effects — this is the dry-run path.

    Returns
    -------
    ReencryptResult
        Exact counts for each bucket:
        - ``scanned``: total rows examined.
        - ``upgraded``: rows whose ``key_value`` was plaintext and is now ciphertext.
        - ``skipped_encrypted``: rows already carrying the ``enc:v1:`` tag (not touched).
        - ``skipped_empty``: rows with an empty/falsy ``key_value`` (not touched).

        Invariant: ``scanned == upgraded + skipped_encrypted + skipped_empty``, and every
        one of ``upgraded`` rows is now genuinely tag-prefixed ciphertext.

    Raises
    ------
    CryptoError
        If an upgrade produced an untagged value (a disabled/misconfigured cipher) — the
        sweep is rolled back rather than reporting a false ``upgraded`` count.
    """
    scanned = 0
    upgraded = 0
    skipped_encrypted = 0
    skipped_empty = 0

    try:
        for row in db.query(ApiKey).all():
            scanned += 1
            if not row.key_value:
                skipped_empty += 1
            elif row.key_value.startswith(TAG):
                # Already encrypted: leave byte-identical — skip-by-TAG prevents Fernet churn.
                skipped_encrypted += 1
            else:
                encrypted = cipher.encrypt(row.key_value)
                # Post-condition: a genuine upgrade MUST yield a tagged value. A disabled or
                # misconfigured cipher returns the plaintext unchanged — fail loud rather than
                # increment a phantom ``upgraded`` count, so the count is load-bearing.
                if not encrypted.startswith(TAG):
                    raise CryptoError(
                        f"re-encrypt produced an untagged value for provider={row.provider!r}; "
                        "cipher is disabled or misconfigured — refusing to report a false upgrade"
                    )
                row.key_value = encrypted
                upgraded += 1

        if commit:
            db.commit()
    except Exception:
        # Any failure (untagged post-condition OR a commit error) must not leave the session
        # partially mutated — roll back the whole sweep, then re-raise loudly.
        db.rollback()
        raise

    return ReencryptResult(
        scanned=scanned,
        upgraded=upgraded,
        skipped_encrypted=skipped_encrypted,
        skipped_empty=skipped_empty,
    )
