from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    default_model: str
    timeout_seconds: float
    heartbeat_seconds: float
    working_directory: str
    effort: str
    response_language: str
    model_cache_enabled: bool
    model_cache_ttl_seconds: float
    model_cache_stale_seconds: float
    incremental_streaming: bool
    runtime_credentials_file: str
    runtime_account_index: int | None
    runtime_endpoint: str
    default_context_window: int

    @classmethod
    def from_env(cls) -> "Settings":
        # api_key 默认为空、不再自动生成；未配置时管理接口放行，需在 /admin
        # 管理界面生成并保存后才启用鉴权。
        return cls(
            api_key=os.getenv("PROXY_API_KEY", ""),
            default_model=os.getenv("DEFAULT_MODEL", "auto"),
            timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "600")),
            # SSE 空档心跳间隔（秒）。上游长时间无输出（首 token 未到、
            # thinking）时，每隔该间隔向下游发一个 SSE 注释行，避免下游
            # 客户端读超时（SSE read timed out）。设为 0 关闭心跳。
            heartbeat_seconds=float(os.getenv("SSE_HEARTBEAT_SECONDS", "15")),
            working_directory=os.getenv(
                "KIRO_WORKING_DIRECTORY", str(Path.cwd())
            ),
            effort=os.getenv("KIRO_EFFORT", ""),
            response_language=os.getenv("RESPONSE_LANGUAGE", "简体中文"),
            model_cache_enabled=_bool("MODEL_CACHE_ENABLED", True),
            model_cache_ttl_seconds=float(
                os.getenv("MODEL_CACHE_TTL_SECONDS", "300")
            ),
            model_cache_stale_seconds=float(
                os.getenv("MODEL_CACHE_STALE_SECONDS", "3600")
            ),
            incremental_streaming=_bool("INCREMENTAL_STREAMING", True),
            # 凭据路径固定为 config.json 同目录（见 config_store.credentials_path），
            # 不再从环境变量读；此字段仅由 _reload_transport 以推导值填充。
            runtime_credentials_file="",
            runtime_account_index=(
                int(os.environ["RUNTIME_ACCOUNT_INDEX"])
                if os.getenv("RUNTIME_ACCOUNT_INDEX", "").strip()
                else None
            ),
            runtime_endpoint=os.getenv("RUNTIME_ENDPOINT", ""),
            default_context_window=int(
                os.getenv("DEFAULT_CONTEXT_WINDOW", "200000")
            ),
        )


settings = Settings.from_env()
