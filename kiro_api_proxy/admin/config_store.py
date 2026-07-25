"""可写配置存储。

管理界面在线修改的配置写入独立的 config.json，优先级高于 .env；
.env（经 Settings 读取）仅作为首次启动的默认值来源。这样界面改动
可持久化，又不会破坏用户手写的 .env 注释与格式。
"""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import settings


def _default_config_dir() -> Path:
    """config 目录：默认安装目录下的 .config/，可用 KIRO_PROXY_CONFIG_DIRECTORY 覆盖。"""
    override = os.getenv("KIRO_PROXY_CONFIG_DIRECTORY", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd() / ".config"


def _default_config_path() -> Path:
    """config.json 默认路径：config 目录下的 config.json。"""
    return _default_config_dir() / "config.json"


# 界面可编辑且需持久化的字段。值为 None 表示尚未显式设置，读取时回退到 .env 默认。
_PERSISTED_FIELDS = (
    "api_host",
    "api_port",
    "api_key",
    "runtime_account_index",
)


@dataclass(slots=True)
class AdminConfig:
    """管理界面可见的有效配置（已合并 .env 默认值）。"""

    api_host: str
    api_port: int
    api_key: str
    runtime_credentials_file: str
    runtime_account_index: int | None


class ConfigStore:
    """线程安全的 config.json 读写层。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_config_path()
        self._lock = threading.RLock()
        # 仅保存界面显式写入的键；未写入的键在读取时回退 .env 默认。
        self._overrides: dict[str, object] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def credentials_path(self) -> Path:
        """凭据文件固定在 config.json 同目录下的 runtime-credentials.json。

        不再可配置：程序（SSO/导入/刷新回写）统一读写这个位置，与 config.json
        同目录，随 KIRO_PROXY_CONFIG_DIRECTORY 一起移动。
        """
        return self._path.parent / "runtime-credentials.json"

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self._overrides = {
                key: value
                for key, value in data.items()
                if key in _PERSISTED_FIELDS and value is not None
            }

    def _env_defaults(self) -> dict[str, object]:
        """从 Settings（.env）派生的默认值。"""
        return {
            "api_host": os.getenv("PROXY_HOST", "127.0.0.1"),
            "api_port": int(os.getenv("PROXY_PORT", "3458")),
            "api_key": settings.api_key,
            "runtime_account_index": settings.runtime_account_index,
        }

    def get(self) -> AdminConfig:
        """返回合并 .env 默认后的有效配置。"""
        with self._lock:
            merged = self._env_defaults()
            merged.update(self._overrides)
            return AdminConfig(
                api_host=str(merged["api_host"]),
                api_port=int(merged["api_port"]),
                api_key=str(merged["api_key"] or ""),
                # 凭据路径固定为 config.json 同目录，不再可配置。
                runtime_credentials_file=str(self.credentials_path),
                runtime_account_index=(
                    int(merged["runtime_account_index"])
                    if merged["runtime_account_index"] is not None
                    else None
                ),
            )

    def update(self, **changes: object) -> AdminConfig:
        """更新给定字段并原子写回。传入 None 表示恢复为 .env 默认。"""
        with self._lock:
            for key, value in changes.items():
                if key not in _PERSISTED_FIELDS:
                    raise KeyError(f"未知配置字段: {key}")
                if value is None:
                    self._overrides.pop(key, None)
                else:
                    self._overrides[key] = value
            self._write()
            return self.get()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self._overrides, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # config.json 含 api_key，收紧权限（仅 POSIX 生效）。
        if os.name == "posix":
            temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temp_path.replace(self._path)


store = ConfigStore()

__all__ = ["AdminConfig", "ConfigStore", "store", "asdict"]
