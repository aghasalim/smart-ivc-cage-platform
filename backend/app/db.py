import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from .config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")

# Global write-lock: guarantees only one thread can write to SQLite at a time.
# This prevents "database is locked" errors under concurrent load (simulator +
# health checks + API calls) even during Railway rolling restarts.
_sqlite_write_lock = threading.Lock()

if _is_sqlite:
    db_path = settings.database_url.replace("sqlite:///", "").lstrip("/")
    if db_path and "/" in db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False, "timeout": 30}

    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")
        dbapi_conn.execute("PRAGMA busy_timeout=30000")  # 30s busy wait
        dbapi_conn.execute("PRAGMA wal_autocheckpoint=100")

else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
    )
    _sqlite_write_lock = None  # type: ignore[assignment]


def checkpoint_wal() -> None:
    """Force a WAL checkpoint at startup to clear any stale lock from a prior crash."""
    if not _is_sqlite:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:
        pass  # Non-fatal — DB may not exist yet

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency — acquires the write-lock for the duration of the request."""
    lock = _sqlite_write_lock
    if lock is not None:
        lock.acquire()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        if lock is not None:
            lock.release()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-manager for background tasks — same write-lock serialisation."""
    lock = _sqlite_write_lock
    if lock is not None:
        lock.acquire()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if lock is not None:
            lock.release()
