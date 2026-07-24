"""OIDC Token 自动刷新 Provider。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .runtime_credentials import RuntimeCredentials

logger = logging.getLogger(__name__)

# Token 过期前提前刷新的秒数
REFRESH_BUFFER_SECONDS = 60


class TokenRefreshError(Exception):
    """Token 刷新失败。"""


class TokenProvider:
    """管理 Access Token 生命周期：过期检测、单航班刷新、文件回写。"""

    def __init__(self, credentials: RuntimeCredentials, credentials_file: str) -> None:
        self._credentials = credentials
        self._credentials_file = credentials_file
        self._lock = asyncio.Lock()
        self._access_token = credentials.access_token
        self._expires_at = credentials.expires_at

    @property
    def access_token(self) -> str:
        return self._access_token

    def is_expired(self) -> bool:
        """检查 Token 是否过期或即将过期。"""
        if not self._access_token:
            return True
        return time.time() >= (self._expires_at - REFRESH_BUFFER_SECONDS)

    async def get_token(self) -> str:
        """获取有效的 Access Token，必要时触发刷新。"""
        if not self.is_expired():
            return self._access_token
        return await self._refresh()

    async def force_refresh(self) -> str:
        """强制刷新 Token（用于 401 重放场景）。"""
        return await self._refresh(force=True)

    async def _refresh(self, force: bool = False) -> str:
        """单航班刷新：多个并发调用只发送一次 HTTP 请求。"""
        async with self._lock:
            # 双重检查：其他等待者可能已完成刷新（force 时跳过）
            if not force and not self.is_expired():
                return self._access_token

            token_endpoint = (
                f"https://oidc.{self._credentials.auth_region}.amazonaws.com/token"
            )
            payload = {
                "grantType": "refresh_token",
                "clientId": self._credentials.client_id,
                "clientSecret": self._credentials.client_secret,
                "refreshToken": self._credentials.refresh_token,
            }

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        token_endpoint,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Token 刷新失败: HTTP %d, endpoint=%s",
                    exc.response.status_code,
                    token_endpoint,
                )
                raise TokenRefreshError(
                    f"OIDC 刷新失败: HTTP {exc.response.status_code}"
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("Token 刷新网络错误: %s", type(exc).__name__)
                raise TokenRefreshError(
                    f"OIDC 刷新网络错误: {type(exc).__name__}"
                ) from exc

            self._access_token = data.get("accessToken", data.get("access_token", ""))
            if not self._access_token:
                raise TokenRefreshError("OIDC 刷新响应缺少 Access Token")
            expires_in = data.get("expiresIn", data.get("expires_in", 3600))
            self._expires_at = time.time() + expires_in

            # 更新内部凭据（供回写）
            new_refresh = data.get(
                "refreshToken",
                data.get("refresh_token", self._credentials.refresh_token),
            )
            self._credentials.refresh_token = new_refresh
            self._credentials.access_token = self._access_token
            self._credentials.expires_at = self._expires_at

            # 回写文件
            await self._write_credentials()

            logger.info(
                "Token 刷新成功，有效期 %ds",
                expires_in,
            )
            return self._access_token

    async def _write_credentials(self) -> None:
        """将更新后的凭据回写到文件。"""
        path = Path(self._credentials_file).expanduser()
        try:
            if self._credentials.source_index is None:
                data: object = {
                    "refresh_token": self._credentials.refresh_token,
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "auth_region": self._credentials.auth_region,
                    "profile_arn": self._credentials.profile_arn,
                    "access_token": self._credentials.access_token,
                    "expires_at": self._credentials.expires_at,
                }
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                account = data[self._credentials.source_index]
                account["refreshToken"] = self._credentials.refresh_token
                account["accessToken"] = self._credentials.access_token
                account["expiresAt"] = self._credentials.expires_at
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temp_path.chmod(path.stat().st_mode & 0o777)
            temp_path.replace(path)
        except OSError as exc:
            logger.warning("凭据回写失败: %s", exc)
