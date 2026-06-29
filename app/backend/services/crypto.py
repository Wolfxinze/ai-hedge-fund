"""API-key encryption at rest (PRD v4 §9.10, Phase 1b, X5).

``KEY_ENCRYPTION`` defaults OFF (legacy plaintext, backward-compatible). When ON,
stored ``ApiKey.key_value`` values are Fernet-encrypted with a tagged format so the
column stays ``Text`` (no migration) and on/off can toggle without breaking rows:

  * encrypt is FLAG-gated: enabled -> ``enc:v1:<fernet token>``; off -> raw unchanged.
  * decrypt is TAG-gated: a value with the ``enc:v1:`` prefix is Fernet-decrypted
    (loud on a wrong/missing key — never returns ciphertext); anything else is
    returned verbatim as legacy plaintext. Decrypt ignores the flag deliberately, so
    toggling encryption OFF still reads existing ciphertext (as long as the master
    key resolves) and turning it ON still reads pre-existing plaintext rows.

Master key (one global Fernet key, ``master_key``) resolves OS keyring ->
``AHF_MASTER_KEY`` env -> first-run provisioning -> a loud, actionable failure. Boot
never eagerly resolves the key, so a misconfig fails the first key-using request
loudly rather than hanging startup (mirrors the scheduler's resilient startup).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

TAG = "enc:v1:"
_KEYRING_ITEM = "master_key"
_DEFAULT_NAMESPACE = "ai-hedge-fund"
_ENABLED_TRUTHY = {"on", "true", "1", "yes"}

# One resolved Fernet for the process (one master key). reset_crypto_cache() clears it.
_FERNET_CACHE: Fernet | None = None


class CryptoError(Exception):
    """Decrypt failure / corrupt or wrong-key ciphertext. Never carries key material."""


class CryptoMasterKeyError(CryptoError):
    """No usable master key (missing/malformed). Message names the exact remedy."""


def is_encryption_enabled() -> bool:
    """Read ``KEY_ENCRYPTION`` live (default off). Gates the ENCRYPT path only."""
    return os.environ.get("KEY_ENCRYPTION", "off").strip().lower() in _ENABLED_TRUTHY


def keyring_namespace() -> str:
    return os.environ.get("KEYRING_NAMESPACE", _DEFAULT_NAMESPACE).strip() or _DEFAULT_NAMESPACE


def reset_crypto_cache() -> None:
    """Clear the module-level Fernet cache (test hook; also after a key change)."""
    global _FERNET_CACHE
    _FERNET_CACHE = None


# ── master-key resolution ────────────────────────────────────────────────────
def _keyring_lookup(keyring_get: Callable[[str, str], str | None]) -> tuple[str | None, bool]:
    """Return (master_key_or_None, backend_available). A missing OS keyring backend
    (NoKeyringError) is reported as backend_available=False so provisioning never
    stores a key into a backend that does not exist (it would be silently lost)."""
    try:
        return keyring_get(keyring_namespace(), _KEYRING_ITEM), True
    except Exception as exc:  # keyring.errors.NoKeyringError (and any backend error) -> fall through
        # INFO (not debug) + the exception class so a reachable-but-locked keyring is
        # distinguishable from a genuinely absent backend (fires once per process — cached).
        logger.info("keyring lookup unavailable (%s: %s); falling back to AHF_MASTER_KEY / provisioning", type(exc).__name__, exc)
        return None, False


def _build_fernet(key: str, *, source: str) -> Fernet:
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CryptoMasterKeyError(f"{source} is set but malformed; expected a Fernet.generate_key() value (44-char urlsafe base64).") from exc


def build_fernet(key: str, *, source: str) -> Fernet:
    """Public seam: build a Fernet from an EXPLICIT key string, with the same loud
    malformed-key error as master-key resolution. Used by key rotation to construct the
    NEW-key cipher (e.g. from ``AHF_MASTER_KEY_NEW``). Deliberately does NOT touch the
    process Fernet cache — the new key must stay independent of the resolved old key."""
    return _build_fernet(key, source=source)


def _resolve_fernet(
    *,
    keyring_get: Callable[[str, str], str | None] = None,
    keyring_set: Callable[[str, str, str], None] = None,
    env_get: Callable[[str], str | None] = None,
    has_encrypted_rows: Callable[[], bool],
    generate_key: Callable[[], bytes] = Fernet.generate_key,
) -> Fernet:
    """Resolve the master Fernet via keyring -> env -> first-run provision -> loud fail.

    Seams are keyword-injectable (defaults hit the real keyring/env) so the whole
    resolution order is unit-testable with no OS keyring and no DB. Result is cached.
    ``has_encrypted_rows`` is the ONLY guard distinguishing genuine-first-run (safe to
    generate) from lost-key (must fail, never orphan data); it MUST fail closed.
    """
    global _FERNET_CACHE
    if _FERNET_CACHE is not None:
        return _FERNET_CACHE

    import keyring as _keyring  # lazy: avoid import cost / backend probe at module load

    keyring_get = keyring_get or _keyring.get_password
    keyring_set = keyring_set or _keyring.set_password
    env_get = env_get or os.environ.get

    # Step 1 — OS keyring.
    key, backend_available = _keyring_lookup(keyring_get)
    if key:
        _FERNET_CACHE = _build_fernet(key, source="OS keyring master_key")
        return _FERNET_CACHE

    # Step 2 — AHF_MASTER_KEY env (headless/Docker path).
    env_key = (env_get("AHF_MASTER_KEY") or "").strip()
    if env_key:
        _FERNET_CACHE = _build_fernet(env_key, source="AHF_MASTER_KEY")
        return _FERNET_CACHE

    # Step 3 — first-run provisioning, gated on "no encrypted rows exist". Fail closed:
    # if the probe errors we assume rows may exist and refuse to generate.
    try:
        rows_exist = bool(has_encrypted_rows())
    except Exception as exc:
        logger.warning("could not probe for encrypted rows (%s); assuming they exist (fail-closed)", exc)
        rows_exist = True

    if not rows_exist:
        new_key = generate_key().decode()
        if backend_available:
            keyring_set(keyring_namespace(), _KEYRING_ITEM, new_key)
            logger.warning("KEY_ENCRYPTION first-run: provisioned a new master key in the OS keyring (namespace %r).", keyring_namespace())
            _FERNET_CACHE = _build_fernet(new_key, source="provisioned keyring master_key")
            return _FERNET_CACHE
        # Headless: never mint a silent ephemeral key (it would regenerate every boot
        # and orphan rows). Log the generated key for the operator to pin, then hard-fail
        # WITHOUT the key in the exception message — so no `str(exc)` / HTTP-500 body can
        # ever transport the master key (the message points to the server log instead).
        logger.error(
            "KEY_ENCRYPTION first run with no OS keyring backend and no AHF_MASTER_KEY. A new master " "key was generated; set AHF_MASTER_KEY to the following value and restart: %s",
            new_key,
        )
        raise CryptoMasterKeyError("KEY_ENCRYPTION is on but no OS keyring backend is available and AHF_MASTER_KEY is unset. " "A new master key was generated and written to the server log — set AHF_MASTER_KEY to that " "value and restart.")

    # Step 4 — encrypted rows exist but no key resolved: loud, terminal (never generate).
    raise CryptoMasterKeyError("KEY_ENCRYPTION is on and encrypted API keys exist, but no master key was found. " f"Restore it via the OS keyring (namespace {keyring_namespace()!r}, item {_KEYRING_ITEM!r}) " "or set AHF_MASTER_KEY=<your Fernet key>. Without the original key these rows are unrecoverable.")


# ── the cipher used by the repository (encrypt) and service (decrypt) ────────
class KeyCipher:
    """Encrypt/decrypt a key string. ``enabled`` gates encrypt; the ``enc:v1:`` tag
    gates decrypt. ``fernet_provider`` is called LAZILY (only when a tagged value is
    decrypted or an encrypt happens while enabled), so the off path never resolves a
    master key and tests can inject a fixed Fernet without any keyring/env/DB."""

    def __init__(self, *, enabled: bool, fernet_provider: Callable[[], Fernet]):
        self._enabled = enabled
        self._fernet_provider = fernet_provider
        self._fernet: Fernet | None = None

    def _fernet_handle(self) -> Fernet:
        if self._fernet is None:
            self._fernet = self._fernet_provider()
        return self._fernet

    def encrypt(self, raw: str) -> str:
        # Flag-gated + idempotent: off -> store plaintext; already-tagged -> don't nest.
        if not self._enabled or raw.startswith(TAG):
            return raw
        token = self._fernet_handle().encrypt(raw.encode()).decode()
        return TAG + token

    def decrypt(self, stored: str) -> str:
        # Tag-gated (flag-independent): legacy plaintext (no tag) returns verbatim.
        if not stored.startswith(TAG):
            return stored
        try:
            return self._fernet_handle().decrypt(stored[len(TAG) :].encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("could not decrypt a stored API key: master key mismatch or corrupted ciphertext.") from exc


def get_cipher(db) -> KeyCipher:
    """Production cipher bound to a DB session for the first-run provisioning probe.

    ``enabled`` is read once here; the master key is resolved lazily on first use.
    """
    from app.backend.repositories.api_key_repository import ApiKeyRepository

    return KeyCipher(
        enabled=is_encryption_enabled(),
        fernet_provider=lambda: _resolve_fernet(has_encrypted_rows=lambda: ApiKeyRepository(db).has_encrypted_rows()),
    )


def resolve_master_fernet(db) -> Fernet:
    """Resolve the CURRENT (old) master Fernet for rotation — keyring -> env -> provision ->
    loud fail, the SAME order as ``get_cipher``. Returns the resolved Fernet object directly
    (not wrapped in a ``KeyCipher``) so rotation can decrypt existing rows with the OLD key
    while a separately-built NEW Fernet (``build_fernet``) re-encrypts them. Shares the process
    cache, exactly like ``get_cipher`` — the resolved key is the master that wrote the rows."""
    from app.backend.repositories.api_key_repository import ApiKeyRepository

    return _resolve_fernet(has_encrypted_rows=lambda: ApiKeyRepository(db).has_encrypted_rows())
