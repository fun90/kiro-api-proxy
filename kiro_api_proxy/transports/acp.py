from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import ClientCapabilities, Implementation

from ..config import Settings
from ..sessions import SessionRecord, SessionStore
from .base import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)


def _event_from_update(update: Any) -> GenerationEvent | None:
    kind = getattr(update, "session_update", "")
    if kind in {"agent_message_chunk", "agent_thought_chunk"}:
        content = getattr(update, "content", None)
        text = getattr(content, "text", "") if content else ""
        event_type = (
            EventType.TEXT_DELTA
            if kind == "agent_message_chunk"
            else EventType.THINKING_DELTA
        )
        return GenerationEvent(event_type, text=text)
    if kind in {"tool_call", "tool_call_update"}:
        return GenerationEvent(
            EventType.TOOL,
            data=update.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    if kind == "usage_update":
        return GenerationEvent(
            EventType.USAGE,
            data=update.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    return None


class _AcpClient:
    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue[GenerationEvent]] = {}

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        event = _event_from_update(update)
        queue = self.queues.get(session_id)
        if queue and event:
            event.data.setdefault("upstream_received_at", time.perf_counter())
            await queue.put(event)

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **_: Any,
    ) -> dict[str, Any]:
        if not options:
            return {"outcome": {"outcome": "cancelled"}}
        preferred = next(
            (
                option
                for option in options
                if getattr(option, "kind", "") in {"allow_always", "allow_once"}
            ),
            options[0],
        )
        return {
            "outcome": {
                "outcome": "selected",
                "optionId": preferred.option_id,
            }
        }

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


class AcpWorker:
    def __init__(self, settings: Settings, model: str, effort: str) -> None:
        self.settings = settings
        self.model = model
        self.effort = effort
        self.id = uuid.uuid4().hex
        self.client = _AcpClient()
        self.agent: Any = None
        self.process: asyncio.subprocess.Process | None = None
        self.active = 0
        self.lock = asyncio.Lock()
        self._context: Any = None
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def healthy(self) -> bool:
        return bool(
            self.process is not None
            and self.process.returncode is None
            and self.agent is not None
        )

    async def start(self) -> None:
        arguments = ["acp", "--model", self.model, "--trust-all-tools"]
        if self.effort:
            arguments.extend(["--effort", self.effort])
        self._context = spawn_agent_process(
            self.client,
            self.settings.kiro_cli,
            *arguments,
            env={
                **os.environ,
                "NO_COLOR": "1",
                "KIRO_LOG_NO_COLOR": "1",
                "TERM": "dumb",
            },
            cwd=self.settings.working_directory,
        )
        self.agent, self.process = await self._context.__aenter__()
        await asyncio.wait_for(
            self.agent.initialize(
                PROTOCOL_VERSION,
                ClientCapabilities(),
                Implementation(name="kiro-api-proxy", version="1.1.0"),
            ),
            timeout=20,
        )
        if self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while await self.process.stderr.readline():
            # stderr 仅用于防止管道反压；不得把可能含凭证的原文写入日志。
            pass

    async def close(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

    async def new_session(self) -> str:
        result = await self.agent.new_session(
            self.settings.working_directory, mcp_servers=[]
        )
        return result.session_id

    async def prompt(
        self, session_id: str, prompt: str
    ) -> AsyncIterator[GenerationEvent]:
        queue: asyncio.Queue[GenerationEvent] = asyncio.Queue()
        self.client.queues[session_id] = queue

        async def execute() -> None:
            try:
                result = await self.agent.prompt(session_id, [text_block(prompt)])
                await queue.put(
                    GenerationEvent(
                        EventType.DONE,
                        data={"stop_reason": str(result.stop_reason)},
                    )
                )
            except Exception as exc:
                await queue.put(
                    GenerationEvent(
                        EventType.ERROR,
                        text=f"ACP 请求失败：{exc}",
                        data={"category": ErrorCategory.PROTOCOL.value},
                    )
                )

        task = asyncio.create_task(execute())
        try:
            while True:
                event = await asyncio.wait_for(
                    queue.get(), timeout=self.settings.timeout_seconds
                )
                yield event
                if event.type in {EventType.DONE, EventType.ERROR}:
                    break
        except asyncio.CancelledError:
            await self.agent.cancel(session_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            if not task.done():
                await self.agent.cancel(session_id)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.client.queues.pop(session_id, None)


class AcpTransport:
    name = "acp"
    _CONTEXT_OVERFLOW_MARKERS = (
        "context window",
        "context length",
        "maximum context",
        "max context",
        "context limit",
        "too many tokens",
        "token limit",
        "prompt is too long",
        "prompt too long",
        "上下文过长",
        "上下文超限",
        "超过上下文",
        "令牌过多",
        "超过令牌",
    )

    def __init__(self, settings: Settings, model_transport: Any) -> None:
        self.settings = settings
        self.model_transport = model_transport
        self.workers: list[AcpWorker] = []
        self.sessions = SessionStore(
            settings.session_ttl_seconds, settings.session_max_entries
        )
        self._pool_lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(
            settings.acp_max_workers + settings.acp_queue_size
        )
        self._cleanup_task: asyncio.Task[None] | None = None
        self._restart_failures = 0

    async def start(self) -> None:
        for _ in range(self.settings.acp_min_workers):
            await self._spawn_worker(
                self.settings.default_model, self.settings.effort
            )
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60, self.settings.session_ttl_seconds))
            await self.sessions.cleanup()
            await self._replace_dead_workers()

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        await asyncio.gather(
            *(worker.close() for worker in self.workers),
            return_exceptions=True,
        )
        self.workers.clear()

    async def _spawn_worker(self, model: str, effort: str) -> AcpWorker:
        worker = AcpWorker(self.settings, model, effort)
        await worker.start()
        self.workers.append(worker)
        return worker

    async def _replace_dead_workers(self) -> None:
        async with self._pool_lock:
            dead = [worker for worker in self.workers if not worker.healthy]
            for worker in dead:
                self.workers.remove(worker)
                await self.sessions.orphan_worker(worker.id)
                await worker.close()
            while len(self.workers) < self.settings.acp_min_workers:
                if self._restart_failures:
                    await asyncio.sleep(min(30, 2 ** self._restart_failures))
                try:
                    await self._spawn_worker(
                        self.settings.default_model, self.settings.effort
                    )
                    self._restart_failures = 0
                except Exception:
                    self._restart_failures += 1
                    break

    async def _select_worker(
        self,
        model: str,
        effort: str,
        preferred_id: str | None = None,
    ) -> AcpWorker:
        async with self._pool_lock:
            if preferred_id:
                preferred = next(
                    (
                        worker
                        for worker in self.workers
                        if worker.id == preferred_id
                        and worker.healthy
                        and worker.model == model
                        and worker.effort == effort
                    ),
                    None,
                )
                if preferred and preferred.active == 0:
                    return preferred
            healthy = [
                worker
                for worker in self.workers
                if worker.healthy
                and worker.model == model
                and worker.effort == effort
            ]
            idle_matching = [
                worker for worker in healthy if worker.active == 0
            ]
            if idle_matching:
                return min(idle_matching, key=lambda worker: worker.active)
            if len(self.workers) < self.settings.acp_max_workers:
                return await self._spawn_worker(model, effort)
            # 达到池上限时，用目标模型替换空闲的异模型 worker。关联会话
            # 保留为 orphan，后续请求可用客户端完整 Prompt 自动恢复。
            idle = next(
                (
                    item
                    for item in self.workers
                    if item.active == 0
                    and (item.model != model or item.effort != effort)
                ),
                None,
            )
            if idle is not None:
                self.workers.remove(idle)
                await self.sessions.orphan_worker(idle.id)
                await idle.close()
                return await self._spawn_worker(model, effort)
            raise TransportError(
                "ACP worker 池已饱和", ErrorCategory.CAPACITY, True
            )

    async def models(self) -> list[dict[str, Any]]:
        return await self.model_transport.models()

    async def generate(self, request: GenerationRequest) -> str:
        chunks: list[str] = []
        async for event in self.stream(request):
            if event.type is EventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type is EventType.ERROR:
                raise TransportError(event.text, ErrorCategory.PROTOCOL, True)
        return "".join(chunks).strip()

    def _rotation_reason(
        self,
        record: SessionRecord,
        full_prompt: str,
        latest_turn: str,
    ) -> str | None:
        current_chars = len(full_prompt)
        ratio = min(1.0, max(0.0, self.settings.session_compaction_ratio))
        if (
            record.last_prompt_chars > 0
            and ratio > 0
            and current_chars < record.last_prompt_chars * ratio
        ):
            return "client_compacted"
        if (
            self.settings.session_max_turns > 0
            and record.turn_count >= self.settings.session_max_turns
        ):
            return "max_turns"
        projected_chars = record.upstream_context_chars + len(latest_turn)
        if (
            self.settings.session_max_context_chars > 0
            and projected_chars > self.settings.session_max_context_chars
            and current_chars < projected_chars
        ):
            return "max_context_chars"
        return None

    @classmethod
    def _is_context_overflow(cls, message: str) -> bool:
        normalized = message.casefold()
        return any(
            marker in normalized for marker in cls._CONTEXT_OVERFLOW_MARKERS
        )

    async def _rebuild_session(
        self, record: SessionRecord, worker: AcpWorker
    ) -> str:
        upstream_id = await worker.new_session()
        record.worker_id = worker.id
        record.upstream_session_id = upstream_id
        record.turn_count = 0
        record.upstream_context_chars = 0
        record.rebuilt = True
        await self.sessions.put(record)
        return upstream_id

    async def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        record_lock: asyncio.Lock | None = None
        try:
            await asyncio.wait_for(
                self._capacity.acquire(), timeout=self.settings.timeout_seconds
            )
        except TimeoutError as exc:
            raise TransportError(
                "ACP 等待队列已满", ErrorCategory.CAPACITY, True
            ) from exc
        try:
            record = (
                await self.sessions.get(request.session_id)
                if request.session_id and self.settings.session_reuse_enabled
                else None
            )
            if record is not None:
                try:
                    await asyncio.wait_for(
                        record.lock.acquire(),
                        timeout=self.settings.timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise TransportError(
                        "ACP 会话等待超时",
                        ErrorCategory.CAPACITY,
                        True,
                    ) from exc
                record_lock = record.lock
            worker = await self._select_worker(
                request.model,
                request.effort,
                record.worker_id if record else None,
            )
        except BaseException:
            if record_lock is not None:
                record_lock.release()
            self._capacity.release()
            raise
        worker.active += 1
        try:
            async with worker.lock:
                rebuild_reason: str | None = None
                if record is None:
                    upstream_id = await worker.new_session()
                    if request.session_id and self.settings.session_reuse_enabled:
                        record = SessionRecord(
                            request.session_id, worker.id, upstream_id
                        )
                        await record.lock.acquire()
                        record_lock = record.lock
                        await self.sessions.put(record)
                else:
                    if (
                        record.worker_id != worker.id
                        or not record.upstream_session_id
                    ):
                        upstream_id = await self._rebuild_session(record, worker)
                        rebuild_reason = "worker_changed"
                    else:
                        upstream_id = record.upstream_session_id

                latest_turn = self._latest_user_turn(request.prompt)
                if record and record.turn_count and not record.rebuilt:
                    reason = self._rotation_reason(
                        record, request.prompt, latest_turn
                    )
                    if reason:
                        upstream_id = await self._rebuild_session(record, worker)
                        rebuild_reason = reason

                prompt = (
                    latest_turn
                    if record and record.turn_count and not record.rebuilt
                    else request.prompt
                )
                retried_context_overflow = False
                succeeded = False
                completed_output_chars = 0
                while True:
                    retry_with_full_prompt = False
                    emitted_output = False
                    attempt_output_chars = 0
                    async for event in worker.prompt(upstream_id, prompt):
                        if event.type in {
                            EventType.TEXT_DELTA,
                            EventType.THINKING_DELTA,
                            EventType.TOOL,
                        }:
                            emitted_output = True
                        if event.type in {
                            EventType.TEXT_DELTA,
                            EventType.THINKING_DELTA,
                        }:
                            attempt_output_chars += len(event.text)
                        if (
                            event.type is EventType.ERROR
                            and record is not None
                            and not emitted_output
                            and not retried_context_overflow
                            and self._is_context_overflow(event.text)
                        ):
                            upstream_id = await self._rebuild_session(
                                record, worker
                            )
                            prompt = request.prompt
                            retried_context_overflow = True
                            retry_with_full_prompt = True
                            rebuild_reason = "context_overflow"
                            break
                        if event.type is EventType.ERROR and record is not None:
                            record.upstream_session_id = ""
                            record.turn_count = 0
                            record.upstream_context_chars = 0
                            record.rebuilt = True
                        if event.type is EventType.DONE:
                            succeeded = True
                            completed_output_chars = attempt_output_chars
                        if (
                            rebuild_reason is not None
                            and event.type is not EventType.ERROR
                        ):
                            event.data.setdefault("session_rebuilt", True)
                            event.data.setdefault(
                                "session_rebuild_reason", rebuild_reason
                            )
                            rebuild_reason = None
                        yield event
                    if retry_with_full_prompt:
                        continue
                    break

                if record and succeeded:
                    record.turn_count += 1
                    record.last_prompt_chars = len(request.prompt)
                    if prompt == request.prompt:
                        record.upstream_context_chars = (
                            len(prompt) + completed_output_chars
                        )
                    else:
                        record.upstream_context_chars += (
                            len(prompt) + completed_output_chars
                        )
                    record.last_used = asyncio.get_running_loop().time()
                    record.rebuilt = False
                    await self.sessions.put(record)
        except Exception as exc:
            yield GenerationEvent(
                EventType.ERROR,
                text=f"ACP worker 失败：{exc}",
                data={"category": ErrorCategory.PROTOCOL.value},
            )
        finally:
            worker.active -= 1
            if record_lock is not None:
                record_lock.release()
            self._capacity.release()

    @staticmethod
    def _latest_user_turn(prompt: str) -> str:
        matches = list(
            re.finditer(
                r"### 用户(?:（[^）]+）)?\n(.*?)(?=\n\n### |\Z)",
                prompt,
                re.DOTALL,
            )
        )
        if not matches:
            return prompt
        return matches[-1].group(1).strip()

    async def cancel(self, request_id: str) -> None:
        record = await self.sessions.get(request_id)
        if not record:
            return
        worker = next(
            (item for item in self.workers if item.id == record.worker_id), None
        )
        if worker and worker.healthy:
            await worker.agent.cancel(record.upstream_session_id)
