"""直接 Runtime 传输实现。

通过 OIDC Bearer Token 调用 Kiro 数据面端点，是唯一的上游路径。
"""

from __future__ import annotations

import json
import logging
import platform
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import Settings
from ..event_mapper import map_event
from ..event_stream import EventStreamDecoder, EventStreamError
from ..runtime_credentials import CredentialLoadError, load_credentials
from ..token_provider import TokenProvider, TokenRefreshError
from .base import (
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)

logger = logging.getLogger(__name__)

REST_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"
STREAM_ENDPOINT = "https://q.us-east-1.amazonaws.com"


def _context_window_tokens(model: dict[str, Any]) -> int | None:
    """兼容不同区域/版本模型列表中的上下文窗口字段。"""
    containers = [model]
    for key in ("tokenLimits", "token_limits", "limits"):
        value = model.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in (
            "contextWindowTokens",
            "contextWindow",
            "context_window_tokens",
            "maxInputTokens",
            "inputTokenLimit",
        ):
            value = container.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


KIRO_VERSION = "0.11.107"


class RuntimeTransport:
    """直接 Runtime 传输：Bearer 认证调用 Kiro 数据面。"""

    name = "runtime"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token_provider: TokenProvider | None = None
        self._client: httpx.AsyncClient | None = None
        self._endpoint: str = ""
        self._profile_arn: str = ""

    async def start(self) -> None:
        """加载凭据，初始化 Token Provider 和 HTTP 客户端。"""
        if not self.settings.runtime_credentials_file:
            raise TransportError(
                "Runtime 凭据文件未配置（RUNTIME_CREDENTIALS_FILE）",
                ErrorCategory.AUTHENTICATION,
                retryable=False,
            )

        try:
            credentials = load_credentials(
                self.settings.runtime_credentials_file,
                getattr(self.settings, "runtime_account_index", None),
            )
        except CredentialLoadError as exc:
            raise TransportError(
                f"Runtime 凭据加载失败: {exc}",
                ErrorCategory.AUTHENTICATION,
                retryable=False,
            ) from exc

        self._token_provider = TokenProvider(
            credentials, self.settings.runtime_credentials_file
        )
        self._profile_arn = credentials.profile_arn

        # 确定端点
        if self.settings.runtime_endpoint:
            self._endpoint = self.settings.runtime_endpoint.rstrip("/")
        else:
            region = credentials.endpoint_region
            self._endpoint = (
                REST_ENDPOINT
                if region == "us-east-1"
                else f"https://q.{region}.amazonaws.com"
            )

        # 初始化 HTTP 客户端
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10,
                read=self.settings.timeout_seconds,
                write=30,
                pool=10,
            ),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
        logger.info(
            "Runtime 传输已初始化: endpoint=%s, profile=%s",
            self._endpoint,
            self._profile_arn,
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def models(self) -> list[dict[str, Any]]:
        """调用 ListAvailableModels 获取模型列表。"""
        if not self._client or not self._token_provider:
            raise TransportError(
                "Runtime 传输未初始化", ErrorCategory.PROTOCOL
            )

        token = await self._token_provider.get_token()
        url = f"{self._endpoint}/ListAvailableModels"

        try:
            resp = await self._client.get(
                url,
                params={
                    "origin": "AI_EDITOR",
                    "maxResults": "50",
                    "profileArn": self._profile_arn,
                },
                headers=self._headers(token, runtime=True),
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Runtime 模型发现网络错误: {type(exc).__name__}",
                ErrorCategory.UPSTREAM,
                retryable=True,
            ) from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise TransportError(
                f"Runtime 模型发现认证失败: HTTP {resp.status_code}",
                ErrorCategory.AUTHENTICATION,
                retryable=False,
            )
        if resp.status_code >= 400:
            raise TransportError(
                f"Runtime 模型发现失败: HTTP {resp.status_code}",
                ErrorCategory.UPSTREAM,
                retryable=resp.status_code >= 500,
            )

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise TransportError(
                "Runtime 模型列表响应格式无效",
                ErrorCategory.PROTOCOL,
            )

        # 转换为标准模型记录格式
        models: list[dict[str, Any]] = []
        raw_models = data.get("models", data.get("availableModels", []))
        if isinstance(raw_models, list):
            for item in raw_models:
                if isinstance(item, dict):
                    model_id = item.get("modelId", item.get("model_id", ""))
                    if model_id:
                        context_window = _context_window_tokens(item)
                        record = {
                            "model_id": model_id,
                            "name": item.get("modelName", item.get("name", model_id)),
                            "provider": item.get("provider", "kiro"),
                        }
                        if context_window is not None:
                            record["context_window_tokens"] = context_window
                        models.append(record)
        return models

    async def stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        """流式生成，包含首次 401 刷新重放。"""
        if not self._client or not self._token_provider:
            raise TransportError(
                "Runtime 传输未初始化", ErrorCategory.PROTOCOL
            )

        try:
            async for event in self._do_stream(request):
                yield event
        except TransportError as exc:
            # 首次 401：刷新 Token 后重放一次
            if exc.category is ErrorCategory.AUTHENTICATION and exc.retryable:
                logger.info("Runtime 401，尝试刷新 Token 后重放")
                try:
                    await self._token_provider.force_refresh()
                except TokenRefreshError as refresh_exc:
                    raise TransportError(
                        f"Token 刷新失败: {refresh_exc}",
                        ErrorCategory.AUTHENTICATION,
                        retryable=False,
                    ) from refresh_exc
                # 重放一次，不再捕获 401
                async for event in self._do_stream(request):
                    yield event
            else:
                raise

    async def _do_stream(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationEvent]:
        """执行单次流式请求。"""
        token = await self._token_provider.get_token()
        base = self._endpoint
        if not self.settings.runtime_endpoint:
            region = self._profile_arn.split(":")[3]
            base = (
                STREAM_ENDPOINT
                if region == "us-east-1"
                else f"https://q.{region}.amazonaws.com"
            )
        url = f"{base}/generateAssistantResponse"

        user_input_message: dict[str, Any] = {
            "content": request.prompt,
            "modelId": request.model,
            "origin": "AI_EDITOR",
        }
        # 结构化工具契约：填充客户端工具规格与历史工具结果。
        context: dict[str, Any] = {}
        if request.tools:
            context["tools"] = request.tools
        if request.tool_results:
            context["toolResults"] = request.tool_results
        if context:
            user_input_message["userInputMessageContext"] = context

        body = {
            "conversationState": {
                "agentContinuationId": str(uuid.uuid4()),
                "agentTaskType": "vibe",
                "chatTriggerType": "MANUAL",
                "conversationId": request.session_id or str(uuid.uuid4()),
                "currentMessage": {
                    "userInputMessage": user_input_message,
                },
            },
            "profileArn": self._profile_arn,
        }
        if request.history:
            for item in request.history:
                user_message = item.get("userInputMessage")
                if user_message is not None:
                    user_message.setdefault("modelId", request.model)
                    user_message.setdefault("origin", "AI_EDITOR")
            body["conversationState"]["history"] = request.history

        headers = self._headers(token, runtime=False)

        try:
            async with self._client.stream(
                "POST", url, json=body, headers=headers
            ) as resp:
                if resp.status_code == 401:
                    raise TransportError(
                        "Runtime 认证失败: HTTP 401",
                        ErrorCategory.AUTHENTICATION,
                        retryable=True,
                    )
                if resp.status_code == 403:
                    raise TransportError(
                        "Runtime 访问被拒: HTTP 403",
                        ErrorCategory.AUTHENTICATION,
                        retryable=False,
                    )
                if resp.status_code >= 500:
                    raise TransportError(
                        f"Runtime 上游错误: HTTP {resp.status_code}",
                        ErrorCategory.UPSTREAM,
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise TransportError(
                        f"Runtime 请求错误: HTTP {resp.status_code}",
                        ErrorCategory.UPSTREAM,
                        retryable=False,
                    )

                decoder = EventStreamDecoder()
                completed = False
                async for chunk in resp.aiter_bytes():
                    try:
                        decoder.feed(chunk)
                    except EventStreamError as exc:
                        yield GenerationEvent(
                            type=EventType.ERROR,
                            text=f"Event Stream 协议错误: {exc}",
                            data={"category": ErrorCategory.PROTOCOL.value},
                        )
                        return

                    for message in decoder.drain():
                        for event in map_event(message):
                            yield event
                            if event.type is EventType.DONE:
                                completed = True
                                return
                if not completed:
                    yield GenerationEvent(type=EventType.DONE)

        except TransportError:
            raise
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Runtime 流式请求网络错误: {type(exc).__name__}",
                ErrorCategory.UPSTREAM,
                retryable=True,
            ) from exc

    async def generate(self, request: GenerationRequest) -> str:
        """非流式生成：收集 stream() 的文本输出。"""
        parts: list[str] = []
        async for event in self.stream(request):
            if event.type is EventType.TEXT_DELTA:
                parts.append(event.text)
            elif event.type is EventType.ERROR:
                raise TransportError(
                    event.text,
                    ErrorCategory(event.data.get("category", "upstream")),
                )
        return "".join(parts)

    async def cancel(self, request_id: str) -> None:
        """取消请求（Runtime 暂不支持）。"""
        return None

    def _headers(self, token: str, *, runtime: bool) -> dict[str, str]:
        api_name = "codewhispererruntime" if runtime else "codewhispererstreaming"
        sdk_version = "1.0.0" if runtime else "1.0.34"
        system = f"{platform.system().lower()}#{platform.release()}"
        user_agent = (
            f"aws-sdk-js/{sdk_version} ua/2.1 os/{system} lang/js "
            f"md/nodejs#22.22.0 api/{api_name}#{sdk_version} m/E KiroIDE-{KIRO_VERSION}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json" if runtime else "*/*",
            "User-Agent": user_agent,
            "x-amz-user-agent": f"aws-sdk-js/{sdk_version} KiroIDE-{KIRO_VERSION}",
            "x-amzn-codewhisperer-optout": "true",
        }
        if not runtime:
            headers.update(
                {
                    "x-amzn-kiro-agent-mode": "vibe",
                    "Amz-Sdk-Request": "attempt=1; max=3",
                    "Amz-Sdk-Invocation-Id": str(uuid.uuid4()),
                }
            )
        return headers
