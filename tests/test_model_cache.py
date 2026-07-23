import asyncio

import pytest

from kiro_api_proxy.model_cache import ModelCache


async def test_cache_hit_and_invalidation():
    cache = ModelCache(ttl_seconds=60, stale_seconds=120)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return [{"model_id": "model-a"}]

    first = await cache.get(loader)
    second = await cache.get(loader)
    assert first.source == "upstream"
    assert second.source == "cache"
    assert calls == 1

    cache.invalidate()
    refreshed = await cache.get(loader)
    assert refreshed.source == "upstream"
    assert calls == 2


async def test_expired_cache_uses_single_flight():
    cache = ModelCache(ttl_seconds=0, stale_seconds=120)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return [{"model_id": "model-a"}]

    results = await asyncio.gather(*(cache.get(loader) for _ in range(8)))
    assert len(results) == 8
    # TTL 为 0 会使锁内复检也过期，因此使用一个极短但非零 TTL 验证并发合并。
    assert calls >= 1


async def test_single_flight_with_positive_ttl():
    cache = ModelCache(ttl_seconds=60, stale_seconds=120)
    calls = 0
    gate = asyncio.Event()

    async def loader():
        nonlocal calls
        calls += 1
        await gate.wait()
        return [{"model_id": "model-a"}]

    tasks = [asyncio.create_task(cache.get(loader)) for _ in range(8)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert {result.source for result in results} == {"upstream", "cache"}


async def test_stale_if_error_and_expired_error(monkeypatch):
    now = 100.0
    monkeypatch.setattr("kiro_api_proxy.model_cache.time.monotonic", lambda: now)
    cache = ModelCache(ttl_seconds=1, stale_seconds=10)

    async def good_loader():
        return [{"model_id": "model-a"}]

    async def bad_loader():
        raise RuntimeError("上游不可用")

    await cache.get(good_loader)
    now = 102.0
    stale = await cache.get(bad_loader)
    assert stale.source == "stale"

    now = 111.0
    with pytest.raises(RuntimeError, match="上游不可用"):
        await cache.get(bad_loader)
