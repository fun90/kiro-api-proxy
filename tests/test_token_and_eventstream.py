"""Token 刷新和 Event Stream 解码器单元测试。"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_api_proxy.event_mapper import map_event
from kiro_api_proxy.event_stream import (
    EventStreamDecoder,
    EventStreamError,
    EventStreamMessage,
    _crc32c,
)
from kiro_api_proxy.runtime_credentials import RuntimeCredentials
from kiro_api_proxy.token_provider import (
    REFRESH_BUFFER_SECONDS,
    TokenProvider,
    TokenRefreshError,
)


# ============================================================
# 辅助函数：构造 AWS Event Stream 二进制帧
# ============================================================


def _build_string_header(name: str, value: str) -> bytes:
    """构建字符串类型的 Event Stream header。"""
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return (
        struct.pack("B", len(name_bytes))
        + name_bytes
        + struct.pack("B", 7)  # string type
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def _build_frame(headers: dict[str, str], payload: bytes) -> bytes:
    """构建完整的 Event Stream 二进制帧。"""
    headers_data = b"".join(
        _build_string_header(k, v) for k, v in headers.items()
    )
    headers_length = len(headers_data)
    total_length = 12 + headers_length + len(payload) + 4  # prelude + headers + payload + msg_crc

    # Prelude: total_length + headers_length
    prelude_bytes = struct.pack(">II", total_length, headers_length)
    prelude_crc = _crc32c(prelude_bytes) & 0xFFFFFFFF
    prelude = prelude_bytes + struct.pack(">I", prelude_crc)

    # Message body (without message CRC)
    message_body = prelude + headers_data + payload
    message_crc = _crc32c(message_body) & 0xFFFFFFFF

    return message_body + struct.pack(">I", message_crc)


# ============================================================
# Event Stream Decoder 测试
# ============================================================


class TestEventStreamDecoder:
    def test_decode_single_frame(self):
        payload = b'{"content": "hello"}'
        headers = {":event-type": "assistantResponseEvent", ":message-type": "event"}
        frame = _build_frame(headers, payload)

        decoder = EventStreamDecoder()
        decoder.feed(frame)
        messages = decoder.drain()

        assert len(messages) == 1
        assert messages[0].headers[":event-type"] == "assistantResponseEvent"
        assert messages[0].payload == payload

    def test_decode_multiple_frames(self):
        frame1 = _build_frame(
            {":event-type": "assistantResponseEvent", ":message-type": "event"},
            b'{"content": "first"}',
        )
        frame2 = _build_frame(
            {":event-type": "conversationTurnComplete", ":message-type": "event"},
            b"{}",
        )

        decoder = EventStreamDecoder()
        decoder.feed(frame1 + frame2)
        messages = decoder.drain()

        assert len(messages) == 2
        assert messages[0].headers[":event-type"] == "assistantResponseEvent"
        assert messages[1].headers[":event-type"] == "conversationTurnComplete"

    def test_decode_incremental_feed(self):
        frame = _build_frame(
            {":event-type": "assistantResponseEvent", ":message-type": "event"},
            b'{"content": "chunk"}',
        )

        decoder = EventStreamDecoder()
        # 分块喂入
        mid = len(frame) // 2
        decoder.feed(frame[:mid])
        assert decoder.drain() == []
        decoder.feed(frame[mid:])
        messages = decoder.drain()
        assert len(messages) == 1

    def test_crc_mismatch_raises(self):
        frame = _build_frame(
            {":event-type": "test", ":message-type": "event"},
            b"payload",
        )
        # 篡改 payload 中的一个字节
        corrupted = bytearray(frame)
        corrupted[15] ^= 0xFF
        corrupted = bytes(corrupted)

        decoder = EventStreamDecoder()
        with pytest.raises(EventStreamError, match="CRC 校验失败"):
            decoder.feed(corrupted)

    def test_frame_size_exceeds_limit(self):
        decoder = EventStreamDecoder(max_frame_size=50)
        # 构造一个声称很大的 prelude
        big_length = 100
        prelude_bytes = struct.pack(">II", big_length, 0)
        prelude_crc = _crc32c(prelude_bytes) & 0xFFFFFFFF
        fake_prelude = prelude_bytes + struct.pack(">I", prelude_crc)

        with pytest.raises(EventStreamError, match="超过上限"):
            decoder.feed(fake_prelude + b"\x00" * 100)

    def test_empty_payload(self):
        frame = _build_frame(
            {":event-type": "conversationTurnComplete", ":message-type": "event"},
            b"",
        )
        decoder = EventStreamDecoder()
        decoder.feed(frame)
        messages = decoder.drain()
        assert len(messages) == 1
        assert messages[0].payload == b""

    def test_exception_frame(self):
        frame = _build_frame(
            {":exception-type": "validationException", ":message-type": "exception"},
            json.dumps({"message": "invalid model"}).encode(),
        )
        decoder = EventStreamDecoder()
        decoder.feed(frame)
        messages = decoder.drain()
        assert len(messages) == 1
        assert messages[0].headers[":message-type"] == "exception"


# ============================================================
# Event Mapper 测试
# ============================================================


class TestEventMapper:
    def test_text_delta(self):
        msg = EventStreamMessage(
            headers={":event-type": "assistantResponseEvent", ":message-type": "event"},
            payload=json.dumps({"content": "hello world"}).encode(),
        )
        events = list(map_event(msg))
        assert len(events) == 1
        assert events[0].type.value == "text_delta"
        assert events[0].text == "hello world"

    def test_thinking_delta(self):
        msg = EventStreamMessage(
            headers={":event-type": "reasoningContentEvent", ":message-type": "event"},
            payload=json.dumps({"content": "let me think..."}).encode(),
        )
        events = list(map_event(msg))
        assert len(events) == 1
        assert events[0].type.value == "thinking_delta"
        assert events[0].text == "let me think..."

    def test_tool_use_structured_event(self):
        # 起始分片：仅 name + toolUseId
        start = EventStreamMessage(
            headers={":event-type": "toolUseEvent", ":message-type": "event"},
            payload=json.dumps(
                {"name": "get_weather", "toolUseId": "tuse_1"}
            ).encode(),
        )
        events = list(map_event(start))
        assert len(events) == 1
        assert events[0].type.value == "tool"
        assert events[0].data == {
            "id": "tuse_1",
            "name": "get_weather",
            "input": "",
            "stop": False,
        }

        # input 分片
        delta = EventStreamMessage(
            headers={":event-type": "toolUseEvent", ":message-type": "event"},
            payload=json.dumps(
                {"input": '{"city": "北', "name": "get_weather", "toolUseId": "tuse_1"}
            ).encode(),
        )
        events = list(map_event(delta))
        assert events[0].type.value == "tool"
        assert events[0].data["input"] == '{"city": "北'
        assert events[0].data["stop"] is False

        # 结束分片
        stop = EventStreamMessage(
            headers={":event-type": "toolUseEvent", ":message-type": "event"},
            payload=json.dumps(
                {"name": "get_weather", "stop": True, "toolUseId": "tuse_1"}
            ).encode(),
        )
        events = list(map_event(stop))
        assert events[0].data["stop"] is True

    def test_tool_use_object_input_serialized(self):
        msg = EventStreamMessage(
            headers={":event-type": "toolUseEvent", ":message-type": "event"},
            payload=json.dumps(
                {"name": "read_file", "input": {"path": "/foo"}, "toolUseId": "t1"}
            ).encode(),
        )
        events = list(map_event(msg))
        assert events[0].type.value == "tool"
        assert json.loads(events[0].data["input"]) == {"path": "/foo"}

    def test_usage_event(self):
        msg = EventStreamMessage(
            headers={":event-type": "usageEvent", ":message-type": "event"},
            payload=json.dumps({"usage": {"inputTokens": 100, "outputTokens": 50}}).encode(),
        )
        events = list(map_event(msg))
        assert len(events) == 1
        assert events[0].type.value == "usage"
        assert events[0].data["input_tokens"] == 100
        assert events[0].data["output_tokens"] == 50

    def test_usage_event_merges_context_and_cache_fields(self):
        msg = EventStreamMessage(
            headers={":event-type": "usageEvent", ":message-type": "event"},
            payload=json.dumps(
                {
                    "usage": {
                        "inputTokens": 4100,
                        "outputTokens": 50,
                        "cachedReadTokens": 3900,
                        "cachedWriteTokens": 20,
                        "thoughtTokens": 10,
                    },
                    "used": 4096,
                    "size": 200000,
                }
            ).encode(),
        )
        events = list(map_event(msg))
        assert events[0].data == {
            "input_tokens": 4100,
            "output_tokens": 50,
            "cache_read_input_tokens": 3900,
            "cache_creation_input_tokens": 20,
            "reasoning_tokens": 10,
            "context_tokens": 4096,
            "context_window": 200000,
        }

    def test_usage_event_does_not_invent_missing_zero_values(self):
        msg = EventStreamMessage(
            headers={
                ":event-type": "contextUsageEvent",
                ":message-type": "event",
            },
            payload=json.dumps({"used": 4096, "size": 200000}).encode(),
        )
        events = list(map_event(msg))
        assert events[0].data == {
            "context_tokens": 4096,
            "context_window": 200000,
        }

    def test_completion_event(self):
        msg = EventStreamMessage(
            headers={":event-type": "conversationTurnComplete", ":message-type": "event"},
            payload=b"{}",
        )
        events = list(map_event(msg))
        assert len(events) == 1
        assert events[0].type.value == "done"

    def test_exception_event(self):
        msg = EventStreamMessage(
            headers={":exception-type": "throttlingException", ":message-type": "exception"},
            payload=json.dumps({"message": "rate limited"}).encode(),
        )
        events = list(map_event(msg))
        assert len(events) == 1
        assert events[0].type.value == "error"
        assert "throttlingException" in events[0].text

    def test_unknown_event_ignored(self):
        msg = EventStreamMessage(
            headers={":event-type": "unknownFutureEvent", ":message-type": "event"},
            payload=b'{"foo": "bar"}',
        )
        events = list(map_event(msg))
        assert len(events) == 0


# ============================================================
# Token Provider 测试
# ============================================================


def _make_credentials(access_token: str = "", expires_at: float = 0) -> RuntimeCredentials:
    return RuntimeCredentials(
        refresh_token="rt-test",
        client_id="client-id",
        client_secret="client-secret",
        auth_region="us-east-1",
        profile_arn="arn:aws:codewhisperer:us-east-1:123:profile/P",
        access_token=access_token,
        expires_at=expires_at,
    )


class TestTokenProvider:
    async def test_returns_valid_token_without_refresh(self, tmp_path: Path):
        creds = _make_credentials("valid-token", time.time() + 3600)
        provider = TokenProvider(creds, str(tmp_path / "creds.json"))
        token = await provider.get_token()
        assert token == "valid-token"

    async def test_expired_token_triggers_refresh(self, tmp_path: Path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("{}")
        creds = _make_credentials("", 0)
        provider = TokenProvider(creds, str(creds_file))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-token-abc",
            "expires_in": 3600,
            "refresh_token": "new-rt",
        }

        with patch("kiro_api_proxy.token_provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            token = await provider.get_token()

        assert token == "new-token-abc"
        call = mock_client.post.call_args
        assert call.kwargs["json"] == {
            "grantType": "refresh_token",
            "clientId": "client-id",
            "clientSecret": "client-secret",
            "refreshToken": "rt-test",
        }
        assert provider.access_token == "new-token-abc"
        # 验证文件回写
        written = json.loads(creds_file.read_text())
        assert written["access_token"] == "new-token-abc"
        assert written["refresh_token"] == "new-rt"

    async def test_concurrent_refresh_single_flight(self, tmp_path: Path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("{}")
        creds = _make_credentials("", 0)
        provider = TokenProvider(creds, str(creds_file))

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # 模拟网络延迟
            response = MagicMock()
            response.status_code = 200
            response.raise_for_status = MagicMock()
            response.json.return_value = {
                "access_token": "shared-token",
                "expires_in": 3600,
            }
            return response

        with patch("kiro_api_proxy.token_provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # 5 个并发请求
            results = await asyncio.gather(
                *(provider.get_token() for _ in range(5))
            )

        # 所有请求应该获得相同的 token
        assert all(r == "shared-token" for r in results)
        # 只应该有一次 HTTP 请求
        assert call_count == 1

    async def test_refresh_failure_raises(self, tmp_path: Path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("{}")
        creds = _make_credentials("", 0)
        provider = TokenProvider(creds, str(creds_file))

        with patch("kiro_api_proxy.token_provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            import httpx

            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(TokenRefreshError, match="网络错误"):
                await provider.get_token()

    async def test_force_refresh_ignores_valid_token(self, tmp_path: Path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("{}")
        creds = _make_credentials("old-token", time.time() + 3600)
        provider = TokenProvider(creds, str(creds_file))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "access_token": "forced-new",
            "expires_in": 3600,
        }

        with patch("kiro_api_proxy.token_provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            token = await provider.force_refresh()

        assert token == "forced-new"

    def test_is_expired_with_buffer(self):
        creds = _make_credentials("token", time.time() + REFRESH_BUFFER_SECONDS - 1)
        provider = TokenProvider(creds, "/dev/null")
        assert provider.is_expired() is True

    def test_is_expired_empty_token(self):
        creds = _make_credentials("", time.time() + 9999)
        provider = TokenProvider(creds, "/dev/null")
        assert provider.is_expired() is True
