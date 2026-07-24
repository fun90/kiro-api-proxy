"""将 Kiro Runtime Event Stream 消息映射为 GenerationEvent。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from .event_stream import EventStreamMessage
from .transports.base import EventType, GenerationEvent

logger = logging.getLogger(__name__)

# Event Stream header 中标识事件类型的 key
EVENT_TYPE_HEADER = ":event-type"
EXCEPTION_TYPE_HEADER = ":exception-type"
MESSAGE_TYPE_HEADER = ":message-type"


def map_event(message: EventStreamMessage) -> Iterator[GenerationEvent]:
    """将单条 Event Stream 消息映射为 GenerationEvent。

    可能产生 0 或 1 个 GenerationEvent。使用 Iterator 以支持未来扩展。
    """
    msg_type = message.headers.get(MESSAGE_TYPE_HEADER, "event")

    # 异常帧
    if msg_type == "exception":
        exc_type = message.headers.get(EXCEPTION_TYPE_HEADER, "unknown")
        try:
            body = json.loads(message.payload) if message.payload else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        error_msg = body.get("message", body.get("Message", exc_type))
        yield GenerationEvent(
            type=EventType.ERROR,
            text=f"{exc_type}: {error_msg}",
            data={"exception_type": exc_type},
        )
        return

    # 普通事件帧
    event_type = message.headers.get(EVENT_TYPE_HEADER, "")

    # 文本增量
    if event_type in ("assistantResponseEvent", "contentBlockDelta"):
        try:
            body = json.loads(message.payload) if message.payload else {}
        except (json.JSONDecodeError, ValueError):
            return
        # assistantResponseEvent 直接含 content
        text = body.get("text", body.get("content", ""))
        # contentBlockDelta 有 delta.text 形式
        if not text and "delta" in body:
            text = body["delta"].get("text", "")
        if text:
            yield GenerationEvent(type=EventType.TEXT_DELTA, text=text)
        return

    # 思考增量
    if event_type in ("reasoningContentEvent", "reasoningBlockDelta"):
        try:
            body = json.loads(message.payload) if message.payload else {}
        except (json.JSONDecodeError, ValueError):
            return
        text = body.get("content", "")
        if not text and "delta" in body:
            text = body["delta"].get("thinking", body["delta"].get("text", ""))
        if text:
            yield GenerationEvent(type=EventType.THINKING_DELTA, text=text)
        return

    # 工具使用 → 结构化 TOOL 事件（分片：起始 / input 片段 / stop）
    if event_type in ("toolUseEvent", "toolUseBlockStart", "toolUseBlockDelta"):
        try:
            body = json.loads(message.payload) if message.payload else {}
        except (json.JSONDecodeError, ValueError):
            return
        tool_id = body.get("toolUseId", body.get("id", ""))
        if not tool_id:
            return
        tool_input = body.get("input", "")
        # input 可能是字符串片段或结构化对象；统一为 JSON 文本片段拼接
        if isinstance(tool_input, (dict, list)):
            tool_input = json.dumps(tool_input, ensure_ascii=False)
        elif tool_input is None:
            tool_input = ""
        yield GenerationEvent(
            type=EventType.TOOL,
            data={
                "id": tool_id,
                "name": body.get("name", body.get("toolName", "")),
                "input": str(tool_input),
                "stop": bool(body.get("stop", False)),
            },
        )
        return

    # 用量/计量事件
    if event_type in (
        "usageEvent",
        "meteringEvent",
        "contextUsageEvent",
        "contentBlockStop",
        "messageStop",
    ):
        try:
            body = json.loads(message.payload) if message.payload else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        # 提取 token 用量信息
        usage_data: dict = {}
        if "usage" in body:
            usage_data = body["usage"]
        elif "inputTokens" in body or "outputTokens" in body:
            usage_data = body
        if isinstance(usage_data, dict) and usage_data:
            yield GenerationEvent(
                type=EventType.USAGE,
                data={
                    "input_tokens": usage_data.get("inputTokens", 0),
                    "output_tokens": usage_data.get("outputTokens", 0),
                },
            )
        elif isinstance(usage_data, (int, float)):
            yield GenerationEvent(
                type=EventType.USAGE,
                data={"credits": float(usage_data)},
            )
        elif "used" in body or "size" in body:
            # contextUsageEvent：上下文占用（used）与窗口大小（size）
            yield GenerationEvent(
                type=EventType.USAGE,
                data={
                    "context_tokens": body.get("used", 0),
                    "context_window": body.get("size", 0),
                },
            )
        return

    # 补全/结束事件
    if event_type in (
        "completionEvent",
        "messageComplete",
        "conversationTurnComplete",
    ):
        yield GenerationEvent(type=EventType.DONE)
        return

    # 未知事件类型 — 忽略并记录
    if event_type:
        logger.debug("忽略未知 Runtime 事件类型: %s", event_type)
