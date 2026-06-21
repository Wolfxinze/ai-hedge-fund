import logging
from typing import Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.backend.database.models import ApiKey
from app.backend.models.schemas import ApiKeyResponse, ApiKeySummaryResponse
from app.backend.repositories.api_key_repository import ApiKeyRepository
from app.backend.services.crypto import CryptoError, KeyCipher

logger = logging.getLogger(__name__)


class ApiKeyService:
    """Load + project API keys. The SINGLE decrypt boundary: every internal consumer
    receives decrypted values via this service; the route projection never exposes the
    raw key (only is_set + a masked tail)."""

    def __init__(self, db: Session, cipher: KeyCipher | None = None):
        self.repository = ApiKeyRepository(db, cipher=cipher)

    @property
    def cipher(self) -> KeyCipher:
        return self.repository.cipher

    def get_api_keys_dict(self) -> Dict[str, str]:
        """All active API keys, decrypted, as a {provider: key} dict for requests.

        Fails CLOSED: a single undecryptable row raises rather than silently feeding a
        wrong/ciphertext value to an LLM/provider call (the dict is all-or-nothing)."""
        api_keys = self.repository.get_all_api_keys(include_inactive=False)
        return {key.provider: self.cipher.decrypt(key.key_value) for key in api_keys}

    def get_api_key(self, provider: str) -> Optional[str]:
        """Get a specific decrypted API key by provider."""
        api_key = self.repository.get_api_key_by_provider(provider)
        return self.cipher.decrypt(api_key.key_value) if api_key else None

    # ── route projection (never returns the raw key) ─────────────────────────
    def _project(self, api_key: ApiKey) -> Tuple[bool, str]:
        """Decrypt -> (is_set, masked_tail). masked_tail is the last 4 chars of the
        PLAINTEXT key (or '*'*len for very short keys, '' when no key). A decrypt
        failure is surfaced as set-but-unreadable (is_set True, tail '') with a loud
        server log — the key exists, it just cannot be read, so the UI can prompt a
        replace rather than silently showing 'not set'."""
        try:
            plaintext = self.cipher.decrypt(api_key.key_value or "")
        except CryptoError:
            logger.warning("API key for provider %s is stored but undecryptable (master key mismatch?)", api_key.provider)
            return True, ""
        if not plaintext:
            return False, ""
        if len(plaintext) >= 4:
            return True, plaintext[-4:]
        return True, "*" * len(plaintext)

    def to_response(self, api_key: ApiKey) -> ApiKeyResponse:
        is_set, masked_tail = self._project(api_key)
        return ApiKeyResponse(
            id=api_key.id,
            provider=api_key.provider,
            is_set=is_set,
            masked_tail=masked_tail,
            is_active=api_key.is_active,
            description=api_key.description,
            created_at=api_key.created_at,
            updated_at=api_key.updated_at,
            last_used=api_key.last_used,
        )

    def to_summary(self, api_key: ApiKey) -> ApiKeySummaryResponse:
        is_set, masked_tail = self._project(api_key)
        return ApiKeySummaryResponse(
            id=api_key.id,
            provider=api_key.provider,
            is_active=api_key.is_active,
            description=api_key.description,
            created_at=api_key.created_at,
            updated_at=api_key.updated_at,
            last_used=api_key.last_used,
            has_key=is_set,
            is_set=is_set,
            masked_tail=masked_tail,
        )
