"""Async engine / session plumbing."""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

# asyncpg connections bind to the event loop that created them, so a process that calls
# asyncio.run() more than once (scripts/eval.py, test helpers) must not reuse a pooled
# connection from a closed loop. Engines are therefore keyed by the running loop.
_engines: dict[int, AsyncEngine] = {}
_sessionmakers: dict[int, async_sessionmaker[AsyncSession]] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_engine() -> AsyncEngine:
    key = _loop_key()
    if key not in _engines:
        # The fetch worker runs up to GLOBAL_FETCH_CONCURRENCY domains at once and each
        # checkpoints its stage, so the pool must cover that or domains fail on
        # pool_timeout rather than on anything real.
        _engines[key] = create_async_engine(
            settings.database_url,
            pool_size=max(5, settings.global_fetch_concurrency // 2),
            max_overflow=settings.global_fetch_concurrency,
            pool_pre_ping=True,
            echo=False,
        )
    return _engines[key]


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    key = _loop_key()
    if key not in _sessionmakers:
        _sessionmakers[key] = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmakers[key]


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the engine bound to the current loop."""
    key = _loop_key()
    engine = _engines.pop(key, None)
    _sessionmakers.pop(key, None)
    if engine is not None:
        await engine.dispose()
