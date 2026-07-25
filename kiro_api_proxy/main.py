from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .admin.config_store import store as config_store
from .admin.routes import router as admin_router, set_reload_hook
from .config import settings
from .model_cache import ModelCache
from .prompts import (
    anthropic_to_messages,
    anthropic_upstream_model,
    claude_session_working_directory,
    content_text,
    messages_to_prompt,
    prompt_working_directory,
    responses_to_messages,
)
from .schemas import (
    AnthropicMessage,
    AnthropicRequest,
    ChatRequest,
    Message,
    ResponsesRequest,
)
from .tools import (
    ToolCallAccumulator,
    anthropic_tool_history,
    anthropic_tool_results,
    anthropic_tools_to_specs,
    openai_tool_history,
    openai_tool_results,
    openai_tools_to_specs,
    parse_tool_input,
)
from .transports import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)
from .transports.runtime import RuntimeTransport
from .usage import TokenUsage, estimate_tokens

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
transport = RuntimeTransport(settings)
model_cache = ModelCache(
    settings.model_cache_ttl_seconds,
    settings.model_cache_stale_seconds,
)
_credential_fingerprint: tuple[int, ...] | None = None
_inflight_generations: set[str] = set()
_inflight_lock = asyncio.Lock()


def _log(event: str, **fields: Any) -> None:
    safe = {
        key: value
        for key, value in fields.items()
        if key.lower() not in {"authorization", "api_key", "token", "prompt"}
    }
    safe.update({"event": event, "request_id": request_id_var.get()})
    logger.info(json.dumps(safe, ensure_ascii=False, default=str))


def authorize(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    # 动态读取 config.json（优先）/.env 的 API Key，使界面改 Key 即时生效。
    api_key = config_store.get().api_key
    if (
        api_key
        and authorization != f"Bearer {api_key}"
        and x_api_key != api_key
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "无效的 API Key",
                    "type": "authentication_error",
                }
            },
        )


def resolve_model(model: str) -> tuple[str, str]:
    normalized = model.removesuffix("[1m]")
    thinking = normalized.endswith("-thinking")
    if thinking:
        normalized = normalized.removesuffix("-thinking")
    claude_aliases = {
        "claude-opus-4-8": "claude-opus-4.8",
        "claude-opus-4-7": "claude-opus-4.7",
        "claude-opus-4-6": "claude-opus-4.6",
        "claude-opus-4-5": "claude-opus-4.5",
        "claude-sonnet-4-6": "claude-sonnet-4.6",
        "claude-sonnet-4-5": "claude-sonnet-4.5",
        "claude-sonnet-4": "claude-sonnet-4",
        "claude-haiku-4-5": "claude-haiku-4.5",
    }
    upstream_model = claude_aliases.get(normalized, normalized)
    return upstream_model, (settings.effort or "high") if thinking else settings.effort


def normalize_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    normalized = effort.lower()
    if normalized == "none":
        return ""
    if normalized == "minimal":
        return "low"
    if normalized in {"low", "medium", "high", "xhigh", "max"}:
        return normalized
    raise HTTPException(status_code=400, detail=f"不支持的推理强度：{effort}")


async def available_models(*, force: bool = False) -> list[dict[str, Any]]:
    global _credential_fingerprint
    credential_files = (
        Path.home() / ".local/share/kiro-cli/data.sqlite3",
        Path.home() / ".local/share/.kiro-account-manager/accounts.json",
    )
    fingerprint = tuple(
        path.stat().st_mtime_ns if path.exists() else 0
        for path in credential_files
    )
    if (
        _credential_fingerprint is not None
        and fingerprint != _credential_fingerprint
    ):
        model_cache.invalidate()
        force = True
    _credential_fingerprint = fingerprint
    try:
        if not settings.model_cache_enabled:
            return await transport.models()
        snapshot = await model_cache.get(transport.models, force=force)
        _log("model_discovery", source=snapshot.source, count=len(snapshot.models))
        return snapshot.models
    except TransportError as exc:
        raise _http_error(exc) from exc


async def ensure_model(model: str) -> None:
    models = await available_models()
    if model in {item["model_id"] for item in models}:
        return
    # 模型不存在时主动刷新一次，避免缓存掩盖刚发布的模型。
    models = await available_models(force=True)
    if model not in {item["model_id"] for item in models}:
        raise HTTPException(status_code=404, detail=f"Kiro 模型不存在：{model}")


def _generation_request(
    model: str,
    prompt: str,
    effort_override: str | None = None,
    session_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> GenerationRequest:
    upstream_model, model_effort = resolve_model(model)
    requested_effort = normalize_effort(effort_override)
    effort = model_effort if requested_effort is None else requested_effort
    return GenerationRequest(
        upstream_model,
        prompt,
        effort,
        session_id,
        prompt_working_directory(prompt),
        tools or [],
        tool_results or [],
        history or [],
    )


def _session_context(request: Request) -> tuple[str, str | None]:
    external_id = next(
        (
            request.headers[name]
            for name in (
                "x-claude-code-session-id",
                "x-opencode-session-id",
                "x-session-id",
                "openai-conversation-id",
            )
            if request.headers.get(name)
        ),
        request_id_var.get(),
    )
    return external_id, None


def _set_session_headers(response: Response, external_id: str) -> None:
    response.headers["x-kiro-session-id"] = external_id
    response.headers["x-claude-code-session-id"] = external_id


async def _claude_working_directory(session_id: str) -> str | None:
    for delay in (0.0, 0.1, 0.2, 0.4, 0.8):
        if delay:
            await asyncio.sleep(delay)
        working_directory = await asyncio.to_thread(
            claude_session_working_directory,
            session_id,
        )
        if working_directory:
            return working_directory
    return None


_ERROR_STATUSES: dict[ErrorCategory, int] = {
    ErrorCategory.AUTHENTICATION: 401,
    ErrorCategory.MODEL_NOT_FOUND: 404,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.CAPACITY: 503,
    ErrorCategory.PROTOCOL: 502,
    ErrorCategory.UPSTREAM: 502,
    ErrorCategory.CANCELLED: 499,
}


def _http_error(exc: TransportError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUSES.get(exc.category, 502), detail=str(exc)
    )


async def call_kiro(
    model: str,
    prompt: str,
    effort_override: str | None = None,
    session_id: str | None = None,
) -> str:
    generation = _generation_request(model, prompt, effort_override, session_id)
    await ensure_model(generation.model)
    started = time.perf_counter()
    try:
        result = await transport.generate(generation)
    except TransportError as exc:
        raise _http_error(exc) from exc
    _log(
        "generation_complete",
        transport=transport.name,
        model=generation.model,
        total_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return result


async def _events(
    model: str,
    prompt: str,
    effort: str | None,
    client_request: Request | None = None,
    session_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> AsyncIterator[GenerationEvent]:
    generation = _generation_request(
        model, prompt, effort, session_id, tools, tool_results, history
    )
    await ensure_model(generation.model)
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                generation.model,
                generation.effort,
                generation.session_id or "",
                generation.working_directory or "",
                generation.prompt,
            )
        ).encode()
    ).hexdigest()
    async with _inflight_lock:
        if fingerprint in _inflight_generations:
            yield GenerationEvent(
                EventType.ERROR,
                text="相同请求正在处理中，请勿重复提交",
                data={"category": ErrorCategory.CAPACITY.value},
            )
            return
        _inflight_generations.add(fingerprint)
    started = time.perf_counter()
    first = True
    saw_usage = False
    try:
        if not settings.incremental_streaming:
            try:
                content = await transport.generate(generation)
            except TransportError as exc:
                yield GenerationEvent(
                    EventType.ERROR,
                    text=str(exc),
                    data={"category": exc.category.value},
                )
                return
            _log(
                "first_token",
                transport=transport.name,
                model=generation.model,
                ttft_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            yield GenerationEvent(EventType.TEXT_DELTA, text=content)
            yield GenerationEvent(EventType.DONE)
            return
        upstream = transport.stream(generation)
        deadline = asyncio.get_running_loop().time() + settings.timeout_seconds
        next_event: asyncio.Task[GenerationEvent] | None = None
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    yield GenerationEvent(
                        EventType.ERROR,
                        text="Kiro 请求总时长超限",
                        data={"category": ErrorCategory.TIMEOUT.value},
                    )
                    return
                next_event = asyncio.create_task(anext(upstream))
                # 先让上游生成器进入执行态，确保随后取消时能运行其 finally。
                await asyncio.sleep(0)
                while not next_event.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        next_event.cancel()
                        await asyncio.gather(
                            next_event, return_exceptions=True
                        )
                        yield GenerationEvent(
                            EventType.ERROR,
                            text="Kiro 请求总时长超限",
                            data={"category": ErrorCategory.TIMEOUT.value},
                        )
                        return
                    if (
                        client_request is not None
                        and await client_request.is_disconnected()
                    ):
                        next_event.cancel()
                        await asyncio.gather(
                            next_event, return_exceptions=True
                        )
                        _log("client_disconnected", transport=transport.name)
                        return
                    await asyncio.wait(
                        {next_event},
                        timeout=min(0.25, remaining),
                    )
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                finally:
                    next_event = None
                if (
                    client_request is not None
                    and await client_request.is_disconnected()
                ):
                    _log("client_disconnected", transport=transport.name)
                    return
                if first and event.type in {
                    EventType.TEXT_DELTA,
                    EventType.THINKING_DELTA,
                }:
                    first = False
                    _log(
                        "first_token",
                        transport=transport.name,
                        model=generation.model,
                        ttft_ms=round(
                            (time.perf_counter() - started) * 1000, 2
                        ),
                        forward_ms=round(
                            (
                                time.perf_counter()
                                - float(
                                    event.data.get(
                                        "upstream_received_at",
                                        time.perf_counter(),
                                    )
                                )
                            )
                            * 1000,
                            2,
                        ),
                    )
                if event.type is EventType.USAGE:
                    saw_usage = True
                yield event
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            await upstream.aclose()
    finally:
        async with _inflight_lock:
            _inflight_generations.discard(fingerprint)
        _log(
            "stream_complete",
            transport=transport.name,
            model=generation.model,
            total_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        if not saw_usage:
            _log(
                "usage_missing",
                transport=transport.name,
                model=generation.model,
            )


async def _collect_generation(
    model: str,
    prompt: str,
    effort: str | None,
    client_request: Request | None = None,
    session_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, TokenUsage, list[dict[str, str]]]:
    """消费 _events 累积完整文本、真实用量与工具调用，供非流式端点复用。

    上游提供 token 统计时直接采用，否则由 ensure_estimates 回退估算，
    从而让非流式响应也回传准确的上下文用量。
    """
    usage = TokenUsage()
    parts: list[str] = []
    tool_calls = ToolCallAccumulator()
    event_args = (
        (model, prompt, effort, client_request, session_id, tools, tool_results, history)
        if history
        else (model, prompt, effort, client_request, session_id, tools, tool_results)
    )
    async for event in _events(*event_args):
        if event.type is EventType.TEXT_DELTA:
            parts.append(event.text)
        elif event.type is EventType.TOOL:
            tool_calls.add(event.data)
        elif event.type is EventType.USAGE:
            usage.update(event.data)
        elif event.type is EventType.ERROR:
            category = ErrorCategory(
                event.data.get("category", ErrorCategory.UPSTREAM.value)
            )
            raise HTTPException(
                status_code=_ERROR_STATUSES.get(category, 502),
                detail=event.text,
            )
    content = "".join(parts).strip()
    usage.ensure_estimates(prompt, content)
    return content, usage, tool_calls.calls()


def chat_chunk(
    completion_id: str,
    created: int,
    model: str,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    usage_only: bool = False,
    tool_calls: list[dict[str, Any]] | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": (
            []
            if usage_only
            else [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ]
        ),
        "usage": usage,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def chat_stream(
    model: str,
    prompt: str,
    completion_id: str,
    created: int,
    reasoning_effort: str | None = None,
    client_request: Request | None = None,
    session_id: str | None = None,
    include_usage: bool = False,
    tools: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    # 不预填本地估算值，否则上游随后上报的 context_tokens 无法在
    # ensure_estimates 中成为 input_tokens，长会话会被严重低估。
    usage = TokenUsage()
    output_parts: list[str] = []
    tool_index: dict[str, int] = {}
    try:
        event_args = (
            (
                model,
                prompt,
                reasoning_effort,
                client_request,
                session_id,
                tools,
                tool_results,
                history,
            )
            if history
            else (
                model,
                prompt,
                reasoning_effort,
                client_request,
                session_id,
                tools,
                tool_results,
            )
        )
        async for event in _events(*event_args):
            if event.type is EventType.TEXT_DELTA:
                output_parts.append(event.text)
                yield chat_chunk(completion_id, created, model, event.text)
            elif event.type is EventType.TOOL:
                data = event.data
                tool_id = data.get("id", "")
                if tool_id not in tool_index:
                    index = len(tool_index)
                    tool_index[tool_id] = index
                    yield chat_chunk(
                        completion_id,
                        created,
                        model,
                        tool_calls=[
                            {
                                "index": index,
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": data.get("name", ""),
                                    "arguments": "",
                                },
                            }
                        ],
                    )
                if data.get("input"):
                    yield chat_chunk(
                        completion_id,
                        created,
                        model,
                        tool_calls=[
                            {
                                "index": tool_index[tool_id],
                                "function": {"arguments": data["input"]},
                            }
                        ],
                    )
            elif event.type is EventType.USAGE:
                usage.update(event.data)
            elif event.type is EventType.ERROR:
                error = {"error": {"message": event.text, "type": "api_error"}}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                break
        else:
            yield chat_chunk(
                completion_id,
                created,
                model,
                finish_reason="tool_calls" if tool_index else "stop",
            )
            if include_usage:
                usage.ensure_estimates(prompt, "".join(output_parts))
                yield chat_chunk(
                    completion_id,
                    created,
                    model,
                    usage=usage.chat_completions(),
                    usage_only=True,
                )
    except HTTPException as exc:
        error = {"error": {"message": str(exc.detail), "type": "api_error"}}
        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def anthropic_event(event: str, payload: dict[str, Any]) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


async def anthropic_stream(
    request: AnthropicRequest,
    message_id: str,
    client_request: Request | None = None,
    session_id: str | None = None,
    working_directory: str | None = None,
) -> AsyncIterator[str]:
    prompt = messages_to_prompt(anthropic_to_messages(request))
    if working_directory:
        prompt = f"Working directory: {working_directory}\n\n{prompt}"
    tools = anthropic_tools_to_specs(request.tools)
    tool_results = anthropic_tool_results(request.messages)
    history = anthropic_tool_history(request.messages)
    if tool_results and not history:
        tool_results = []
    usage = TokenUsage()
    input_estimate = estimate_tokens(prompt)
    yield anthropic_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": request.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_estimate,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    )
    output_parts: list[str] = []
    # 块状态机：文本块与 tool_use 块按到达顺序分配 index，交错时先关闭再开启。
    next_index = 0
    open_kind: str | None = None  # None | "text" | "tool"
    open_index = -1
    open_tool_id: str | None = None
    saw_tool = False

    def close_open() -> str | None:
        nonlocal open_kind, open_tool_id
        if open_kind is None:
            return None
        payload = anthropic_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": open_index},
        )
        open_kind = None
        open_tool_id = None
        return payload

    try:
        event_args = (
            (
                anthropic_upstream_model(request),
                prompt,
                None,
                client_request,
                session_id,
                tools,
                tool_results,
                history,
            )
            if history
            else (
                anthropic_upstream_model(request),
                prompt,
                None,
                client_request,
                session_id,
                tools,
                tool_results,
            )
        )
        async for event in _events(*event_args):
            if event.type is EventType.TEXT_DELTA:
                output_parts.append(event.text)
                if open_kind != "text":
                    closed = close_open()
                    if closed:
                        yield closed
                    open_index = next_index
                    next_index += 1
                    open_kind = "text"
                    yield anthropic_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": open_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield anthropic_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": open_index,
                        "delta": {"type": "text_delta", "text": event.text},
                    },
                )
            elif event.type is EventType.TOOL:
                data = event.data
                tool_id = data.get("id", "")
                if open_kind != "tool" or open_tool_id != tool_id:
                    closed = close_open()
                    if closed:
                        yield closed
                    open_index = next_index
                    next_index += 1
                    open_kind = "tool"
                    open_tool_id = tool_id
                    saw_tool = True
                    yield anthropic_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": open_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": data.get("name", ""),
                                "input": {},
                            },
                        },
                    )
                if data.get("input"):
                    yield anthropic_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": open_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": data["input"],
                            },
                        },
                    )
                if data.get("stop"):
                    closed = close_open()
                    if closed:
                        yield closed
            elif event.type is EventType.USAGE:
                usage.update(event.data)
            elif event.type is EventType.ERROR:
                yield anthropic_event(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": event.text},
                    },
                )
                return
    except HTTPException as exc:
        yield anthropic_event(
            "error",
            {
                "type": "error",
                "error": {"type": "api_error", "message": str(exc.detail)},
            },
        )
        return
    usage.ensure_estimates(prompt, "".join(output_parts))
    closed = close_open()
    if closed:
        yield closed
    yield anthropic_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if saw_tool else "end_turn",
                "stop_sequence": None,
            },
            "usage": usage.anthropic(),
        },
    )
    yield anthropic_event("message_stop", {"type": "message_stop"})


async def responses_stream(
    request: ResponsesRequest,
    client_request: Request | None = None,
    session_id: str | None = None,
) -> AsyncIterator[str]:
    response_id = f"resp_{uuid.uuid4().hex}"
    prompt = messages_to_prompt(responses_to_messages(request))
    messages = responses_to_messages(request)
    tools = openai_tools_to_specs(request.tools)
    tool_results = openai_tool_results(messages)
    history = openai_tool_history(messages)
    if tool_results and not history:
        tool_results = []
    # 等流结束后再决定是否估算，优先采用上游的真实上下文用量。
    usage = TokenUsage()

    def event(name: str, payload: dict[str, Any]) -> str:
        return (
            f"event: {name}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )

    yield event(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": request.model,
            },
        },
    )
    yield event(
        "response.in_progress",
        {
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": request.model,
            },
        },
    )
    item_id = f"msg_{uuid.uuid4().hex}"
    yield event(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "response_id": response_id,
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
    )
    yield event(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )
    sequence = 0
    text_parts: list[str] = []
    tool_calls = ToolCallAccumulator()
    event_args = (
        (
            request.model,
            prompt,
            request.reasoning_effort,
            client_request,
            session_id,
            tools,
            tool_results,
            history,
        )
        if history
        else (
            request.model,
            prompt,
            request.reasoning_effort,
            client_request,
            session_id,
            tools,
            tool_results,
        )
    )
    async for item in _events(*event_args):
        if item.type is EventType.TEXT_DELTA:
            sequence += 1
            text_parts.append(item.text)
            yield event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "response_id": response_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": item.text,
                    "sequence_number": sequence,
                },
            )
        elif item.type is EventType.TOOL:
            tool_calls.add(item.data)
        elif item.type is EventType.USAGE:
            usage.update(item.data)
        elif item.type is EventType.ERROR:
            yield event(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {"id": response_id, "status": "failed"},
                    "error": {"message": item.text, "type": "api_error"},
                },
            )
            return
    output_text = "".join(text_parts)
    usage.ensure_estimates(prompt, output_text)
    yield event(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": output_text,
        },
    )
    yield event(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": output_text,
                "annotations": [],
            },
        },
    )
    yield event(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "response_id": response_id,
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": output_text,
                        "annotations": [],
                    }
                ],
            },
        },
    )
    for offset, call in enumerate(tool_calls.calls(), start=1):
        fc_id = f"fc_{uuid.uuid4().hex}"
        arguments = call["input"] or "{}"
        yield event(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "response_id": response_id,
                "output_index": offset,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": "",
                },
            },
        )
        sequence += 1
        yield event(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "response_id": response_id,
                "item_id": fc_id,
                "output_index": offset,
                "delta": arguments,
                "sequence_number": sequence,
            },
        )
        yield event(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "response_id": response_id,
                "item_id": fc_id,
                "output_index": offset,
                "arguments": arguments,
            },
        )
        yield event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "response_id": response_id,
                "output_index": offset,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": arguments,
                },
            },
        )
    yield event(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "model": request.model,
                "usage": usage.responses(),
            },
        },
    )
    yield "data: [DONE]\n\n"


async def _reload_transport() -> None:
    """用 config.json（优先）/.env 的最新凭据重建 Runtime 传输。

    供界面登录/导入凭据/改凭据路径后热重载调用；也用于启动初始化。
    失败仅记录日志，不影响调用方——凭据已通过校验写入，重启即可生效，
    且核心场景允许先启动服务、后登录。
    """
    global transport
    cfg = config_store.get()
    new_settings = dataclasses.replace(
        settings,
        runtime_credentials_file=cfg.runtime_credentials_file,
        runtime_account_index=cfg.runtime_account_index,
    )
    candidate = RuntimeTransport(new_settings)
    try:
        await candidate.start()
    except TransportError as exc:
        await candidate.close()
        _log("transport_reload_failed", error=str(exc))
        return
    old = transport
    transport = candidate
    model_cache.invalidate()
    await old.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 容忍无凭据启动：核心场景是先启动服务、再通过管理界面登录。
    await _reload_transport()
    try:
        yield
    finally:
        await transport.close()


app = FastAPI(title="Kiro API Proxy", version="1.1.0", lifespan=lifespan)

# 界面登录/导入凭据后热重载传输。
set_reload_hook(_reload_transport)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response
    finally:
        _log(
            "request_complete",
            method=request.method,
            path=request.url.path,
            total_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        request_id_var.reset(token)


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "Kiro API Proxy", "status": "ok", "docs": "/docs"}


@app.head("/")
async def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.head("/health")
async def health_head() -> Response:
    return Response(status_code=200)


@app.get("/v1/models", dependencies=[Depends(authorize)])
async def models() -> dict[str, Any]:
    result = await available_models()
    return {
        "object": "list",
        "data": [
            {
                "id": item["model_id"],
                "object": "model",
                "created": 0,
                "owned_by": "kiro",
                "context_window": item.get("context_window_tokens")
                or settings.default_context_window,
            }
            for item in result
        ],
    }


@app.post("/admin/models/refresh", dependencies=[Depends(authorize)])
async def refresh_models() -> dict[str, Any]:
    model_cache.invalidate()
    models = await available_models(force=True)
    return {"status": "ok", "count": len(models)}


@app.post("/v1/chat/completions", dependencies=[Depends(authorize)])
async def chat(request: ChatRequest, raw_request: Request, response: Response):
    external_id, session_key = _session_context(raw_request)
    _set_session_headers(response, external_id)
    prompt = messages_to_prompt(request.messages)
    tools = openai_tools_to_specs(request.tools)
    tool_results = openai_tool_results(request.messages)
    history = openai_tool_history(request.messages)
    if tool_results and not history:
        tool_results = []
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    if request.stream:
        return StreamingResponse(
            chat_stream(
                request.model,
                prompt,
                completion_id,
                created,
                request.reasoning_effort,
                raw_request,
                session_key,
                bool(
                    request.stream_options
                    and request.stream_options.get("include_usage")
                ),
                tools,
                tool_results,
                history,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    content, usage, tool_calls = await _collect_generation(
        request.model,
        prompt,
        request.reasoning_effort,
        raw_request,
        session_key,
        tools,
        tool_results,
        history,
    )
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call["input"] or "{}",
                },
            }
            for call in tool_calls
        ]
        finish_reason = "tool_calls"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage.chat_completions(),
    }


@app.post("/v1/responses", dependencies=[Depends(authorize)])
async def responses(
    request: ResponsesRequest, raw_request: Request, response: Response
):
    external_id, session_key = _session_context(raw_request)
    _set_session_headers(response, external_id)
    if request.stream:
        return StreamingResponse(
            responses_stream(request, raw_request, session_key),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    messages = responses_to_messages(request)
    prompt = messages_to_prompt(messages)
    tool_results = openai_tool_results(messages)
    history = openai_tool_history(messages)
    if tool_results and not history:
        tool_results = []
    content, usage, tool_calls = await _collect_generation(
        request.model,
        prompt,
        request.reasoning_effort,
        raw_request,
        session_key,
        openai_tools_to_specs(request.tools),
        tool_results,
        history,
    )
    response_id = f"resp_{uuid.uuid4().hex}"
    output: list[dict[str, Any]] = [
        {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": content, "annotations": []}
            ],
        }
    ]
    for call in tool_calls:
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["input"] or "{}",
            }
        )
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": request.model,
        "output": output,
        "output_text": content,
        "usage": usage.responses(),
    }


@app.post("/v1/messages", dependencies=[Depends(authorize)])
async def anthropic_messages(
    request: AnthropicRequest, raw_request: Request, response: Response
):
    external_id, session_key = _session_context(raw_request)
    working_directory = await _claude_working_directory(external_id)
    _set_session_headers(response, external_id)
    message_id = f"msg_{uuid.uuid4().hex}"
    if request.stream:
        return StreamingResponse(
            anthropic_stream(
                request,
                message_id,
                raw_request,
                session_key,
                working_directory,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    prompt = messages_to_prompt(anthropic_to_messages(request))
    if working_directory:
        prompt = f"Working directory: {working_directory}\n\n{prompt}"
    tool_results = anthropic_tool_results(request.messages)
    history = anthropic_tool_history(request.messages)
    if tool_results and not history:
        tool_results = []
    content, usage, tool_calls = await _collect_generation(
        anthropic_upstream_model(request),
        prompt,
        None,
        raw_request,
        session_key,
        anthropic_tools_to_specs(request.tools),
        tool_results,
        history,
    )
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for call in tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": parse_tool_input(call["input"]),
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": blocks,
        "stop_reason": "tool_use" if tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": usage.anthropic(),
    }


@app.post("/v1/messages/count_tokens", dependencies=[Depends(authorize)])
async def anthropic_count_tokens(request: AnthropicRequest):
    text = messages_to_prompt(anthropic_to_messages(request))
    return {"input_tokens": estimate_tokens(text)}


@app.exception_handler(HTTPException)
async def openai_error(_: Request, exc: HTTPException):
    detail = exc.detail
    payload = (
        detail
        if isinstance(detail, dict) and "error" in detail
        else {"error": {"message": str(detail), "type": "api_error"}}
    )
    return JSONResponse(status_code=exc.status_code, content=payload)


# 管理接口与静态页。include_router 在所有 @app 路由之后注册，
# StaticFiles 挂在 /admin 兜底其余静态资源，不会抢占 /admin/api/* 与
# /admin/models/refresh。
app.include_router(admin_router)
_ADMIN_STATIC_DIR = Path(__file__).parent / "admin" / "static"
app.mount(
    "/admin",
    StaticFiles(directory=_ADMIN_STATIC_DIR, html=True),
    name="admin-static",
)


def run() -> None:
    """命令行入口：按 config.json（优先）/.env 的地址端口启动服务。"""
    import uvicorn

    cfg = config_store.get()
    uvicorn.run(app, host=cfg.api_host, port=cfg.api_port)


__all__ = [
    "AnthropicMessage",
    "AnthropicRequest",
    "ChatRequest",
    "Message",
    "ResponsesRequest",
    "anthropic_to_messages",
    "anthropic_upstream_model",
    "app",
    "available_models",
    "call_kiro",
    "content_text",
    "messages_to_prompt",
    "resolve_model",
    "responses_to_messages",
]
