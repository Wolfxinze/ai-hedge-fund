import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Register observing-pool tables on the shared Base so create_all discovers them
# (PRD v4 §8.1 model-discovery). Import is for its registration side-effect.
import src.storage.models  # noqa: F401
from app.backend.database.connection import engine
from app.backend.database.models import Base
from app.backend.routes import api_router
from app.backend.services.ollama_service import ollama_service
from src.scheduler.scheduler import build_scheduler, start_scheduler, stop_scheduler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Hedge Fund API", description="Backend API for AI Hedge Fund", version="0.1.0")

# Dev convenience: create any missing tables. Alembic migrations are the canonical
# schema source (run `alembic upgrade head`); create_all coexists idempotently.
Base.metadata.create_all(bind=engine)

# Phase 8 in-process scheduler handle (set on startup, stopped on shutdown).
_scheduler = None

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Frontend URLs
    allow_credentials=True,
    allow_methods=["*"],
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

    # Phase 8: start the in-process observing-pools scheduler. A misconfigured
    # OBSERVING_POOL_REFRESH_CRON raises here — caught + logged so the API still starts (just
    # without the scheduler), rather than failing the whole boot.
    global _scheduler
    try:
        _scheduler = build_scheduler()
        start_scheduler(_scheduler)
    except Exception:
        logger.exception("APScheduler failed to start; API continuing without it")
        _scheduler = None


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the scheduler on shutdown (wait=False — don't block ASGI teardown on an in-flight
    refresh; the PoolLock finally-release + TTL cover lock cleanup)."""
    global _scheduler
    if _scheduler is not None:
        stop_scheduler(_scheduler)
        _scheduler = None
