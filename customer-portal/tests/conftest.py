"""Shared pytest fixtures.

The portal's `app.config.Settings` requires several env vars at module-load
time, so this conftest sets harmless defaults *before* anything imports the
app. Tests that need the real values (DATABASE_URL, etc.) override them via
monkeypatch or by setting the env var before invoking pytest.

`portal_db_url` returns a Postgres connection string. Set
`PORTAL_TEST_DB_URL` if the local pg lives somewhere other than the dockerised
default the test plan documents (postgres:16 on :55432, db=portal_test, user
portal, password portal).
"""

from __future__ import annotations

import os
import secrets

# Set env vars before app modules import.
os.environ.setdefault("OIDC_ISSUER", "https://idp.test.invalid")
os.environ.setdefault("OIDC_CLIENT_ID", "portal-test")
os.environ.setdefault("OIDC_CLIENT_SECRET", "test")
os.environ.setdefault("OIDC_REDIRECT_URI", "http://localhost/callback")
os.environ.setdefault("SECRET_KEY", secrets.token_hex(16))
os.environ.setdefault("PROJECT_GIT_REPO_URL", "/dev/null")
os.environ.setdefault("PROJECT_GIT_USERNAME", "test")
os.environ.setdefault("PROJECT_GIT_TOKEN", "test")
os.environ.setdefault("CLUSTER_DNS_ZONE", "k8s-test.sunetvdc.se")
os.environ.setdefault("PORTAL_ADMIN_USERS", "admin@test")
os.environ.setdefault("BASE_URL", "http://localhost")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "PORTAL_TEST_DB_URL",
        "postgresql+asyncpg://portal:portal@localhost:55432/portal_test",
    ),
)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base  # noqa: E402


def _sync_url(async_url: str) -> str:
    return async_url.replace("+asyncpg", "+psycopg2")


@pytest.fixture(scope="session")
def portal_db_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def sync_db_url(portal_db_url: str) -> str:
    return _sync_url(portal_db_url)


@pytest.fixture(scope="session")
def _migrate(portal_db_url: str) -> None:
    """Run alembic upgrade head once per test session.

    We don't tear down between tests; instead each DB-backed test truncates
    the tables it touched in its own teardown. That keeps tests fast and
    decoupled while still proving the migration matches the models.
    """
    from alembic.config import Config

    from alembic import command

    cfg = Config(os.path.join(os.path.dirname(__file__), os.pardir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _sync_url(portal_db_url))
    command.upgrade(cfg, "head")


@pytest.fixture
async def engine(portal_db_url: str, _migrate):
    eng = create_async_engine(portal_db_url, pool_pre_ping=True)
    yield eng
    await eng.dispose()


def _truncate_non_seed_sql() -> str:
    non_seed = [
        t.name
        for t in reversed(Base.metadata.sorted_tables)
        if t.name not in ("resource_price", "alembic_version")
    ]
    return f"TRUNCATE {', '.join(non_seed)} RESTART IDENTITY CASCADE"


@pytest.fixture
async def session(engine) -> AsyncSession:
    """A clean AsyncSession; truncates non-seed tables before yielding."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text(_truncate_non_seed_sql()))
    async with sm() as s:
        yield s


@pytest.fixture
def sync_session(sync_db_url: str, _migrate):
    """A sync Session against the same DB. Used by billing-engine tests
    (the synthetic-usage emitter is sync because the surrounding billing
    pipeline already is). Truncates non-seed tables before yielding."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(sync_db_url)
    SessionLocal = sessionmaker(bind=eng)
    with eng.begin() as conn:
        conn.execute(text(_truncate_non_seed_sql()))
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        eng.dispose()
