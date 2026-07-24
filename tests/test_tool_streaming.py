"""出站工具调用聚合测试（Anthropic / OpenAI，流式与非流式）。"""

from __future__ import annotations

import json

from kiro_api_proxy import main
from kiro_api_proxy.transports import EventType, GenerationEvent


def _tool_fragments():
    """模拟 Runtime 分片 toolUseEvent 序列 + 一段前置文本。"""
    yield GenerationEvent(EventType.TEXT_DELTA, text="让我查一下。")
    yield GenerationEvent(EventType.TOOL, data={"id": "t1", "name": "get_weather", "input": "", "stop": False})
    yield GenerationEvent(EventType.TOOL, data={"id": "t1", "name": "get_weather", "input": '{"city":', "stop": False})
    yield GenerationEvent(EventType.TOOL, data={"id": "t1", "name": "get_weather", "input": ' "北京"}', "stop": False})
    yield GenerationEvent(EventType.TOOL, data={"id": "t1", "name": "get_weather", "input": "", "stop": True})
    yield GenerationEvent(EventType.USAGE, data={"input_tokens": 10, "output_tokens": 5})
    yield GenerationEvent(EventType.DONE)


def _sse_events(chunks: list[str]) -> list[dict]:
    parsed = []
    for chunk in chunks:
        if "data: " in chunk:
            data = chunk.split("data: ", 1)[1].strip()
            if data and data != "[DONE]":
                parsed.append(json.loads(data))
    return parsed


class TestAnthropicStreamTools:
    async def test_tool_use_blocks_and_stop_reason(self, monkeypatch):
        async def fake_events(*args, **kwargs):
            for event in _tool_fragments():
                yield event

        monkeypatch.setattr(main, "_events", fake_events)
        request = main.AnthropicRequest(
            model="auto",
            messages=[{"role": "user", "content": "北京天气"}],
            tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
        )
        chunks = [c async for c in main.anthropic_stream(request, "msg_1")]
        events = _sse_events(chunks)

        # 文本块（index 0）与 tool_use 块（index 1）
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert starts[0]["content_block"]["type"] == "text"
        assert starts[0]["index"] == 0
        assert starts[1]["content_block"]["type"] == "tool_use"
        assert starts[1]["index"] == 1
        assert starts[1]["content_block"]["id"] == "t1"
        assert starts[1]["content_block"]["name"] == "get_weather"

        # input_json_delta 分片拼接为完整 JSON
        input_deltas = [
            e["delta"]["partial_json"]
            for e in events
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "input_json_delta"
        ]
        assert json.loads("".join(input_deltas)) == {"city": "北京"}

        # 两个块都被关闭
        stops = [e for e in events if e["type"] == "content_block_stop"]
        assert {s["index"] for s in stops} == {0, 1}

        # stop_reason = tool_use
        delta = next(e for e in events if e["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "tool_use"

    async def test_pure_text_keeps_end_turn(self, monkeypatch):
        async def fake_events(*args, **kwargs):
            yield GenerationEvent(EventType.TEXT_DELTA, text="hi")
            yield GenerationEvent(EventType.DONE)

        monkeypatch.setattr(main, "_events", fake_events)
        request = main.AnthropicRequest(
            model="auto", messages=[{"role": "user", "content": "hi"}]
        )
        chunks = [c async for c in main.anthropic_stream(request, "msg_2")]
        events = _sse_events(chunks)
        delta = next(e for e in events if e["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "end_turn"


class TestChatStreamTools:
    async def test_tool_calls_and_finish_reason(self, monkeypatch):
        async def fake_events(*args, **kwargs):
            for event in _tool_fragments():
                yield event

        monkeypatch.setattr(main, "_events", fake_events)
        chunks = [
            c
            async for c in main.chat_stream(
                "auto", "prompt", "chatcmpl-1", 0, tools=[{"type": "function"}]
            )
        ]
        events = _sse_events(chunks)

        # 起始分片带 id/name
        tool_starts = [
            tc
            for e in events
            for tc in e["choices"][0]["delta"].get("tool_calls", [])
            if "id" in tc
        ]
        assert tool_starts[0]["id"] == "t1"
        assert tool_starts[0]["function"]["name"] == "get_weather"
        assert tool_starts[0]["index"] == 0

        # arguments 分片拼接
        args = "".join(
            tc["function"]["arguments"]
            for e in events
            for tc in e["choices"][0]["delta"].get("tool_calls", [])
            if "arguments" in tc.get("function", {})
        )
        assert json.loads(args) == {"city": "北京"}

        # finish_reason = tool_calls
        finishes = [
            e["choices"][0]["finish_reason"]
            for e in events
            if e["choices"] and e["choices"][0]["finish_reason"]
        ]
        assert finishes == ["tool_calls"]


class TestResponsesStreamTools:
    async def test_function_call_output_items(self, monkeypatch):
        async def fake_events(*args, **kwargs):
            for event in _tool_fragments():
                yield event

        monkeypatch.setattr(main, "_events", fake_events)
        request = main.ResponsesRequest(model="auto", input="北京天气")
        chunks = [c async for c in main.responses_stream(request)]
        events = _sse_events(chunks)

        added = [
            e
            for e in events
            if e["type"] == "response.output_item.added"
            and e["item"]["type"] == "function_call"
        ]
        assert added[0]["item"]["call_id"] == "t1"
        assert added[0]["item"]["name"] == "get_weather"
        assert added[0]["output_index"] == 1

        done = next(
            e
            for e in events
            if e["type"] == "response.function_call_arguments.done"
        )
        assert json.loads(done["arguments"]) == {"city": "北京"}


class TestCollectGenerationTools:
    async def test_returns_tool_calls(self, monkeypatch):
        async def fake_events(*args, **kwargs):
            for event in _tool_fragments():
                yield event

        monkeypatch.setattr(main, "_events", fake_events)
        content, usage, tool_calls = await main._collect_generation(
            "auto", "prompt", None
        )
        assert content == "让我查一下。"
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "t1"
        assert tool_calls[0]["name"] == "get_weather"
        assert json.loads(tool_calls[0]["input"]) == {"city": "北京"}
