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

from cryptography.fernet import Fernet, InvalidToken
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
                    raise CryptoError(f"re-encrypt produced an untagged value for provider={row.provider!r}; " "cipher is disabled or misconfigured — refusing to report a false upgrade")
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


@dataclass(frozen=True)
class RotateResult:
    """Immutable summary of a master-key rotation run."""

    scanned: int
    rotated: int
    skipped_plaintext: int
    skipped_empty: int


def rotate_api_key_master(
    db: Session,
    *,
    old_fernet: Fernet,
    new_fernet: Fernet,
    commit: bool = True,
) -> RotateResult:
    """Re-encrypt every ``enc:v1:`` ``ApiKey.key_value`` row from ``old_fernet`` to ``new_fernet``.

    Rotation is a pure key swap: each already-encrypted row is decrypted with the OLD key and
    re-encrypted with the NEW key, keeping the ``enc:v1:`` tag (the Fernet algorithm is unchanged,
    so there is no ``enc:v2:`` format bump — issue #25, decision 2026-06-28).

    Parameters
    ----------
    db:
        A SQLAlchemy session. The caller owns its lifecycle.
    old_fernet:
        The CURRENT master key's Fernet — must decrypt the existing tokens. A wrong key trips a
        loud ``CryptoError`` and the whole rotation rolls back (never re-encrypts ciphertext it
        could not decrypt). Obtain it via ``crypto.resolve_master_fernet``.
    new_fernet:
        The rotation-target Fernet, built explicitly from the new key (``crypto.build_fernet``)
        so it is independent of the process Fernet cache that still holds the old key.
    commit:
        When ``True`` (default) a single ``db.commit()`` is issued at the end — all rows rotate or
        none do. When ``False`` the session is left dirty for the caller to inspect/rollback (the
        dry-run path).

    Returns
    -------
    RotateResult
        Exact counts for each bucket:
        - ``scanned``: total rows examined.
        - ``rotated``: ``enc:v1:`` rows re-encrypted from the old key to the new key.
        - ``skipped_plaintext``: untagged rows left untouched — rotation only re-keys already
          encrypted rows; plaintext migration is the SWEEP's job (``reencrypt_plaintext_api_keys``).
        - ``skipped_empty``: rows with an empty/falsy ``key_value`` (not touched).

        Invariant: ``scanned == rotated + skipped_plaintext + skipped_empty``.

    Raises
    ------
    CryptoError
        If ``new_fernet`` is identical to ``old_fernet`` (refused up front — a same-key rotation
        would re-key nothing while reporting success), OR if a tagged row fails to decrypt under
        ``old_fernet`` (wrong key / corrupt ciphertext) — the rotation is rolled back rather than
        leaving rows split across two keys.
    """
    # Refuse a no-op rotation: if the NEW key is identical to the OLD one, re-encrypting still
    # changes the stored bytes (Fernet is non-deterministic), so it LOOKS like a successful rotation
    # while the OLD key still decrypts every row. For a rotation triggered by a suspected key
    # compromise that is the worst outcome — the operator retires the "old" key believing the secrets
    # moved off it, when they did not. Detect identity via a public-API probe (no Fernet internals):
    # a token minted by NEW decrypts under OLD iff the two share key material.
    try:
        old_fernet.decrypt(new_fernet.encrypt(b""))
        keys_identical = True
    except InvalidToken:
        keys_identical = False
    if keys_identical:
        raise CryptoError("the new master key is identical to the current master key — rotation would re-key nothing while reporting success; supply a DIFFERENT AHF_MASTER_KEY_NEW.")

    scanned = 0
    rotated = 0
    skipped_plaintext = 0
    skipped_empty = 0

    try:
        for row in db.query(ApiKey).all():
            scanned += 1
            if not row.key_value:
                skipped_empty += 1
            elif not row.key_value.startswith(TAG):
                # Untagged plaintext is the SWEEP's domain — never silently encrypt it during a
                # rotation (that would conflate two distinct operations and surprise the operator).
                skipped_plaintext += 1
            else:
                token = row.key_value[len(TAG) :].encode()
                try:
                    plaintext = old_fernet.decrypt(token)
                except InvalidToken as exc:
                    # Loud + terminal: a row we cannot decrypt with the OLD key must abort the whole
                    # rotation (rolled back below), never be "re-encrypted" as raw ciphertext.
                    raise CryptoError(f"could not decrypt a stored API key during rotation for provider={row.provider!r}: " "old master key mismatch or corrupted ciphertext — refusing to rotate.") from exc
                row.key_value = TAG + new_fernet.encrypt(plaintext).decode()
                rotated += 1

        if commit:
            db.commit()
    except Exception:
        # All-or-nothing: any failure must not leave rows split across the old and new keys
        # (both tagged enc:v1:, indistinguishable). Roll the whole rotation back, then re-raise.
        db.rollback()
        raise

    return RotateResult(
        scanned=scanned,
        rotated=rotated,
        skipped_plaintext=skipped_plaintext,
        skipped_empty=skipped_empty,
    )
