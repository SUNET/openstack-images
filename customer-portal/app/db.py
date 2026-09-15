"""Database engine and session management."""

import logging
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command as alembic_command

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None


def _to_async_url(url: str) -> str:
    """Convert a postgresql:// URL to postgresql+asyncpg://."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _to_sync_url(url: str) -> str:
    """Convert a database URL to a sync psycopg2 URL for Alembic."""
    url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(_to_async_url(database_url), echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def run_migrations(database_url: str) -> None:
    """Run Alembic migrations to head on startup (sync — runs before event loop)."""
    app_dir = Path(__file__).parent.parent
    alembic_cfg = AlembicConfig(str(app_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(app_dir / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", _to_sync_url(database_url))
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Database migrations applied")


async def close_db() -> None:
    global _engine
    if _engine:
        await _engine.dispose()


async def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    async with _session_factory() as session:
        yield session


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the initialized factory for bounded background operations."""
    if _session_factory is None:
        raise RuntimeError("Database is not initialized")
    return _session_factory
