import asyncio

from kiro_api_proxy.sessions import SessionRecord, SessionStore


async def test_session_lru_and_isolation():
    store = SessionStore(ttl_seconds=60, max_entries=2)
    await store.put(SessionRecord("tenant-a:same", "w1", "s1"))
    await store.put(SessionRecord("tenant-b:same", "w1", "s2"))
    assert (await store.get("tenant-a:same")).upstream_session_id == "s1"
    assert (await store.get("tenant-b:same")).upstream_session_id == "s2"

    await store.put(SessionRecord("tenant-c:new", "w2", "s3"))
    assert await store.get("tenant-a:same") is None


async def test_session_expiry_and_worker_orphan(monkeypatch):
    now = 100.0
    monkeypatch.setattr("kiro_api_proxy.sessions.time.monotonic", lambda: now)
    store = SessionStore(ttl_seconds=10, max_entries=10)
    record = SessionRecord("tenant-a:same", "w1", "s1")
    await store.put(record)
    await store.orphan_worker("w1")
    recovered = await store.get("tenant-a:same")
    assert recovered.worker_id == ""
    assert recovered.rebuilt is True
    assert recovered.turn_count == 0
    assert recovered.upstream_context_chars == 0
    assert not hasattr(recovered, "history")

    now = 111.0
    assert await store.get("tenant-a:same") is None


async def test_same_session_lock_serializes():
    record = SessionRecord("key", "worker", "session")
    active = 0
    maximum = 0

    async def work():
        nonlocal active, maximum
        async with record.lock:
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(work(), work())
    assert maximum == 1
