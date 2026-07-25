"""IAM Identity Center（企业 SSO）登录。

流程：注册 OIDC 客户端（authorization_code + refresh_token grant）→ 生成
PKCE → 构建授权 URL 交给用户浏览器 → 用户授权后回调
`http://127.0.0.1/oauth/callback?code=...&state=...` → 前端粘贴完整回调 URL
→ 用 code + code_verifier 换 token → 调 ListAvailableProfiles 补 profile_arn。
最终拼装成 RuntimeCredentials 所需的完整凭据对象。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# Kiro 使用的 CodeWhisperer scope 集合，与官方客户端一致。
SCOPES = (
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
)

REDIRECT_URI = "http://127.0.0.1/oauth/callback"
DEFAULT_START_URL = "https://view.awsapps.com/start"
SESSION_TTL_SECONDS = 600
REST_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"


class SsoError(Exception):
    """SSO 登录失败。"""


@dataclass(slots=True)
class SsoSession:
    """一次进行中的 SSO 授权会话。"""

    session_id: str
    client_id: str
    client_secret: str
    code_verifier: str
    state: str
    region: str
    start_url: str
    expires_at: float


@dataclass(slots=True)
class SsoCredentials:
    """SSO 换取并补全后的完整凭据。"""

    refresh_token: str
    client_id: str
    client_secret: str
    auth_region: str
    profile_arn: str
    access_token: str
    expires_at: float


def _oidc_base(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"


def _pkce_pair() -> tuple[str, str]:
    """生成 PKCE code_verifier 与 S256 code_challenge。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class SsoLoginManager:
    """管理进行中的 SSO 会话（内存态，带过期清理）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SsoSession] = {}

    def _cleanup(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items() if now > s.expires_at
        ]
        for sid in expired:
            self._sessions.pop(sid, None)

    async def start(
        self, start_url: str = "", region: str = "us-east-1"
    ) -> tuple[SsoSession, str]:
        """发起登录：注册客户端、生成 PKCE、返回会话与授权 URL。"""
        self._cleanup()
        region = region or "us-east-1"
        start_url = start_url or DEFAULT_START_URL
        oidc_base = _oidc_base(region)

        async with httpx.AsyncClient(timeout=30) as client:
            client_id, client_secret = await self._register_client(
                client, oidc_base, start_url
            )

        verifier, challenge = _pkce_pair()
        state = str(uuid.uuid4())
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scopes": ",".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{oidc_base}/authorize?{urlencode(params)}"

        session = SsoSession(
            session_id=str(uuid.uuid4()),
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=verifier,
            state=state,
            region=region,
            start_url=start_url,
            expires_at=time.time() + SESSION_TTL_SECONDS,
        )
        self._sessions[session.session_id] = session
        return session, authorize_url

    async def complete(
        self, session_id: str, callback_url: str
    ) -> SsoCredentials:
        """完成登录：校验回调、换 token、补 profile_arn。"""
        self._cleanup()
        session = self._sessions.get(session_id)
        if session is None:
            raise SsoError("会话不存在或已过期")
        if time.time() > session.expires_at:
            self._sessions.pop(session_id, None)
            raise SsoError("会话已过期，请重新发起登录")

        code, state = _parse_callback(callback_url)
        if state != session.state:
            raise SsoError("state 不匹配，可能存在安全风险")

        oidc_base = _oidc_base(session.region)
        async with httpx.AsyncClient(timeout=30) as client:
            access_token, refresh_token, expires_in = await self._exchange_token(
                client, oidc_base, session, code
            )
            profile_arn = await self._first_profile_arn(
                client, session.region, access_token
            )

        self._sessions.pop(session_id, None)
        return SsoCredentials(
            refresh_token=refresh_token,
            client_id=session.client_id,
            client_secret=session.client_secret,
            auth_region=session.region,
            profile_arn=profile_arn,
            access_token=access_token,
            expires_at=time.time() + expires_in,
        )

    async def _register_client(
        self, client: httpx.AsyncClient, oidc_base: str, start_url: str
    ) -> tuple[str, str]:
        payload = {
            "clientName": "Kiro API Proxy",
            "clientType": "public",
            "scopes": list(SCOPES),
            "grantTypes": ["authorization_code", "refresh_token"],
            "redirectUris": [REDIRECT_URI],
            "issuerUrl": start_url,
        }
        try:
            resp = await client.post(
                f"{oidc_base}/client/register",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SsoError(f"注册客户端网络错误: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SsoError(f"注册客户端失败: HTTP {resp.status_code}")
        data = resp.json()
        client_id = data.get("clientId", "")
        client_secret = data.get("clientSecret", "")
        if not client_id or not client_secret:
            raise SsoError("注册客户端响应缺少 clientId/clientSecret")
        return client_id, client_secret

    async def _exchange_token(
        self,
        client: httpx.AsyncClient,
        oidc_base: str,
        session: SsoSession,
        code: str,
    ) -> tuple[str, str, int]:
        payload = {
            "clientId": session.client_id,
            "clientSecret": session.client_secret,
            "grantType": "authorization_code",
            "redirectUri": REDIRECT_URI,
            "code": code,
            "codeVerifier": session.code_verifier,
        }
        try:
            resp = await client.post(
                f"{oidc_base}/token",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SsoError(f"换取 Token 网络错误: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SsoError(f"换取 Token 失败: HTTP {resp.status_code}")
        data = resp.json()
        access_token = data.get("accessToken", "")
        refresh_token = data.get("refreshToken", "")
        expires_in = int(data.get("expiresIn", 3600))
        if not access_token or not refresh_token:
            raise SsoError("Token 响应缺少 accessToken/refreshToken")
        return access_token, refresh_token, expires_in

    async def _first_profile_arn(
        self, client: httpx.AsyncClient, region: str, access_token: str
    ) -> str:
        """调用 ListAvailableProfiles 取第一个可用 profile ARN。"""
        endpoint = (
            REST_ENDPOINT
            if region == "us-east-1"
            else f"https://q.{region}.amazonaws.com"
        )
        try:
            resp = await client.post(
                f"{endpoint}/ListAvailableProfiles",
                json={"maxResults": 50},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SsoError(
                f"获取 Profile 网络错误: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise SsoError(
                f"获取 Profile 失败: HTTP {resp.status_code}，"
                "可尝试改用粘贴凭据 JSON 方式"
            )
        data = resp.json()
        profiles = data.get("profiles", [])
        if not isinstance(profiles, list) or not profiles:
            raise SsoError("账户下没有可用的 Kiro Profile")
        arn = profiles[0].get("arn", "")
        if not arn:
            raise SsoError("Profile 响应缺少 ARN")
        return arn


def _parse_callback(callback_url: str) -> tuple[str, str]:
    """从回调 URL 中解析 code 与 state。"""
    try:
        parsed = urlparse(callback_url.strip())
    except ValueError as exc:
        raise SsoError("无效的回调 URL") from exc
    query = parse_qs(parsed.query)
    error = query.get("error", [""])[0]
    if error:
        raise SsoError(f"授权失败: {error}")
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code:
        raise SsoError("回调 URL 中未包含授权码 code")
    return code, state


manager = SsoLoginManager()

__all__ = [
    "SsoCredentials",
    "SsoError",
    "SsoLoginManager",
    "SsoSession",
    "manager",
]
