"""Async Postgres access: a small connection pool plus a TTL cache for query results.

The registry data changes at most every few minutes (the upstream parser refreshes the
materialized views hourly), so caching results for minutes is safe and keeps load on the
shared database low.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("wsdc_mcp.db")

P = ParamSpec("P")
T = TypeVar("T")


class Database:
    def __init__(self, dsn: str, *, max_size: int = 3, statement_timeout_ms: int = 8000):
        # Every session is read-only and time-boxed, whatever the role's own defaults are.
        options = (
            f"-c statement_timeout={int(statement_timeout_ms)} "
            "-c default_transaction_read_only=on "
            "-c lock_timeout=2000"
        )
        self.pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=max_size,
            timeout=15,
            max_idle=300,
            kwargs={"options": options, "application_name": "wsdc-mcp-public", "autocommit": True},
            open=False,
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params or {})
                rows = await cur.fetchall()
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > 1500:
            logger.warning("slow query (%.0f ms): %s", elapsed, " ".join(sql.split())[:200])
        return rows

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None


class TTLCache:
    """A tiny LRU cache with per-entry expiry, shared by all cached data functions."""

    def __init__(self, max_entries: int = 2000):
        self.max_entries = max_entries
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> tuple[bool, Any]:
        item = self._data.get(key)
        if item is None:
            return False, None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return False, None
        self._data.move_to_end(key)
        return True, value

    def set(self, key: Any, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


CACHE = TTLCache()


def cached(ttl: float) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Cache an async function's result by its (hashable) arguments.

    Concurrent calls with the same key share one in-flight computation, so a burst of
    identical requests hits the database once.
    """

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        inflight: dict[Any, asyncio.Future[Any]] = {}

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # args[0] is the Database instance; it is not part of the key.
            key = (fn.__qualname__, args[1:], tuple(sorted(kwargs.items())))
            hit, value = CACHE.get(key)
            if hit:
                return value
            pending = inflight.get(key)
            if pending is not None:
                return await asyncio.shield(pending)
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            inflight[key] = future
            try:
                value = await fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
                # Mark retrieved so an unawaited failure does not log "exception never retrieved".
                future.exception()
                raise
            else:
                CACHE.set(key, value, ttl)
                future.set_result(value)
                return value
            finally:
                inflight.pop(key, None)

        return wrapper

    return decorator
