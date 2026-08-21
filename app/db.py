"""Postgres connection pool for the web process.

Deliberately synchronous. FastAPI runs `def` handlers and dependencies in a
threadpool, so database work never blocks the event loop, and the loop stays
free for the two things that genuinely need it: the LLM HTTP call and the MCP
subprocess. It also keeps local development working on Windows, where psycopg's
async mode and asyncio subprocesses require different, mutually exclusive event
loop implementations.

Bookmark SQL is not here. The only component allowed to touch the `bookmarks`
table is the MCP server (`mcp_server/repository.py`).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

_pool: ConnectionPool | None = None


def open_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _pool.open(wait=True, timeout=30)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator:
    """Yield a pooled connection. psycopg commits on clean exit and rolls back
    if the block raises."""
    if _pool is None:
        raise RuntimeError("Connection pool is not open; call open_pool() first.")
    with _pool.connection() as conn:
        yield conn
