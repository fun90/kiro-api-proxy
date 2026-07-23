from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class EventType(StrEnum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
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
