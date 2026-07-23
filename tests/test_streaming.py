import asyncio
import json

from kiro_api_proxy import main
from kiro_api_proxy.transports import EventType, GenerationEvent
from kiro_api_proxy.config import Settings
from kiro_api_proxy.transports import GenerationRequest
from kiro_api_proxy.transports.cli import CliTransport


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


async def test_cli_decodes_chinese_across_byte_boundaries(monkeypatch):
    class Stream:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def read(self, size=-1):
            return self.chunks.pop(0) if self.chunks else b""

    class Process:
        def __init__(self):
            self.stdout = Stream([b"\xe4", b"\xbd", b"\xa0", b""])
            self.stderr = Stream([b""])
            self.returncode = 0

        async def wait(self):
            return 0

    cli = CliTransport(Settings.from_env())
    process = Process()

    async def spawn(request):
        return process

    monkeypatch.setattr(cli, "_spawn", spawn)
    events = [
        event
        async for event in cli.stream(
            GenerationRequest("auto", "提示")
        )
    ]
    assert "".join(
        event.text for event in events if event.type is EventType.TEXT_DELTA
    ) == "你"


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
