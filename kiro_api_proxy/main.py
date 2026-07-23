from __future__ import annotations

import asyncio
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
from .transports import (
    AdaptiveTransport,
    CliTransport,
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)
from .transports.acp import AcpTransport
from .transports.runtime import RuntimeTransport
from .usage import TokenUsage, estimate_tokens

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
cli_transport = CliTransport(settings)
transport_options: dict[str, Any] = {"cli": cli_transport}
if settings.acp_enabled:
    transport_options["acp"] = AcpTransport(settings, cli_transport)
if settings.runtime_enabled:
    transport_options["runtime"] = RuntimeTransport(settings)
ordered_transports = [
    transport_options[name]
    for name in settings.transport_priority
    if name in transport_options
]
if cli_transport not in ordered_transports:
    ordered_transports.append(cli_transport)
if len(ordered_transports) > 1:
    transport = AdaptiveTransport(ordered_transports)
else:
    transport = cli_transport
model_cache = ModelCache(
    settings.model_cache_ttl_seconds,
    settings.model_cache_stale_seconds,
)
_credential_fingerprint: tuple[int, ...] | None = None

# 保留旧模块级名称，避免现有集成导入时破坏兼容性。
KIRO_CLI = settings.kiro_cli
API_KEY = settings.api_key
DEFAULT_MODEL = settings.default_model
TIMEOUT = settings.timeout_seconds
MAX_CONCURRENCY = settings.max_concurrency
WORKING_DIRECTORY = settings.working_directory
EFFORT = settings.effort
TRUST_TOOLS = settings.trust_tools
RESPONSE_LANGUAGE = settings.response_language


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
    if (
        settings.api_key
        and authorization != f"Bearer {settings.api_key}"
        and x_api_key != settings.api_key
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
    if not settings.session_reuse_enabled:
        return external_id, None
    credential = (
        request.headers.get("authorization")
        or request.headers.get("x-api-key")
        or "anonymous"
    )
    tenant = hashlib.sha256(credential.encode()).hexdigest()[:16]
    return external_id, f"{tenant}:{external_id}"


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


def _http_error(exc: TransportError) -> HTTPException:
    statuses = {
        ErrorCategory.AUTHENTICATION: 401,
        ErrorCategory.MODEL_NOT_FOUND: 404,
        ErrorCategory.TIMEOUT: 504,
        ErrorCategory.CAPACITY: 503,
        ErrorCategory.PROTOCOL: 502,
        ErrorCategory.UPSTREAM: 502,
        ErrorCategory.CANCELLED: 499,
    }
    return HTTPException(status_code=statuses[exc.category], detail=str(exc))


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
        transport=getattr(transport, "actual_name", transport.name),
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
) -> AsyncIterator[GenerationEvent]:
    generation = _generation_request(model, prompt, effort, session_id)
    await ensure_model(generation.model)
    started = time.perf_counter()
    first = True
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
        try:
            async for event in upstream:
                if event.data.get("session_rebuilt"):
                    _log(
                        "session_rebuilt",
                        transport=event.data.get("transport", transport.name),
                        reason=event.data.get(
                            "session_rebuild_reason", "unknown"
                        ),
                    )
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
                    actual_transport = event.data.get(
                        "transport", transport.name
                    )
                    _log(
                        "first_token",
                        transport=actual_transport,
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
                yield event
        finally:
            await upstream.aclose()
    finally:
        _log(
            "stream_complete",
            transport=transport.name,
            model=generation.model,
            total_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def chat_chunk(
    completion_id: str,
    created: int,
    model: str,
    content: str | None = None,
    finish_reason: str | None = None,
) -> str:
    delta = {"content": content} if content is not None else {}
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
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
) -> AsyncIterator[str]:
    try:
        async for event in _events(
            model, prompt, reasoning_effort, client_request, session_id
        ):
            if event.type is EventType.TEXT_DELTA:
                yield chat_chunk(completion_id, created, model, event.text)
            elif event.type is EventType.ERROR:
                error = {"error": {"message": event.text, "type": "api_error"}}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                break
        else:
            yield chat_chunk(completion_id, created, model, finish_reason="stop")
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
    usage = TokenUsage(input_tokens=estimate_tokens(prompt))
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
                    "input_tokens": usage.input_tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    )
    yield anthropic_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    output_parts: list[str] = []
    try:
        async for event in _events(
            anthropic_upstream_model(request),
            prompt,
            None,
            client_request,
            session_id,
        ):
            if event.type is EventType.TEXT_DELTA:
                output_parts.append(event.text)
                yield anthropic_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": event.text},
                    },
                )
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
    if usage.output_tokens <= 0:
        usage.output_tokens = estimate_tokens("".join(output_parts))
    yield anthropic_event(
        "content_block_stop", {"type": "content_block_stop", "index": 0}
    )
    yield anthropic_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": usage.output_tokens},
        },
    )
    yield anthropic_event("message_stop", {"type": "message_stop"})


async def responses_stream(
    request: ResponsesRequest,
    client_request: Request | None = None,
    session_id: str | None = None,
) -> AsyncIterator[str]:
    response_id = f"resp_{uuid.uuid4().hex}"

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
    async for item in _events(
        request.model,
        messages_to_prompt(responses_to_messages(request)),
        request.reasoning_effort,
        client_request,
        session_id,
    ):
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
    yield event(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "model": request.model,
            },
        },
    )
    yield "data: [DONE]\n\n"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await transport.start()
    try:
        yield
    finally:
        await transport.close()


app = FastAPI(title="Kiro API Proxy", version="1.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-claude-code-session-id")
        or uuid.uuid4().hex
    )
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
                "context_window": item.get("context_window_tokens"),
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
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if session_key:
        content = await call_kiro(
            request.model, prompt, request.reasoning_effort, session_key
        )
    else:
        content = await call_kiro(request.model, prompt, request.reasoning_effort)
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(content)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
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
    prompt = messages_to_prompt(responses_to_messages(request))
    call_args = (request.model, prompt, request.reasoning_effort)
    content = (
        await call_kiro(*call_args, session_key)
        if session_key
        else await call_kiro(*call_args)
    )
    response_id = f"resp_{uuid.uuid4().hex}"
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(content)
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": request.model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": content, "annotations": []}
                ],
            }
        ],
        "output_text": content,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
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
    call_args = (anthropic_upstream_model(request), prompt)
    content = (
        await call_kiro(*call_args, session_id=session_key)
        if session_key
        else await call_kiro(*call_args)
    )
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(content)
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
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
