"""In-process pub/sub for spend events.

Single Python process, asyncio-only. The HtvLogger publishes each row;
SSE subscribers (one per connected dashboard tab) drain from their own
bounded queue. On slow consumer, we drop the oldest event rather than
backpressure the producer (we never want logging to block requests).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


class PubSub:
    def __init__(self, *, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, message: Any) -> None:
        """Fan-out to every active subscriber. Never blocks; drops on slow consumer."""
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer — drop the oldest and try again. We prefer
                # dropping events to blocking the request hot path.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.warning("htv pubsub: dropped event for slow subscriber")

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Any]]:
        """Context-managed subscription. Use:

            async with pubsub.subscribe() as q:
                while True:
                    msg = await q.get()
                    ...
        """
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(q)
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
