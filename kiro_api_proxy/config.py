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
        api_key = os.getenv("PROXY_API_KEY", "")
        api_key_file = os.getenv("PROXY_API_KEY_FILE", "")
        if not api_key and api_key_file:
            try:
                api_key = Path(api_key_file).read_text().strip()
            except OSError:
                api_key = ""
        return cls(
            api_key=api_key,
            default_model=os.getenv("DEFAULT_MODEL", "auto"),
            timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "600")),
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
            runtime_credentials_file=os.getenv("RUNTIME_CREDENTIALS_FILE", ""),
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
