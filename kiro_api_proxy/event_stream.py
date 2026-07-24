"""AWS Binary Event Stream 增量解码器。

协议格式:
  [total_length:4][headers_length:4][prelude_crc:4]
  [headers:headers_length]
  [payload:total_length - headers_length - 16]
  [message_crc:4]

参考: https://docs.aws.amazon.com/transcribe/latest/dg/event-stream.html
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

# Prelude: 4 (total_length) + 4 (headers_length) + 4 (prelude_crc)
PRELUDE_SIZE = 12
# Message CRC 尾部大小
MESSAGE_CRC_SIZE = 4
# 默认帧大小上限 16 MB
MAX_FRAME_SIZE = 16 * 1024 * 1024


class EventStreamError(Exception):
    """Event Stream 协议错误。"""


@dataclass(slots=True)
class EventStreamMessage:
    """解码后的单条事件消息。"""

    headers: dict[str, str]
    payload: bytes


def _crc32c(data: bytes, crc: int = 0) -> int:
    """计算 AWS Event Stream 使用的 IEEE CRC-32。

    保留旧函数名以兼容现有测试和外部导入。
    """
    return zlib.crc32(data, crc)


def _verify_crc(data: bytes, expected: int, context: str) -> None:
    """校验 CRC，不匹配时抛异常。"""
    actual = _crc32c(data) & 0xFFFFFFFF
    if actual != (expected & 0xFFFFFFFF):
        raise EventStreamError(
            f"{context} CRC 校验失败: 期望 {expected:#010x}, 实际 {actual:#010x}"
        )


def _decode_headers(data: bytes) -> dict[str, str]:
    """解析 Event Stream headers。"""
    headers: dict[str, str] = {}
    offset = 0
    while offset < len(data):
        # Header name length (1 byte)
        name_len = data[offset]
        offset += 1
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        # Header type (1 byte): 7 = string
        header_type = data[offset]
        offset += 1
        if header_type == 7:  # String
            value_len = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            value = data[offset : offset + value_len].decode("utf-8")
            offset += value_len
            headers[name] = value
        else:
            # 跳过非字符串类型（简化实现，event stream 实际只使用 string headers）
            # 其他类型: 0=bool_true, 1=bool_false, 2=byte, 3=short, 4=int, 5=long, 6=bytes, 7=string, 8=timestamp, 9=uuid
            if header_type in (0, 1):
                pass  # 无值
            elif header_type == 2:
                offset += 1
            elif header_type == 3:
                offset += 2
            elif header_type == 4:
                offset += 4
            elif header_type in (5, 8):
                offset += 8
            elif header_type == 9:
                offset += 16
            elif header_type == 6:
                value_len = struct.unpack(">H", data[offset : offset + 2])[0]
                offset += 2 + value_len
            else:
                raise EventStreamError(f"未知 header 类型: {header_type}")
    return headers


@dataclass(slots=True)
class EventStreamDecoder:
    """增量 Event Stream 解码器。

    将字节块逐步喂入 feed()，通过 drain() 获取解码完成的消息。
    """

    _buffer: bytearray = field(default_factory=bytearray)
    _messages: list[EventStreamMessage] = field(default_factory=list)
    max_frame_size: int = MAX_FRAME_SIZE

    def feed(self, data: bytes) -> None:
        """喂入新数据块。"""
        self._buffer.extend(data)
        self._try_decode()

    def drain(self) -> list[EventStreamMessage]:
        """取出已解码完成的消息。"""
        messages = self._messages
        self._messages = []
        return messages

    def _try_decode(self) -> None:
        """尝试从缓冲区中解码完整帧。"""
        while len(self._buffer) >= PRELUDE_SIZE:
            # 读取 prelude
            prelude = bytes(self._buffer[:PRELUDE_SIZE])
            total_length, headers_length, prelude_crc = struct.unpack(
                ">III", prelude
            )

            # 帧大小校验
            if total_length > self.max_frame_size:
                raise EventStreamError(
                    f"帧大小 {total_length} 超过上限 {self.max_frame_size}"
                )
            if total_length < PRELUDE_SIZE + MESSAGE_CRC_SIZE:
                raise EventStreamError(
                    f"帧大小 {total_length} 不合法（小于最小帧）"
                )

            # 校验 prelude CRC（覆盖前 8 字节）
            _verify_crc(prelude[:8], prelude_crc, "Prelude")

            # 等待完整帧
            if len(self._buffer) < total_length:
                return

            # 提取完整帧
            frame = bytes(self._buffer[:total_length])
            self._buffer = self._buffer[total_length:]

            # 校验 message CRC（覆盖帧前 total_length - 4 字节）
            message_crc = struct.unpack(">I", frame[-MESSAGE_CRC_SIZE:])[0]
            _verify_crc(frame[:-MESSAGE_CRC_SIZE], message_crc, "Message")

            # 解析 headers
            headers_start = PRELUDE_SIZE
            headers_end = headers_start + headers_length
            headers = _decode_headers(frame[headers_start:headers_end])

            # 解析 payload
            payload_end = total_length - MESSAGE_CRC_SIZE
            payload = frame[headers_end:payload_end]

            self._messages.append(EventStreamMessage(headers=headers, payload=payload))
