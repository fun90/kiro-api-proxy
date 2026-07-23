from __future__ import annotations

import asyncio
import codecs
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from ..config import Settings
from .base import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
    kiro_environment,
)

ANSI_ESCAPE = re.compile(
    r"(?:\x1B[@-_][0-?]*[ -/]*[@-~])|(?:\x1B\][^\x07]*(?:\x07|\x1B\\))"
)
CREDIT_LINE = re.compile(r"^\s*[▸>]?\s*Credits:.*$", re.MULTILINE)


def clean_text(text: str) -> str:
    text = text.replace("\r", "")
    text = ANSI_ESCAPE.sub("", text)
    text = CREDIT_LINE.sub("", text)
    text = text.replace("\x00", "")
    return text


def clean_output(raw: bytes) -> str:
    return re.sub(
        r"^\s*>\s?", "", clean_text(raw.decode("utf-8", errors="replace")), count=1
    ).strip()


class CliTransport:
    name = "cli"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._processes: set[asyncio.subprocess.Process] = set()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        for process in list(self._processes):
            if process.returncode is None:
                process.terminate()
        if self._processes:
            await asyncio.gather(
                *(process.wait() for process in self._processes),
                return_exceptions=True,
            )

    def command_for(self, request: GenerationRequest) -> list[str]:
        command = [
            self.settings.kiro_cli,
            "chat",
            "--no-interactive",
            "--wrap",
            "never",
            "--model",
            request.model,
        ]
        if request.effort:
            command.extend(["--effort", request.effort])
        if self.settings.trust_tools == "*":
            command.append("--trust-all-tools")
        else:
            command.append(f"--trust-tools={self.settings.trust_tools}")
        command.append(request.prompt)
        return command

    def _environment(
        self, working_directory: str | None = None
    ) -> dict[str, str]:
        return kiro_environment(
            self.settings.extra_path,
            working_directory or self.settings.working_directory,
        )

    async def _spawn(
        self, request: GenerationRequest
    ) -> asyncio.subprocess.Process:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command_for(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=request.working_directory or self.settings.working_directory,
                env=self._environment(request.working_directory),
            )
        except FileNotFoundError as exc:
            raise TransportError(
                "找不到 kiro-cli", ErrorCategory.UPSTREAM
            ) from exc
        self._processes.add(process)
        return process

    async def models(self) -> list[dict[str, Any]]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.kiro_cli,
                "chat",
                "--list-models",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.settings.working_directory,
            )
        except FileNotFoundError as exc:
            raise TransportError("找不到 kiro-cli") from exc
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise TransportError(clean_output(stderr) or "读取 Kiro 模型列表失败")
        try:
            return json.loads(stdout)["models"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TransportError(
                "Kiro 模型列表格式异常", ErrorCategory.PROTOCOL
            ) from exc

    async def generate(self, request: GenerationRequest) -> str:
        chunks: list[str] = []
        async for event in self.stream(request):
            if event.type is EventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type is EventType.ERROR:
                raise TransportError(
                    event.text,
                    ErrorCategory(event.data.get("category", "upstream")),
                )
        result = "".join(chunks).strip()
        if not result:
            raise TransportError("Kiro 返回了空响应", ErrorCategory.PROTOCOL)
        return result

    async def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        async with self._semaphore:
            process = await self._spawn(request)
            assert process.stdout is not None
            assert process.stderr is not None
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            emitted = False
            try:
                while True:
                    raw = await asyncio.wait_for(
                        process.stdout.read(1024),
                        timeout=self.settings.timeout_seconds,
                    )
                    if not raw:
                        break
                    text = clean_text(decoder.decode(raw))
                    if text:
                        emitted = True
                        yield GenerationEvent(EventType.TEXT_DELTA, text=text)
                tail = clean_text(decoder.decode(b"", final=True))
                if tail:
                    emitted = True
                    yield GenerationEvent(EventType.TEXT_DELTA, text=tail)
                stderr = await process.stderr.read()
                returncode = await process.wait()
                if returncode:
                    message = clean_output(stderr) or "Kiro CLI 调用失败"
                    yield GenerationEvent(
                        EventType.ERROR,
                        text=message,
                        data={"category": ErrorCategory.UPSTREAM.value},
                    )
                elif not emitted:
                    yield GenerationEvent(
                        EventType.ERROR,
                        text="Kiro 返回了空响应",
                        data={"category": ErrorCategory.PROTOCOL.value},
                    )
                else:
                    yield GenerationEvent(EventType.DONE)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()
                yield GenerationEvent(
                    EventType.ERROR,
                    text="Kiro 请求超时",
                    data={"category": ErrorCategory.TIMEOUT.value},
                )
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                raise
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                self._processes.discard(process)

    async def cancel(self, request_id: str) -> None:
        # CLI 回退传输没有稳定的上游请求 ID，取消由生成协程取消传播。
        return None
