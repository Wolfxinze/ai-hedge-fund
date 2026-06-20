from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Get the backend directory path
BACKEND_DIR = Path(__file__).parent.parent
DATABASE_PATH = BACKEND_DIR / "hedge_fund.db"

# Database configuration - use absolute path
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    # check_same_thread=False: the Phase-8 in-process APScheduler runs jobs in worker
    # threads that share this engine. timeout=30: SQLite busy-wait (seconds) so concurrent
    # writers (scheduler thread + a CLI/API refresh) serialize instead of immediately raising
    # "database is locked" — the bounded retry the PoolLock claim protocol relies on (PRD §10).
    connect_args={"check_same_thread": False, "timeout": 30},
)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 