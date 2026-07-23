from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass(slots=True)
class SessionRecord:
    key: str
    worker_id: str
    upstream_session_id: str
    turn_count: int = 0
    last_prompt_chars: int = 0
    upstream_context_chars: int = 0
    last_used: float = field(default_factory=time.monotonic)
    rebuilt: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._records: OrderedDict[str, SessionRecord] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> SessionRecord | None:
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            if time.monotonic() - record.last_used >= self.ttl_seconds:
                del self._records[key]
                return None
            record.last_used = time.monotonic()
            self._records.move_to_end(key)
            return record

    async def put(self, record: SessionRecord) -> None:
        async with self._lock:
            record.last_used = time.monotonic()
            self._records[record.key] = record
            self._records.move_to_end(record.key)
            while len(self._records) > self.max_entries:
                self._records.popitem(last=False)

    async def remove_worker(self, worker_id: str) -> list[SessionRecord]:
        async with self._lock:
            removed = [
                record
                for record in self._records.values()
                if record.worker_id == worker_id
            ]
            for record in removed:
                self._records.pop(record.key, None)
            return removed

    async def orphan_worker(self, worker_id: str) -> list[SessionRecord]:
        async with self._lock:
            affected = [
                record
                for record in self._records.values()
                if record.worker_id == worker_id
            ]
            for record in affected:
                record.worker_id = ""
                record.upstream_session_id = ""
                record.turn_count = 0
                record.upstream_context_chars = 0
                record.rebuilt = True
            return affected

    async def cleanup(self) -> int:
        async with self._lock:
            now = time.monotonic()
            expired = [
                key
                for key, record in self._records.items()
                if now - record.last_used >= self.ttl_seconds
            ]
            for key in expired:
                del self._records[key]
            return len(expired)

    def __len__(self) -> int:
        return len(self._records)
