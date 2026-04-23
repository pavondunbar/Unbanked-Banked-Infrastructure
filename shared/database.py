"""PostgreSQL connection pool and session factory.

All services share the same connection configuration.
Sessions are created per-request via FastAPI dependency injection.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://unbanked:unbanked@localhost:5432/unbanked",
)

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
)


@event.listens_for(engine, "connect")
def _set_isolation(dbapi_conn, connection_record):
    dbapi_conn.set_session(
        isolation_level="READ COMMITTED",
        autocommit=False,
    )


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a session, auto-rollback on error."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for non-FastAPI code (workers, scripts)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
