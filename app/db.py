"""Async Postgres connection pool for the web process.

Bookmark SQL deliberately does not live here. The only component allowed to
touch the `bookmarks` table is the MCP server (`mcp_server/repository.py`).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import DATABASE_URL

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await _pool.open(wait=True, timeout=30)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator:
    """Yield a pooled connection wrapped in a transaction."""
    if _pool is None:
        raise RuntimeError("Connection pool is not open; call open_pool() first.")
    async with _pool.connection() as conn:
        yield conn
