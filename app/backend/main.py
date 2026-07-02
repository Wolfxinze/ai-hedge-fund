import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Register observing-pool tables on the shared Base so create_all discovers them
# (PRD v4 §8.1 model-discovery). Import is for its registration side-effect.
import src.storage.models  # noqa: F401
from app.backend.cors import cors_allowed_origins
from app.backend.database.connection import engine
from app.backend.database.models import Base
from app.backend.routes import api_router
from app.backend.services.ollama_service import ollama_service
from src.scheduler.scheduler import build_scheduler, start_scheduler, stop_scheduler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# §19 gate: refuse to bind a non-loopback host without a recorded counsel sign-off.
# Runs at import (before uvicorn binds) so a misconfigured deploy exits non-zero rather
# than exposing the surface. Loopback / unset / dev / CI is a no-op (byte-for-byte
# unchanged). The human legal act itself stays open by design (PRD §19).
from src.compliance import enforce_nonloopback_signoff

enforce_nonloopback_signoff()

app = FastAPI(title="AI Hedge Fund API", description="Backend API for AI Hedge Fund", version="0.1.0")

# Dev convenience: create any missing tables. Alembic migrations are the canonical
# schema source (run `alembic upgrade head`); create_all coexists idempotently.
Base.metadata.create_all(bind=engine)

# Phase 8 in-process scheduler handle (set on startup, stopped on shutdown).
_scheduler = None

# Configure CORS — loopback allowlist + only the verbs the API actually serves.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Include all routes
app.include_router(api_router)

@app.on_event("startup")
async def startup_event():
    """Startup event to check Ollama availability."""
    try:
        logger.info("Checking Ollama availability...")
        status = await ollama_service.check_ollama_status()
        
        if status["installed"]:
            if status["running"]:
                logger.info(f"✓ Ollama is installed and running at {status['server_url']}")
                if status["available_models"]:
                    logger.info(f"✓ Available models: {', '.join(status['available_models'])}")
                else:
                    logger.info("ℹ No models are currently downloaded")
            else:
                logger.info("ℹ Ollama is installed but not running")
                logger.info("ℹ You can start it from the Settings page or manually with 'ollama serve'")
        else:
            logger.info("ℹ Ollama is not installed. Install it to use local models.")
            logger.info("ℹ Visit https://ollama.com to download and install Ollama")
            
    except Exception as e:
        logger.warning(f"Could not check Ollama status: {e}")
        logger.info("ℹ Ollama integration is available if you install it later")

    # Phase 8/9: start the in-process observing-pools scheduler. A misconfigured
    # OBSERVING_POOL_REFRESH_CRON raises here — caught + logged so the API still starts (just
    # without the scheduler), rather than failing the whole boot.
    # app.state.scheduler is the route-facing handle (used by hot-reload in monitors.py);
    # _scheduler is the module-global used for the shutdown event (both are set together).
    global _scheduler
    try:
        _scheduler = build_scheduler()
        start_scheduler(_scheduler)
        app.state.scheduler = _scheduler
    except Exception:
        logger.exception("APScheduler failed to start; API continuing without it")
        _scheduler = None
        app.state.scheduler = None


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the scheduler on shutdown (wait=False — don't block ASGI teardown on an in-flight
    refresh; the PoolLock finally-release + TTL cover lock cleanup)."""
    global _scheduler
    if _scheduler is not None:
        stop_scheduler(_scheduler)
        _scheduler = None
        app.state.scheduler = None
