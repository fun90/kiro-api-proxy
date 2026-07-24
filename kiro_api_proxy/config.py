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
    kiro_cli: str
    api_key: str
    default_model: str
    timeout_seconds: float
    max_concurrency: int
    working_directory: str
    extra_path: tuple[str, ...]
    effort: str
    trust_tools: str
    response_language: str
    model_cache_enabled: bool
    model_cache_ttl_seconds: float
    model_cache_stale_seconds: float
    incremental_streaming: bool
    acp_enabled: bool
    acp_min_workers: int
    acp_max_workers: int
    acp_queue_size: int
    session_reuse_enabled: bool
    session_ttl_seconds: float
    session_max_entries: int
    session_max_turns: int
    session_max_context_chars: int
    session_compaction_ratio: float
    runtime_enabled: bool
    runtime_credentials_file: str
    runtime_account_index: int | None
    runtime_endpoint: str
    transport_priority: tuple[str, ...]

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
            kiro_cli=os.getenv("KIRO_CLI_PATH", "kiro-cli"),
            api_key=api_key,
            default_model=os.getenv("DEFAULT_MODEL", "auto"),
            timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "600")),
            max_concurrency=int(os.getenv("MAX_CONCURRENCY", "2")),
            working_directory=os.getenv(
                "KIRO_WORKING_DIRECTORY", str(Path.cwd())
            ),
            extra_path=tuple(
                item.strip()
                for item in os.getenv("KIRO_EXTRA_PATH", "").split(os.pathsep)
                if item.strip()
            ),
            effort=os.getenv("KIRO_EFFORT", ""),
            trust_tools=os.getenv("KIRO_TRUST_TOOLS", ""),
            response_language=os.getenv("RESPONSE_LANGUAGE", "简体中文"),
            model_cache_enabled=_bool("MODEL_CACHE_ENABLED", True),
            model_cache_ttl_seconds=float(
                os.getenv("MODEL_CACHE_TTL_SECONDS", "300")
            ),
            model_cache_stale_seconds=float(
                os.getenv("MODEL_CACHE_STALE_SECONDS", "3600")
            ),
            incremental_streaming=_bool("INCREMENTAL_STREAMING", True),
            acp_enabled=_bool("ACP_ENABLED", False),
            acp_min_workers=int(os.getenv("ACP_MIN_WORKERS", "1")),
            acp_max_workers=int(os.getenv("ACP_MAX_WORKERS", "2")),
            acp_queue_size=int(os.getenv("ACP_QUEUE_SIZE", "16")),
            session_reuse_enabled=_bool("SESSION_REUSE_ENABLED", False),
            session_ttl_seconds=float(os.getenv("SESSION_TTL_SECONDS", "1800")),
            session_max_entries=int(os.getenv("SESSION_MAX_ENTRIES", "256")),
            session_max_turns=int(os.getenv("SESSION_MAX_TURNS", "40")),
            session_max_context_chars=int(
                os.getenv("SESSION_MAX_CONTEXT_CHARS", "200000")
            ),
            session_compaction_ratio=float(
                os.getenv("SESSION_COMPACTION_RATIO", "0.7")
            ),
            runtime_enabled=_bool("RUNTIME_ENABLED", False),
            runtime_credentials_file=os.getenv("RUNTIME_CREDENTIALS_FILE", ""),
            runtime_account_index=(
                int(os.environ["RUNTIME_ACCOUNT_INDEX"])
                if os.getenv("RUNTIME_ACCOUNT_INDEX", "").strip()
                else None
            ),
            runtime_endpoint=os.getenv("RUNTIME_ENDPOINT", ""),
            transport_priority=tuple(
                item.strip()
                for item in os.getenv("TRANSPORT_PRIORITY", "acp,cli").split(",")
                if item.strip()
            ),
        )


settings = Settings.from_env()
