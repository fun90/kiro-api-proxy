from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class EventType(StrEnum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    # TOOL 事件表示上游发起的一次原生工具调用（分片）。约定 data 结构：
    #   {
    #     "id": str,     # 工具调用 ID（对应 Kiro toolUseId），同一调用的分片一致
    #     "name": str,   # 工具名，起始分片必带，后续分片可省略
    #     "input": str,  # 本次 input 的 JSON 文本片段，按顺序拼接得到完整参数
    #     "stop": bool,  # True 表示该工具调用结束
    #   }
    # 出站层按 id 聚合分片，映射为 Anthropic tool_use / OpenAI tool_calls。
    TOOL = "tool"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    MODEL_NOT_FOUND = "model_not_found"
    TIMEOUT = "timeout"
    CAPACITY = "capacity"
    PROTOCOL = "protocol"
    UPSTREAM = "upstream"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class GenerationEvent:
    type: EventType
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GenerationRequest:
    model: str
    prompt: str
    effort: str = ""
    session_id: str | None = None
    working_directory: str | None = None
    # 结构化工具契约（默认空，CLI/ACP 传输忽略即可，向后兼容）。
    # tools: Kiro toolSpecification 列表，形如
    #   {"toolSpecification": {"name","description","inputSchema": {"json": {...}}}}
    # tool_results: 历史工具执行结果，形如
    #   {"toolUseId": str, "content": [...], "status": "success"|"error"}
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # Runtime 活动工具轮次的历史消息。最后一条 assistantResponseMessage
    # 必须包含与 tool_results 对应的 toolUses，否则上游会返回 HTTP 400。
    history: list[dict[str, Any]] = field(default_factory=list)


def kiro_environment(
    extra_paths: tuple[str, ...] = (),
    working_directory: str | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    user_home = Path.home()
    preferred_paths = [str(Path(item).expanduser()) for item in extra_paths]
    if working_directory:
        project = Path(working_directory)
        preferred_paths.extend(
            str(project / relative)
            for relative in (
                ".venv/bin",
                "venv/bin",
                "node_modules/.bin",
            )
        )
    preferred_paths.extend(
        str(user_home / relative)
        for relative in (
            ".local/bin",
            ".cargo/bin",
            ".npm-global/bin",
            ".local/share/pnpm",
            ".bun/bin",
            ".deno/bin",
            "go/bin",
        )
    )
    preferred_paths.extend(
        item
        for item in environment.get("PATH", "").split(os.pathsep)
        if item
    )
    path_items = list(dict.fromkeys(preferred_paths))
    environment.update(
        {
            "PATH": os.pathsep.join(path_items),
            "NO_COLOR": "1",
            "KIRO_LOG_NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    return environment


class TransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        category: ErrorCategory = ErrorCategory.UPSTREAM,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class KiroTransport(Protocol):
    name: str

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def models(self) -> list[dict[str, Any]]: ...

    async def generate(self, request: GenerationRequest) -> str: ...

    def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]: ...

    async def cancel(self, request_id: str) -> None: ...
