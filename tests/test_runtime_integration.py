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
    EventType,
    GenerationRequest,
    TransportError,
)
from kiro_api_proxy.transports.runtime import (
    RuntimeTransport,
    _context_window_tokens,
)


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
        "runtime_credentials_file": "/tmp/fake-creds.json",
        "runtime_endpoint": "https://test.example.com",
        "timeout_seconds": 60,
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


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ({"contextWindowTokens": 200000}, 200000),
        ({"tokenLimits": {"maxInputTokens": 128000}}, 128000),
        ({"modelId": "auto"}, None),
    ],
)
def test_context_window_tokens_normalizes_model_metadata(model, expected):
    assert _context_window_tokens(model) == expected


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
# 测试：建连失败重放（空闲后连接池死连接自愈）
# ============================================================


class TestRuntimeConnectRetry:
    async def test_connect_error_then_success(self):
        """建连阶段 ConnectError（典型为空闲后 keepalive 死连接）应重放一次。"""
        rt = _make_runtime_transport()
        frames = _text_frame("OK") + _done_frame()

        mock_resp_200 = AsyncMock()
        mock_resp_200.status_code = 200
        mock_resp_200.aiter_bytes = lambda: _async_iter([frames])

        call_count = 0

        class ConnCtx:
            async def __aenter__(self):
                raise httpx.ConnectError("connection reset")

            async def __aexit__(self, *args):
                return False

        class StreamCtx:
            def __init__(self, resp):
                self.resp = resp

            async def __aenter__(self):
                return self.resp

            async def __aexit__(self, *args):
                return False

        def stream_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ConnCtx()
            return StreamCtx(mock_resp_200)

        with patch.object(rt._client, "stream", side_effect=stream_side_effect):
            events = [
                e async for e in rt.stream(GenerationRequest("auto", "hi"))
            ]

        # 重放一次后成功，且不触发 Token 刷新。
        assert call_count == 2
        rt._token_provider.force_refresh.assert_not_called()
        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["OK"]

    async def test_connect_error_not_replayed_after_output(self):
        """已向下游产出内容后再发生连接错误不重放，避免重复输出。"""
        rt = _make_runtime_transport()

        async def _iter_then_error(frames):
            yield frames
            raise httpx.ConnectError("mid-stream drop")

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _iter_then_error(_text_frame("partial"))

        call_count = 0

        class StreamCtx:
            def __init__(self, resp):
                self.resp = resp

            async def __aenter__(self):
                return self.resp

            async def __aexit__(self, *args):
                return False

        def stream_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return StreamCtx(mock_resp)

        events = []
        with patch.object(rt._client, "stream", side_effect=stream_side_effect):
            with pytest.raises(TransportError):
                async for e in rt.stream(GenerationRequest("auto", "hi")):
                    events.append(e)

        # 未重放，且已产出的文本仍送达。
        assert call_count == 1
        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["partial"]

    async def test_ssl_error_mid_stream_becomes_transport_error(self):
        """流式读取中途的裸 OSError（如 ssl.SSLError）应转为 TransportError。

        ssl.SSLError 不是 httpx.HTTPError 的子类，若不显式捕获会直接穿透
        StreamingResponse 导致 ASGI 500，而不是转成干净的 SSE ERROR 事件。
        """
        rt = _make_runtime_transport()

        async def _iter_then_ssl_error(frames):
            yield frames
            import ssl

            raise ssl.SSLError("record layer failure")

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _iter_then_ssl_error(_text_frame("partial"))

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            events = []
            with pytest.raises(TransportError):
                async for e in rt.stream(GenerationRequest("auto", "hi")):
                    events.append(e)

        texts = [e.text for e in events if e.type is EventType.TEXT_DELTA]
        assert texts == ["partial"]


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
# 测试：上游不下发用量帧
# ============================================================


class TestRuntimeStreamNoUsage:
    async def test_stream_without_usage_frame_emits_no_usage_event(self):
        """上游只发文本/结束帧、无 usageEvent/contextUsageEvent 时，
        Runtime 流不应凭空合成 USAGE 事件。

        这条路径下客户端拿不到真实上下文占用，只能由 ensure_estimates 退回
        字符级估算（偏保守、通常低于真实 tokenizer）。锁定该行为，既固定
        当前偏低成因的边界，也避免未来误以为 Runtime 会自带用量统计。
        """
        rt = _make_runtime_transport()
        frames = _text_frame("你好") + _text_frame("世界") + _done_frame()

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.aiter_bytes = lambda: _async_iter([frames])

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            events = [
                e async for e in rt.stream(GenerationRequest("auto", "hi"))
            ]

        assert [e for e in events if e.type is EventType.USAGE] == []
        assert any(e.type is EventType.DONE for e in events)


# ============================================================
# 测试：4xx 携带上游返回的被拒原因
# ============================================================


class TestRuntimeErrorDetail:
    async def test_400_includes_upstream_message(self):
        """400 的 body 写明被拒原因，必须带进错误信息用于定位。"""
        rt = _make_runtime_transport()

        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.aread = AsyncMock(
            return_value=json.dumps(
                {
                    "__type": "ValidationException",
                    "message": "history[1].assistantResponseMessage.content "
                    "must not be empty",
                }
            ).encode()
        )

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            with pytest.raises(TransportError) as excinfo:
                [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        message = str(excinfo.value)
        assert "HTTP 400" in message
        assert "must not be empty" in message

    async def test_400_with_non_json_body_falls_back_to_raw_text(self):
        rt = _make_runtime_transport()

        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.aread = AsyncMock(return_value=b"Bad Request: malformed history")

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            with pytest.raises(TransportError) as excinfo:
                [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        assert "malformed history" in str(excinfo.value)

    async def test_400_with_empty_body_keeps_status_only(self):
        """body 为空时不能拼出尾随冒号。"""
        rt = _make_runtime_transport()

        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.aread = AsyncMock(return_value=b"")

        with patch.object(rt._client, "stream") as mock_stream:
            mock_stream.return_value = _async_context(mock_resp)
            with pytest.raises(TransportError) as excinfo:
                [e async for e in rt.stream(GenerationRequest("auto", "hi"))]

        assert str(excinfo.value) == "Runtime 请求错误: HTTP 400"


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
