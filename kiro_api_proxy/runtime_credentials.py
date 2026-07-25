"""Runtime 凭据文件加载与管理。"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("refresh_token", "client_id", "client_secret", "auth_region", "profile_arn")


@dataclass(slots=True)
class RuntimeCredentials:
    """从 JSON 文件加载的 Runtime 凭据。"""

    refresh_token: str
    client_id: str
    client_secret: str
    auth_region: str
    profile_arn: str
    access_token: str
    expires_at: float  # Unix timestamp，0 表示未知/已过期
    source_index: int | None = None

    @property
    def endpoint_region(self) -> str:
        """从 Profile ARN 中提取数据面区域。"""
        # arn:aws:codewhisperer:<region>:<account>:profile/<id>
        parts = self.profile_arn.split(":")
        if len(parts) >= 4:
            return parts[3]
        return "us-east-1"


class CredentialLoadError(Exception):
    """凭据加载失败。"""


def _check_permissions(path: Path) -> None:
    """检查凭据文件权限，过宽时发出警告（仅 POSIX）。"""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    # 检查组和其他用户是否有读权限
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "凭据文件 %s 权限过宽（当前 %o），建议设为 0600",
            path,
            stat.S_IMODE(mode),
        )


def load_credentials(
    file_path: str, account_index: int | None = None
) -> RuntimeCredentials:
    """从 JSON 文件加载 Runtime 凭据。

    Args:
        file_path: 凭据文件路径。

    Returns:
        RuntimeCredentials 实例。

    Raises:
        CredentialLoadError: 文件不存在、格式错误或缺少必需字段。
    """
    path = Path(file_path).expanduser()

    if not path.exists():
        raise CredentialLoadError(f"凭据文件不存在: {path}")

    # POSIX 权限检查（仅警告）
    if os.name == "posix":
        _check_permissions(path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialLoadError(f"无法读取凭据文件: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialLoadError(f"凭据文件 JSON 格式无效: {exc}") from exc

    source_index: int | None = None
    if isinstance(data, list):
        candidates = [
            (index, item)
            for index, item in enumerate(data)
            if isinstance(item, dict)
            and item.get("enabled", True)
            and item.get("status", "active") == "active"
            and item.get("refreshToken")
            and item.get("clientId")
            and item.get("clientSecret")
            and item.get("profileArn")
        ]
        if account_index is not None:
            candidates = [
                (index, item)
                for index, item in candidates
                if index == account_index
            ]
        if not candidates:
            raise CredentialLoadError(
                "凭据文件内容必须是 JSON 对象，或包含指定可用账户的 JSON 数组"
            )
        source_index, source = candidates[0]
        data = {
            "refresh_token": source.get("refreshToken"),
            "client_id": source.get("clientId"),
            "client_secret": source.get("clientSecret"),
            "auth_region": source.get("region"),
            "profile_arn": source.get("profileArn"),
            "access_token": source.get("accessToken", ""),
            "expires_at": _parse_expires_at(source.get("expiresAt")),
        }
    elif not isinstance(data, dict):
        raise CredentialLoadError("凭据文件内容必须是 JSON 对象或账户数组")

    missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
    if missing:
        raise CredentialLoadError(f"凭据文件缺少必需字段: {', '.join(missing)}")

    return RuntimeCredentials(
        refresh_token=data["refresh_token"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        auth_region=data["auth_region"],
        profile_arn=data["profile_arn"],
        access_token=data.get("access_token", ""),
        expires_at=_parse_expires_at(data.get("expires_at", 0)),
        source_index=source_index,
    )


def _parse_expires_at(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        # ISO 8601（含带毫秒与 Z 的 kiro cli/ide 格式，如
        # 2026-07-25T14:10:01.899Z）。Python 3.11 的 fromisoformat 支持 Z。
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(value, pattern).timestamp()
            except ValueError:
                continue
    return 0
