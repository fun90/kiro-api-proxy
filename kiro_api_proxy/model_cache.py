from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    models: list[dict[str, Any]]
    loaded_at: float
    source: str


class ModelCache:
    def __init__(self, ttl_seconds: float, stale_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self.stale_seconds = stale_seconds
        self._snapshot: ModelSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> ModelSnapshot | None:
        return self._snapshot

    def invalidate(self) -> None:
        self._snapshot = None

    async def get(
        self,
        loader: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        force: bool = False,
    ) -> ModelSnapshot:
        now = time.monotonic()
        current = self._snapshot
        if not force and current and now - current.loaded_at < self.ttl_seconds:
            return ModelSnapshot(current.models, current.loaded_at, "cache")
        async with self._lock:
            now = time.monotonic()
            current = self._snapshot
            if not force and current and now - current.loaded_at < self.ttl_seconds:
                return ModelSnapshot(current.models, current.loaded_at, "cache")
            try:
                models = await loader()
            except Exception:
                if current and now - current.loaded_at < self.stale_seconds:
                    return ModelSnapshot(current.models, current.loaded_at, "stale")
                raise
            self._snapshot = ModelSnapshot(models, now, "upstream")
            return self._snapshot
