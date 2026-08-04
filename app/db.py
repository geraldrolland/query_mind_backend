"""Database engine and session management."""

from functools import lru_cache

from sqlmodel import SQLModel, Session, create_engine

from app.core.settings import settings


if settings.ENVIRONMENT == "production":
    engine = create_engine(settings.PROD_DATABASE_URL, pool_pre_ping=True, echo=False)
else:
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, echo=False)


def get_session():
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session


@lru_cache
def get_engine():
    """Return the shared engine (cached; used by the DSL compiler)."""
    return engine


def init_db() -> None:
    """Create all tables. Uses create_all for MVP simplicity."""
    from app.models import (  # noqa: F401
        dataset,
        datasetrow,
        message,
        user,
    )

    SQLModel.metadata.create_all(engine)
