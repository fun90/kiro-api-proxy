import asyncio
import json
from dataclasses import replace

from kiro_api_proxy import main
from kiro_api_proxy.transports import ErrorCategory, EventType, GenerationEvent
from kiro_api_proxy.config import Settings
from kiro_api_proxy.transports import GenerationRequest


async def _fake_events(*args, **kwargs):
    yield GenerationEvent(EventType.TEXT_DELTA, text="你")
    await asyncio.sleep(0)
    yield GenerationEvent(EventType.TEXT_DELTA, text="好")
    yield GenerationEvent(EventType.DONE)


async def test_chat_stream_is_incremental(monkeypatch):
    monkeypatch.setattr(main, "_events", _fake_events)
    chunks = [
        chunk
        async for chunk in main.chat_stream(
            "auto", "提示", "chatcmpl-test", 1
        )
    ]
    assert '"content": "你"' in chunks[0]
    assert '"content": "好"' in chunks[1]
    assert '"finish_reason": "stop"' in chunks[-2]
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_chat_stream_includes_requested_usage(monkeypatch):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(
            EventType.USAGE,
            data={
                "input_tokens": 12,
                "output_tokens": 7,
                "cache_read_input_tokens": 5,
                "reasoning_tokens": 2,
            },
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    chunks = [
        chunk
        async for chunk in main.chat_stream(
            "auto",
            "提示",
            "chatcmpl-test",
            1,
            include_usage=True,
        )
    ]
    usage_chunk = json.loads(chunks[-2].removeprefix("data: "))
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["prompt_tokens"] == 12
    assert usage_chunk["usage"]["completion_tokens"] == 7
    assert usage_chunk["usage"]["prompt_tokens_details"][
        "cached_tokens"
    ] == 5
    assert usage_chunk["usage"]["completion_tokens_details"][
        "reasoning_tokens"
    ] == 2


async def test_chat_stream_prefers_context_usage_over_prompt_estimate(monkeypatch):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(
            EventType.USAGE,
            data={"context_tokens": 4096, "context_window": 200000},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    chunks = [
        chunk
        async for chunk in main.chat_stream(
            "auto",
            "很短的提示",
            "chatcmpl-test",
            1,
            include_usage=True,
        )
    ]
    usage_chunk = json.loads(chunks[-2].removeprefix("data: "))
    assert usage_chunk["usage"]["prompt_tokens"] == 4096


async def test_anthropic_stream_is_incremental(monkeypatch):
    monkeypatch.setattr(main, "_events", _fake_events)
    request = main.AnthropicRequest(
        model="auto",
        messages=[{"role": "user", "content": "你好"}],
    )
    chunks = [
        chunk
        async for chunk in main.anthropic_stream(request, "msg_test")
    ]
    deltas = [chunk for chunk in chunks if "content_block_delta" in chunk]
    assert len(deltas) == 2
    assert '"text": "你"' in deltas[0]
    assert '"text": "好"' in deltas[1]
    start = json.loads(chunks[0].split("data: ", 1)[1])
    assert start["message"]["usage"]["input_tokens"] > 0
    delta = json.loads(chunks[-2].split("data: ", 1)[1])
    assert delta["usage"]["output_tokens"] > 0
    assert "message_stop" in chunks[-1]


async def test_anthropic_stream_prefers_upstream_output_usage(monkeypatch):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(
            EventType.USAGE,
            data={"input_tokens": 12, "output_tokens": 7},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    request = main.AnthropicRequest(
        model="auto",
        messages=[{"role": "user", "content": "你好"}],
    )
    chunks = [
        chunk
        async for chunk in main.anthropic_stream(request, "msg_test")
    ]
    delta = json.loads(chunks[-2].split("data: ", 1)[1])
    assert delta["usage"]["output_tokens"] == 7


async def test_anthropic_stream_delta_carries_full_usage(monkeypatch):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(
            EventType.USAGE,
            data={"context_tokens": 3200, "context_window": 200000},
        )
        yield GenerationEvent(
            EventType.USAGE,
            data={
                "input_tokens": 3210,
                "output_tokens": 7,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 12,
            },
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    request = main.AnthropicRequest(
        model="auto",
        messages=[{"role": "user", "content": "你好"}],
    )
    chunks = [
        chunk
        async for chunk in main.anthropic_stream(request, "msg_test")
    ]
    # message_delta 必须回传真实 input 与缓存 token，供客户端计算上下文占用。
    delta = json.loads(chunks[-2].split("data: ", 1)[1])["usage"]
    assert delta["input_tokens"] == 3210
    assert delta["output_tokens"] == 7
    assert delta["cache_read_input_tokens"] == 3000
    assert delta["cache_creation_input_tokens"] == 12


async def test_anthropic_stream_falls_back_to_context_tokens(monkeypatch):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        # 上游只给出上下文用量（used/size），未给出独立 inputTokens。
        yield GenerationEvent(
            EventType.USAGE,
            data={"context_tokens": 4096, "context_window": 200000},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    request = main.AnthropicRequest(
        model="auto",
        messages=[{"role": "user", "content": "你好"}],
    )
    chunks = [
        chunk
        async for chunk in main.anthropic_stream(request, "msg_test")
    ]
    delta = json.loads(chunks[-2].split("data: ", 1)[1])["usage"]
    # 缺少独立 input_tokens 时，回退采用上游真实上下文用量。
    assert delta["input_tokens"] == 4096


async def test_responses_stream_is_incremental(monkeypatch):
    monkeypatch.setattr(main, "_events", _fake_events)
    request = main.ResponsesRequest(model="auto", input="你好", stream=True)
    chunks = [
        chunk async for chunk in main.responses_stream(request)
    ]
    assert any("response.created" in chunk for chunk in chunks)
    assert sum("response.output_text.delta" in chunk for chunk in chunks) == 2
    assert any("response.completed" in chunk for chunk in chunks)
    completed = json.loads(chunks[-2].split("data: ", 1)[1])
    assert completed["response"]["usage"]["input_tokens"] > 0
    assert completed["response"]["usage"]["output_tokens"] > 0


async def test_responses_stream_prefers_context_usage_over_prompt_estimate(
    monkeypatch,
):
    async def usage_events(*args, **kwargs):
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(
            EventType.USAGE,
            data={"context_tokens": 8192, "context_window": 200000},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", usage_events)
    request = main.ResponsesRequest(model="auto", input="很短的提示", stream=True)
    chunks = [chunk async for chunk in main.responses_stream(request)]
    completed = next(chunk for chunk in chunks if "response.completed" in chunk)
    payload = json.loads(completed.split("data: ", 1)[1])
    assert payload["response"]["usage"]["input_tokens"] == 8192


async def test_stream_error_is_encoded(monkeypatch):
    async def error_events(*args, **kwargs):
        yield GenerationEvent(EventType.ERROR, text="上游中断")

    monkeypatch.setattr(main, "_events", error_events)
    chunks = [
        chunk
        async for chunk in main.chat_stream(
            "auto", "提示", "chatcmpl-test", 1
        )
    ]
    assert "上游中断" in chunks[0]
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_client_disconnect_closes_upstream(monkeypatch):
    closed = False

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            nonlocal closed
            try:
                yield GenerationEvent(EventType.TEXT_DELTA, text="不应发送")
            finally:
                closed = True

    class Disconnected:
        async def is_disconnected(self):
            return True

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)
    assert [
        item
        async for item in main._events(
            "auto", "提示", None, Disconnected()
        )
    ] == []
    assert closed


async def test_client_disconnect_cancels_silent_upstream(monkeypatch):
    cancelled = asyncio.Event()

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            try:
                await asyncio.Event().wait()
                yield GenerationEvent(EventType.DONE)
            finally:
                cancelled.set()

    class Disconnected:
        async def is_disconnected(self):
            return True

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)
    assert [
        item
        async for item in main._events(
            "auto", "静默请求", None, Disconnected()
        )
    ] == []
    assert cancelled.is_set()


async def test_duplicate_inflight_generation_is_rejected(monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            started.set()
            await release.wait()
            yield GenerationEvent(EventType.DONE)

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)

    async def consume():
        return [
            item
            async for item in main._events(
                "auto", "相同请求", None, session_id="session"
            )
        ]

    first = asyncio.create_task(consume())
    await started.wait()
    duplicate = await consume()
    assert duplicate[0].type is EventType.ERROR
    assert duplicate[0].data["category"] == ErrorCategory.CAPACITY.value
    release.set()
    await first


async def test_generation_has_absolute_total_timeout(monkeypatch):
    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            await asyncio.sleep(1)
            yield GenerationEvent(EventType.DONE)

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, timeout_seconds=0.02),
    )
    events = [
        item async for item in main._events("auto", "超时请求", None)
    ]
    assert events[-1].type is EventType.ERROR
    assert events[-1].data["category"] == ErrorCategory.TIMEOUT.value


async def test_outer_cancellation_stops_pending_upstream_before_close(
    monkeypatch,
):
    started = asyncio.Event()
    closed = asyncio.Event()

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            try:
                started.set()
                await asyncio.Event().wait()
                yield GenerationEvent(EventType.DONE)
            finally:
                closed.set()

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)

    async def consume():
        return [item async for item in main._events("auto", "取消", None)]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()


async def test_slow_consumer_keeps_chunk_order(monkeypatch):
    monkeypatch.setattr(main, "_events", _fake_events)
    values = []
    async for chunk in main.chat_stream(
        "auto", "提示", "chatcmpl-test", 1
    ):
        values.append(chunk)
        await asyncio.sleep(0.001)
    assert values[0].find('"你"') < len(values[0])
    assert values[1].find('"好"') < len(values[1])


async def test_events_injects_heartbeat_during_upstream_silence(monkeypatch):
    release = asyncio.Event()

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            await release.wait()
            yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
            yield GenerationEvent(EventType.DONE)

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)
    # 心跳间隔极短，总时长上限足够长，确保空档期先冒出心跳。
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, heartbeat_seconds=0.02, timeout_seconds=5),
    )

    collected: list[GenerationEvent] = []

    async def consume():
        async for item in main._events("auto", "静默", None):
            collected.append(item)

    task = asyncio.create_task(consume())
    # 让空档持续足够触发若干次心跳。
    await asyncio.sleep(0.1)
    assert any(e.type is EventType.HEARTBEAT for e in collected)
    release.set()
    await task
    # 上游文本最终仍被送达。
    assert any(
        e.type is EventType.TEXT_DELTA and e.text == "你好" for e in collected
    )


async def test_heartbeat_disabled_when_zero(monkeypatch):
    release = asyncio.Event()

    class FakeTransport:
        name = "fake"

        async def stream(self, request):
            await release.wait()
            yield GenerationEvent(EventType.DONE)

    async def valid_model(model):
        return None

    monkeypatch.setattr(main, "transport", FakeTransport())
    monkeypatch.setattr(main, "ensure_model", valid_model)
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, heartbeat_seconds=0, timeout_seconds=5),
    )

    collected: list[GenerationEvent] = []

    async def consume():
        async for item in main._events("auto", "静默", None):
            collected.append(item)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    assert not any(e.type is EventType.HEARTBEAT for e in collected)
    release.set()
    await task


async def test_chat_stream_translates_heartbeat_to_comment(monkeypatch):
    async def heartbeat_events(*args, **kwargs):
        yield GenerationEvent(EventType.HEARTBEAT)
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好")
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", heartbeat_events)
    chunks = [
        chunk
        async for chunk in main.chat_stream(
            "auto", "提示", "chatcmpl-test", 1
        )
    ]
    assert chunks[0] == ": keep-alive\n\n"
    assert any('"content": "你好"' in c for c in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
