"""Tests for the in-process pub/sub used by SSE."""

from __future__ import annotations

import asyncio

import pytest

from tokview.pubsub import PubSub

pytestmark = pytest.mark.asyncio


async def test_publish_with_no_subscribers_is_noop():
    p = PubSub()
    await p.publish({"a": 1})  # must not raise


async def test_publish_fans_out_to_all_subscribers():
    p = PubSub()
    received = {"a": [], "b": []}

    async def consume(name: str, target_count: int):
        async with p.subscribe() as q:
            for _ in range(target_count):
                received[name].append(await asyncio.wait_for(q.get(), timeout=1.0))

    a = asyncio.create_task(consume("a", 2))
    b = asyncio.create_task(consume("b", 2))
    await asyncio.sleep(0.01)  # let subscribers register

    await p.publish({"msg": "hello"})
    await p.publish({"msg": "world"})
    await asyncio.gather(a, b)

    assert received["a"] == [{"msg": "hello"}, {"msg": "world"}]
    assert received["b"] == [{"msg": "hello"}, {"msg": "world"}]


async def test_subscriber_unregisters_on_exit():
    p = PubSub()
    async with p.subscribe():
        assert p.subscriber_count == 1
    assert p.subscriber_count == 0


async def test_slow_consumer_drops_oldest_does_not_block_publisher():
    """If a subscriber doesn't drain its queue, publish drops oldest
    rather than blocking the producer (logging must not back-pressure
    the request hot path)."""
    p = PubSub(queue_size=2)
    async with p.subscribe() as q:
        await p.publish({"i": 1})
        await p.publish({"i": 2})
        await p.publish({"i": 3})  # queue is full -> drops oldest, inserts 3
        await p.publish({"i": 4})  # drops 2, inserts 4
        items = [q.get_nowait() for _ in range(2)]
    # Last two messages survive; first two were dropped.
    assert items[-1]["i"] == 4
    assert items[0]["i"] == 3


async def test_publish_does_not_block_when_no_consumer_drains():
    """publish() must complete promptly even if the consumer never reads."""
    p = PubSub(queue_size=3)
    async with p.subscribe():
        for i in range(10):
            await asyncio.wait_for(p.publish({"i": i}), timeout=0.5)
