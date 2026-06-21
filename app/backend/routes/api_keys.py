import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.database.models import ApiKey
from app.backend.models.schemas import (
    ApiKeyBulkUpdateRequest,
    ApiKeyCreateRequest,
    ApiKeyResponse,
    ApiKeySummaryResponse,
    ApiKeyUpdateRequest,
    ErrorResponse,
)
from app.backend.repositories.api_key_repository import ApiKeyRepository
from app.backend.services.api_key_service import ApiKeyService
from app.backend.services.crypto import CryptoError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api-keys", tags=["api-keys"])

# Returned to the client when encryption/decryption fails — generic on purpose so the
# master key / keyring details (which the underlying CryptoError message may carry) are
# logged server-side only and never cross the HTTP boundary.
_CRYPTO_DETAIL = "Encryption is enabled but the master key could not be used. See server logs."


@router.post(
    "/",
    response_model=ApiKeyResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def create_or_update_api_key(request: ApiKeyCreateRequest, db: Session = Depends(get_db)):
    """Create a new API key or update existing one"""
    try:
        service = ApiKeyService(db)
        api_key = service.repository.create_or_update_api_key(
            provider=request.provider,
            key_value=request.key_value,
            description=request.description,
            is_active=request.is_active
        )
        return service.to_response(api_key)  # projection only — never returns key_value
    except CryptoError:
        logger.error("crypto error on create/update API key", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create/update API key: {str(e)}")


@router.get(
    "/",
    response_model=List[ApiKeySummaryResponse],
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_api_keys(include_inactive: bool = False, db: Session = Depends(get_db)):
    """Get all API keys (with is_set + masked_tail, never the actual key value)"""
    try:
        service = ApiKeyService(db)
        api_keys = service.repository.get_all_api_keys(include_inactive=include_inactive)
        return [service.to_summary(key) for key in api_keys]
    except CryptoError:
        logger.error("crypto error on list API keys", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve API keys: {str(e)}")


@router.get(
    "/{provider}",
    response_model=ApiKeyResponse,
    responses={
        404: {"model": ErrorResponse, "description": "API key not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_api_key(provider: str, db: Session = Depends(get_db)):
    """Get a specific API key by provider (is_set + masked_tail only, never the key)"""
    try:
        service = ApiKeyService(db)
        api_key = service.repository.get_api_key_by_provider(provider)
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        return service.to_response(api_key)
    except HTTPException:
        raise
    except CryptoError:
        logger.error("crypto error on get API key", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve API key: {str(e)}")


@router.put(
    "/{provider}",
    response_model=ApiKeyResponse,
    responses={
        404: {"model": ErrorResponse, "description": "API key not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def update_api_key(provider: str, request: ApiKeyUpdateRequest, db: Session = Depends(get_db)):
    """Update an existing API key"""
    try:
        service = ApiKeyService(db)
        api_key = service.repository.update_api_key(
            provider=provider,
            key_value=request.key_value,
            description=request.description,
            is_active=request.is_active
        )
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        return service.to_response(api_key)
    except HTTPException:
        raise
    except CryptoError:
        logger.error("crypto error on update API key", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update API key: {str(e)}")


@router.delete(
    "/{provider}",
    responses={
        204: {"description": "API key deleted successfully"},
        404: {"model": ErrorResponse, "description": "API key not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def delete_api_key(provider: str, db: Session = Depends(get_db)):
    """Delete an API key"""
    try:
        repo = ApiKeyRepository(db)
        success = repo.delete_api_key(provider)
        if not success:
            raise HTTPException(status_code=404, detail="API key not found")
        return {"message": "API key deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete API key: {str(e)}")


@router.patch(
    "/{provider}/deactivate",
    response_model=ApiKeySummaryResponse,
    responses={
        404: {"model": ErrorResponse, "description": "API key not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def deactivate_api_key(provider: str, db: Session = Depends(get_db)):
    """Deactivate an API key without deleting it"""
    try:
        service = ApiKeyService(db)
        success = service.repository.deactivate_api_key(provider)
        if not success:
            raise HTTPException(status_code=404, detail="API key not found")
        # Re-read WITHOUT the is_active filter (get_api_key_by_provider would return None
        # for the now-inactive row). Project through the service so is_set/masked_tail are
        # correct and key_value can never be emitted.
        api_key = db.query(ApiKey).filter(ApiKey.provider == provider).first()
        return service.to_summary(api_key)
    except HTTPException:
        raise
    except CryptoError:
        logger.error("crypto error on deactivate API key", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deactivate API key: {str(e)}")


@router.post(
    "/bulk",
    response_model=List[ApiKeyResponse],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def bulk_update_api_keys(request: ApiKeyBulkUpdateRequest, db: Session = Depends(get_db)):
    """Bulk create or update multiple API keys"""
    try:
        service = ApiKeyService(db)
        api_keys_data = [
            {
                'provider': key.provider,
                'key_value': key.key_value,
                'description': key.description,
                'is_active': key.is_active
            }
            for key in request.api_keys
        ]
        api_keys = service.repository.bulk_create_or_update(api_keys_data)
        return [service.to_response(key) for key in api_keys]
    except CryptoError:
        logger.error("crypto error on bulk update API keys", exc_info=True)
        raise HTTPException(status_code=500, detail=_CRYPTO_DETAIL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to bulk update API keys: {str(e)}")


@router.patch(
    "/{provider}/last-used",
    responses={
        200: {"description": "Last used timestamp updated"},
        404: {"model": ErrorResponse, "description": "API key not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def update_last_used(provider: str, db: Session = Depends(get_db)):
    """Update the last used timestamp for an API key"""
    try:
        repo = ApiKeyRepository(db)
        success = repo.update_last_used(provider)
        if not success:
            raise HTTPException(status_code=404, detail="API key not found")
        return {"message": "Last used timestamp updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update last used timestamp: {str(e)}") 