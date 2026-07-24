"""RuntimeTransport 集成测试（mock 上游 HTTP）。"""

from __future__ import annotations

import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kiro_api_proxy.event_stream import _crc32c
from kiro_api_proxy.transports.base import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)
from kiro_api_proxy.transports.router import AdaptiveTransport
from kiro_api_proxy.transports.runtime import RuntimeTransport


# ============================================================
# 帧构造辅助
# ============================================================


def _build_string_header(name: str, value: str) -> bytes:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return (
        struct.pack("B", len(name_bytes))
        + name_bytes
        + struct.pack("B", 7)
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def _build_frame(headers: dict[str, str], payload: bytes) -> bytes:
    headers_data = b"".join(
        _build_string_header(k, v) for k, v in headers.items()
    )
    headers_length = len(headers_data)
    total_length = 12 + headers_length + len(payload) + 4

    prelude_bytes = struct.pack(">II", total_length, headers_length)
    prelude_crc = _crc32c(prelude_bytes) & 0xFFFFFFFF
    prelude = prelude_bytes + struct.pack(">I", prelude_crc)

    message_body = prelude + headers_data + payload
    message_crc = _crc32c(message_body) & 0xFFFFFFFF

    return message_body + struct.pack(">I", message_crc)


def _text_frame(text: str) -> bytes:
    return _build_frame(
        {":event-type": "assistantResponseEvent", ":message-type": "event"},
        json.dumps({"content": text}).encode(),
    )


def _done_frame() -> bytes:
    return _build_frame(
        {":event-type": "conversationTurnComplete", ":message-type": "event"},
        b"{}",
    )


def _tool_frame(payload: dict) -> bytes:
    return _build_frame(
        {":event-type": "toolUseEvent", ":message-type": "event"},
        json.dumps(payload).encode(),
    )


# ============================================================
# Fixtures
# ============================================================


def _make_settings(**overrides):
    defaults = {
        "runtime_enabled": True,
        "runtime_credentials_file": "/tmp/fake-creds.json",
        "runtime_endpoint": "https://test.example.com",
        "timeout_seconds": 60,
        "transport_priority": ("runtime", "cli"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_runtime_transport(settings=None):
    """创建已初始化状态的 RuntimeTransport（跳过 start）。"""
    if settings is None:
        settings = _make_settings()
    rt = RuntimeTransport(settings)
    # 模拟已初始化状态
    rt._endpoint = "https://test.example.com"
    rt._profile_arn = "arn:aws:codewhisperer:us-east-1:123:profile/PROF1"
    rt._token_provider = MagicMock()
    rt._token_provider.get_token = AsyncMock(return_value="test-token")
    rt._token_provider.force_refresh = AsyncMock(return_value="refreshed-token")
    rt._client = httpx.AsyncClient()
    return rt


# ============================================================
# 测试：成功流式生成
# ============================================================


class TestRuntimeStreamSuccess:
    async def test_stream_success(self):
        rt = _make_runtime_transport()
        frames = _text_frame("Hello ") + _text_frame("world") + _done_frame()

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            events = [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["Hello ", "world"]
        assert any(e.type is EventType.DONE for e in events)
        call = mock_stream.call_args
        assert call.args[1].endswith("/generateAssistantResponse")
        payload = call.kwargs["json"]
        assert payload["conversationState"]["currentMessage"][
            "userInputMessage"
        ]["content"] == "hi"
        assert payload["conversationState"]["agentTaskType"] == "vibe"

    async def test_generate_collects_stream(self):
        rt = _make_runtime_transport()
        frames = _text_frame("part1") + _text_frame("part2") + _done_frame()

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            result = await rt.generate(GenerationRequest("auto", "hi"))

        assert result == "part1part2"


# ============================================================
# 测试：结构化工具契约
# ============================================================


class TestRuntimeTools:
    async def test_request_body_includes_tools_and_results(self):
        rt = _make_runtime_transport()
        frames = _text_frame("ok") + _done_frame()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        tools = [
            {
                "toolSpecification": {
                    "name": "get_weather",
                    "description": "查询天气",
                    "inputSchema": {"json": {"type": "object", "properties": {}}},
                }
            }
        ]
        tool_results = [
            {"toolUseId": "t1", "content": [{"text": "晴"}], "status": "success"}
        ]
        request = GenerationRequest(
            "auto",
            "hi",
            tools=tools,
            tool_results=tool_results,
            history=[
                {
                    "userInputMessage": {
                        "content": "天气？",
                        "origin": "AI_EDITOR",
                    }
                },
                {
                    "assistantResponseMessage": {
                        "content": "",
                        "toolUses": [
                            {
                                "toolUseId": "t1",
                                "name": "get_weather",
                                "input": {"city": "北京"},
                            }
                        ],
                    }
                },
            ],
        )

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            _ = [e async for e in rt.stream(request)]

        context = mock_stream.call_args.kwargs["json"]["conversationState"][
            "currentMessage"
        ]["userInputMessage"]["userInputMessageContext"]
        assert context["tools"] == tools
        assert context["toolResults"] == tool_results
        history = mock_stream.call_args.kwargs["json"]["conversationState"]["history"]
        assert history[-1]["assistantResponseMessage"]["toolUses"][0][
            "toolUseId"
        ] == "t1"
        assert history[0]["userInputMessage"]["modelId"] == "auto"

    async def test_request_body_omits_context_without_tools(self):
        rt = _make_runtime_transport()
        frames = _text_frame("ok") + _done_frame()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            _ = [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        user_message = mock_stream.call_args.kwargs["json"]["conversationState"][
            "currentMessage"
        ]["userInputMessage"]
        assert "userInputMessageContext" not in user_message

    async def test_tool_use_event_decoded_as_tool(self):
        rt = _make_runtime_transport()
        frames = (
            _tool_frame({"name": "get_weather", "toolUseId": "t1"})
            + _tool_frame({"input": '{"city":', "name": "get_weather", "toolUseId": "t1"})
            + _tool_frame({"input": ' "北京"}', "name": "get_weather", "toolUseId": "t1"})
            + _tool_frame({"name": "get_weather", "stop": True, "toolUseId": "t1"})
            + _done_frame()
        )
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            events = [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        tool_events = [e for e in events if e.type is EventType.TOOL]
        assert len(tool_events) == 4
        assert all(e.data["id"] == "t1" for e in tool_events)
        combined = "".join(e.data["input"] for e in tool_events)
        assert json.loads(combined) == {"city": "北京"}
        assert tool_events[-1].data["stop"] is True


# ============================================================
# 测试：401 刷新重放
# ============================================================


class TestRuntime401Replay:
    async def test_401_then_success(self):
        rt = _make_runtime_transport()
        frames = _text_frame("OK") + _done_frame()

        # 第一次 401，第二次成功
        mock_resp_401 = AsyncMock()
        mock_resp_401.status_code = 401

        mock_resp_200 = AsyncMock()
        mock_resp_200.status_code = 200
        mock_resp_200.aiter_bytes = lambda: _async_iter([frames])

        call_count = 0

        class StreamCtx:
            def __init__(self, resp):
                self.resp = resp

            async def __aenter__(self):
                return self.resp

            async def __aexit__(self, *args):
                pass

        def stream_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return StreamCtx(mock_resp_401)
            return StreamCtx(mock_resp_200)

        with patch.object(rt._client, "stream", side_effect=stream_side_effect):
            events = [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        assert call_count == 2
        rt._token_provider.force_refresh.assert_called_once()
        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["OK"]


# ============================================================
# 测试：流后失败（已输出内容后出错）
# ============================================================


class TestRuntimeStreamFailAfterOutput:
    async def test_error_event_after_partial_output(self):
        rt = _make_runtime_transport()
        # 只有文本帧，没有 DONE 帧，流结束
        frames = _text_frame("partial")

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            events = [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        # 应该有文本但没有 DONE（正常——流自然结束）
        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["partial"]


# ============================================================
# 测试：models 降级
# ============================================================


class FakeCli:
    name = "cli"

    async def start(self):
        pass

    async def close(self):
        pass

    async def models(self):
        return [{"model_id": "auto", "name": "auto", "provider": "kiro"}]

    async def generate(self, request):
        return "cli-result"

    async def stream(self, request):
        yield GenerationEvent(EventType.TEXT_DELTA, text="cli")
        yield GenerationEvent(EventType.DONE)

    async def cancel(self, request_id):
        pass


class TestModelsDegradation:
    async def test_runtime_models_fails_degrades_to_cli(self):
        rt = _make_runtime_transport()
        cli = FakeCli()

        # RuntimeTransport.models() 抛异常
        with patch.object(rt, "models", side_effect=TransportError(
            "网络错误", ErrorCategory.UPSTREAM, retryable=True
        )):
            router = AdaptiveTransport([rt, cli])
            result = await router.models()

        assert result == [{"model_id": "auto", "name": "auto", "provider": "kiro"}]

    async def test_runtime_models_success(self):
        rt = _make_runtime_transport()
        cli = FakeCli()

        with patch.object(rt, "models", return_value=[
            {"model_id": "claude-sonnet-5", "name": "Claude Sonnet 5", "provider": "kiro"}
        ]):
            router = AdaptiveTransport([rt, cli])
            result = await router.models()

        assert result[0]["model_id"] == "claude-sonnet-5"


# ============================================================
# 异步辅助
# ============================================================


async def _async_iter(items):
    for item in items:
        yield item


class _async_context:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *args):
        pass
