from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

from .base import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    KiroTransport,
    TransportError,
)


class AdaptiveTransport:
    name = "adaptive"

    def __init__(
        self,
        transports: list[KiroTransport],
        failure_threshold: int = 3,
        cooldown_seconds: float = 30,
    ) -> None:
        if not transports:
            raise ValueError("至少需要一个传输")
        self.transports = transports
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = {item.name: 0 for item in transports}
        self._open_until = {item.name: 0.0 for item in transports}
        self._actual_transport: ContextVar[str] = ContextVar(
            "actual_transport", default=self.name
        )

    @property
    def actual_name(self) -> str:
        return self._actual_transport.get()

    async def start(self) -> None:
        for index, item in enumerate(self.transports):
            try:
                await item.start()
            except Exception:
                self._failure(item)
                self._open_until[item.name] = (
                    time.monotonic() + self.cooldown_seconds
                )
                if index == len(self.transports) - 1:
                    raise

    async def close(self) -> None:
        await asyncio.gather(
            *(item.close() for item in reversed(self.transports)),
            return_exceptions=True,
        )

    def _available(self, item: KiroTransport) -> bool:
        return time.monotonic() >= self._open_until[item.name]

    def _success(self, item: KiroTransport) -> None:
        self._failures[item.name] = 0
        self._open_until[item.name] = 0

    def _failure(self, item: KiroTransport) -> None:
        self._failures[item.name] += 1
        if self._failures[item.name] >= self.failure_threshold:
            self._open_until[item.name] = time.monotonic() + self.cooldown_seconds

    async def models(self) -> list[dict[str, Any]]:
        # 模型发现始终使用最稳定的 CLI 传输，ACP 未定义模型列表方法。
        cli = next(
            (item for item in self.transports if item.name == "cli"),
            self.transports[-1],
        )
        return await cli.models()

    async def generate(self, request: GenerationRequest) -> str:
        last_error: TransportError | None = None
        for item in self.transports:
            if not self._available(item):
                continue
            try:
                result = await item.generate(request)
                self._actual_transport.set(item.name)
                self._success(item)
                return result
            except TransportError as exc:
                self._failure(item)
                last_error = exc
                if not exc.retryable and exc.category not in {
                    ErrorCategory.PROTOCOL,
                    ErrorCategory.CAPACITY,
                }:
                    raise
        raise last_error or TransportError("没有可用传输", ErrorCategory.CAPACITY)

    async def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        last_error: GenerationEvent | None = None
        for item in self.transports:
            if not self._available(item):
                continue
            emitted = False
            failed = False
            try:
                async for event in item.stream(request):
                    self._actual_transport.set(item.name)
                    event.data.setdefault("transport", item.name)
                    if event.type is EventType.ERROR:
                        last_error = event
                        failed = True
                        self._failure(item)
                        break
                    if event.type in {
                        EventType.TEXT_DELTA,
                        EventType.THINKING_DELTA,
                    }:
                        emitted = True
                    yield event
                if not failed:
                    self._success(item)
                    return
                if emitted:
                    yield last_error
                    return
            except (TransportError, ConnectionError) as exc:
                self._failure(item)
                last_error = GenerationEvent(
                    EventType.ERROR,
                    text=str(exc),
                    data={
                        "category": ErrorCategory.PROTOCOL.value,
                        "transport": item.name,
                    },
                )
                if emitted:
                    yield last_error
                    return
        yield last_error or GenerationEvent(
            EventType.ERROR,
            text="没有可用传输",
            data={"category": ErrorCategory.CAPACITY.value},
        )

    async def cancel(self, request_id: str) -> None:
        await asyncio.gather(
            *(item.cancel(request_id) for item in self.transports),
            return_exceptions=True,
        )
