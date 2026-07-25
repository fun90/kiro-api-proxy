"""跨平台本机 Kiro 凭据扫描。

kiro cli / kiro ide 登录后把凭据写入 AWS SSO OIDC 缓存目录，采用两文件
配对布局：
- token 文件（如 kiro-auth-token.json）：accessToken/refreshToken/expiresAt/
  clientIdHash/authMethod/provider/region；
- client 文件（{clientIdHash}.json）：clientId/clientSecret。

两文件合并后仍缺 profile_arn，需由调用方用 refresh_token 刷新补全
（见 credentials_import.complete_profile）。

三平台的 SSO 缓存目录都是 ~/.aws/sso/cache/，Path.home() 在
Windows/macOS/Linux 均能正确解析。KIRO_SSO_CACHE_DIR 可覆盖该目录，
用于自定义位置或测试。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .credentials_import import normalize_fields


@dataclass(slots=True)
class LocalCredential:
    """一份从本机 SSO 缓存发现并配对好的凭据。"""

    id: str  # 稳定标识，用 token 文件绝对路径
    token_file: str
    client_file: str
    fields: dict  # 已归一化的 snake_case 字段（通常不含 profile_arn）

    # 供前端展示的脱敏摘要来源
    auth_method: str = ""
    provider: str = ""
    region: str = ""
    expires_at: str = ""

    def summary(self) -> dict:
        """脱敏摘要，不含任何 token/secret。"""
        return {
            "id": self.id,
            "token_file": self.token_file,
            "client_file": self.client_file,
            "auth_method": self.auth_method,
            "provider": self.provider,
            "region": self.region,
            "expires_at": self.expires_at,
            "has_client_secret": bool(self.fields.get("client_secret")),
            "has_profile_arn": bool(self.fields.get("profile_arn")),
        }


def sso_cache_dirs() -> list[Path]:
    """返回候选的 AWS SSO 缓存目录（跨平台）。"""
    override = os.getenv("KIRO_SSO_CACHE_DIR", "").strip()
    if override:
        return [Path(override).expanduser()]
    return [Path.home() / ".aws" / "sso" / "cache"]


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def scan_local_credentials() -> list[LocalCredential]:
    """扫描候选目录，返回配对好的本机凭据（按 token 文件去重）。"""
    found: list[LocalCredential] = []
    seen_ids: set[str] = set()
    for cache_dir in sso_cache_dirs():
        if not cache_dir.is_dir():
            continue
        # 预加载目录内所有 JSON，供 clientIdHash 配对。
        files: dict[str, dict] = {}
        for p in sorted(cache_dir.glob("*.json")):
            data = _load_json(p)
            if data is not None:
                files[p.name] = data

        for name, data in files.items():
            # token 文件特征：含 refreshToken（或 accessToken）。
            if not (data.get("refreshToken") or data.get("accessToken")):
                continue
            token_path = cache_dir / name
            token_id = str(token_path.resolve())
            if token_id in seen_ids:
                continue

            fields = normalize_fields(data)
            client_file = ""
            # token 文件缺 client_id 时，靠 clientIdHash 找配对的 client 文件。
            if not fields.get("client_id"):
                client_hash = str(data.get("clientIdHash") or "")
                client_data = (
                    files.get(f"{client_hash}.json") if client_hash else None
                )
                if client_data is not None:
                    client_file = str(cache_dir / f"{client_hash}.json")
                    merged = normalize_fields(client_data)
                    for key in ("client_id", "client_secret"):
                        if merged.get(key) and not fields.get(key):
                            fields[key] = merged[key]

            # 必须有 refresh_token 才能后续刷新补全 profile_arn。
            if not fields.get("refresh_token"):
                continue

            seen_ids.add(token_id)
            found.append(
                LocalCredential(
                    id=token_id,
                    token_file=str(token_path),
                    client_file=client_file,
                    fields=fields,
                    auth_method=str(data.get("authMethod", "")),
                    provider=str(data.get("provider", "")),
                    region=str(fields.get("auth_region", "")),
                    expires_at=str(data.get("expiresAt", "")),
                )
            )
    return found


def find_local_credential(cred_id: str) -> LocalCredential | None:
    """按 id（token 文件绝对路径）重新扫描并定位一份本机凭据。"""
    for cred in scan_local_credentials():
        if cred.id == cred_id:
            return cred
    return None


__all__ = [
    "LocalCredential",
    "find_local_credential",
    "scan_local_credentials",
    "sso_cache_dirs",
]
