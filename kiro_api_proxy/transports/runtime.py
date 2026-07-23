from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..config import Settings
from .base import (
    ErrorCategory,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)


class RuntimeTransport:
    """直接 Runtime 实验通道的安全占位实现。

    Kiro 官方目前只公开 CLI/ACP 客户端协议，没有公开可供第三方调用的
    Runtime 请求契约。为了避免依赖逆向私有接口，本传输始终保持不可用，
    由自适应路由自动降级到 ACP/CLI。
    """

    name = "runtime"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def start(self) -> None:
        raise TransportError(
            "Kiro 尚未公开直接 Runtime API，实验通道未启用",
            ErrorCategory.PROTOCOL,
            retryable=True,
        )

    async def close(self) -> None:
        return None

    async def models(self) -> list[dict[str, Any]]:
        raise TransportError("Runtime 不支持模型发现", ErrorCategory.PROTOCOL)

    async def generate(self, request: GenerationRequest) -> str:
        raise TransportError(
            "直接 Runtime API 不受官方支持",
            ErrorCategory.PROTOCOL,
            retryable=True,
        )

    async def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        raise TransportError(
            "直接 Runtime API 不受官方支持",
            ErrorCategory.PROTOCOL,
            retryable=True,
        )
        yield

    async def cancel(self, request_id: str) -> None:
        return None
