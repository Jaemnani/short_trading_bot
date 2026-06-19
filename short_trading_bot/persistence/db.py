"""Async database engine / session management.

SQLite (aiosqlite) for dev; swap ``db_url`` to PostgreSQL (asyncpg) for prod.
``init_models`` creates tables directly (dev/tests); Alembic owns schema in prod.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def _ensure_sqlite_dir(db_url: str) -> None:
    """Create the parent directory for a file-based sqlite URL if needed."""
    marker = "sqlite+aiosqlite:///"
    if db_url.startswith(marker):
        path = db_url[len(marker) :]
        if path and path not in (":memory:",) and not path.startswith(":memory:"):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)


def create_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    _ensure_sqlite_dir(db_url)
    return create_async_engine(db_url, echo=echo, future=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_models(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
