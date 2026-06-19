from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from short_trading_bot.persistence.db import create_engine, init_models, session_factory


@pytest_asyncio.fixture
async def sf(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A fresh file-backed sqlite DB + async session factory per test."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_models(engine)
    yield session_factory(engine)
    await engine.dispose()
