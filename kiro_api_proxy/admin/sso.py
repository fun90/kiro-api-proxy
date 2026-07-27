"""IAM Identity Center（企业 SSO）登录。

流程：注册 OIDC 客户端（authorization_code + refresh_token grant）→ 生成
PKCE → 构建授权 URL 交给用户浏览器 → 用户授权后回调
`<redirect_uri>?code=...&state=...` → 用 code + code_verifier 换 token → 调
ListAvailableProfiles 补 profile_arn。最终拼装成 RuntimeCredentials 所需的
完整凭据对象。

回调有两种落地方式：
- 自动：管理界面经 loopback（127.0.0.1/localhost）访问时，redirect_uri 指向
  正在运行的本服务 `/oauth/callback`，浏览器授权后直接回到服务端自动完成，
  前端轮询会话状态即可。
- 手动兜底：非 loopback（局域网 IP/远程）访问时，AWS 只允许 loopback 用明文
  http 回调，redirect_uri 退回固定的 `http://127.0.0.1/oauth/callback`，由用户
  复制回调 URL 粘贴回界面完成。
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

from ..transports.runtime import kiro_headers

# Kiro 使用的 CodeWhisperer scope 集合，与官方客户端一致。
SCOPES = (
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
)

# loopback 兜底回调地址：非 loopback 访问（局域网/远程）时用它，配合手动
# 粘贴回调 URL 完成登录。AWS SSO OIDC 只允许 loopback 用明文 http 回调。
FALLBACK_REDIRECT_URI = "http://127.0.0.1/oauth/callback"
DEFAULT_START_URL = "https://view.awsapps.com/start"
SESSION_TTL_SECONDS = 600
REST_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"
# ListAvailableProfiles 探测区域：profile 通常在 us-east-1 或 eu-central-1，
# 与登录区域可能不同（详见 runtime-support 文档的区域说明）。
_PROFILE_REGION_CANDIDATES = ("us-east-1", "eu-central-1")


class SsoError(Exception):
    """SSO 登录失败。"""


@dataclass(slots=True)
class SsoSession:
    """一次进行中的 SSO 授权会话。

    status/result_* 供自动回调流程使用：回调端点换取凭据后把结果写回会话，
    前端轮询 poll() 读取。手动粘贴流程不经过这些字段，直接返回凭据。
    """

    session_id: str
    client_id: str
    client_secret: str
    code_verifier: str
    state: str
    region: str
    start_url: str
    redirect_uri: str
    expires_at: float
    # pending（进行中）/ success（已完成）/ error（失败），仅自动回调流程更新。
    status: str = "pending"
    result_path: str = ""
    result_profile_arn: str = ""
    error: str = ""


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
        self,
        start_url: str = "",
        region: str = "us-east-1",
        redirect_uri: str = "",
    ) -> tuple[SsoSession, str]:
        """发起登录：注册客户端、生成 PKCE、返回会话与授权 URL。

        redirect_uri 为空时回退到 loopback 兜底地址（配合手动粘贴回调 URL）；
        传入指向本服务的 loopback 回调地址时即启用自动完成流程。它会写入
        会话并在注册客户端、构建授权 URL、换取 token 三处保持一致（AWS 会
        在换 token 时校验 redirect_uri 与注册/授权时一致）。
        """
        self._cleanup()
        region = region or "us-east-1"
        start_url = start_url or DEFAULT_START_URL
        redirect_uri = redirect_uri or FALLBACK_REDIRECT_URI
        oidc_base = _oidc_base(region)

        async with httpx.AsyncClient(timeout=30) as client:
            client_id, client_secret = await self._register_client(
                client, oidc_base, start_url, redirect_uri
            )

        verifier, challenge = _pkce_pair()
        state = str(uuid.uuid4())
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
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
            redirect_uri=redirect_uri,
            expires_at=time.time() + SESSION_TTL_SECONDS,
        )
        self._sessions[session.session_id] = session
        return session, authorize_url

    def session_by_state(self, state: str) -> SsoSession | None:
        """按 state 查会话，供自动回调流程使用（回调只带 code/state）。"""
        self._cleanup()
        for session in self._sessions.values():
            if session.state == state:
                return session
        return None

    def poll(self, session_id: str) -> dict:
        """返回会话当前状态，供前端轮询自动回调进度。

        终态（success/error）读到后即清理会话；pending 保留继续等待。
        会话不存在时返回 not_found（可能已被读走或过期）。
        """
        self._cleanup()
        session = self._sessions.get(session_id)
        if session is None:
            return {"status": "not_found"}
        if session.status == "success":
            self._sessions.pop(session_id, None)
            return {
                "status": "success",
                "path": session.result_path,
                "profile_arn": session.result_profile_arn,
            }
        if session.status == "error":
            self._sessions.pop(session_id, None)
            return {"status": "error", "error": session.error}
        return {"status": "pending"}

    def mark_success(self, session: SsoSession, path: str, profile_arn: str) -> None:
        """自动回调换取成功后写回结果，等待前端轮询读取。"""
        session.status = "success"
        session.result_path = path
        session.result_profile_arn = profile_arn

    def mark_error(self, session: SsoSession, error: str) -> None:
        """自动回调失败后写回错误，等待前端轮询读取。"""
        session.status = "error"
        session.error = error

    def discard(self, session_id: str) -> None:
        """丢弃会话（手动流程成功后清理）。"""
        self._sessions.pop(session_id, None)

    async def complete(
        self, session_id: str, callback_url: str
    ) -> SsoCredentials:
        """完成登录（手动粘贴流程）：校验回调、换 token、补 profile_arn。"""
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

        credentials = await self.exchange(session, code)
        self._sessions.pop(session_id, None)
        return credentials

    async def exchange(self, session: SsoSession, code: str) -> SsoCredentials:
        """用授权码换 token 并补 profile_arn，拼装完整凭据。

        手动流程（complete）与自动回调共用；不改动会话字典，会话生命周期由
        调用方按各自流程管理。
        """
        oidc_base = _oidc_base(session.region)
        async with httpx.AsyncClient(timeout=30) as client:
            access_token, refresh_token, expires_in = await self._exchange_token(
                client, oidc_base, session, code
            )
            # profile_arn 是必需字段，拿不到就让 SsoError 冒泡，由路由层返回
            # 清晰的 400，而不是吞成空值后在写入阶段抛未捕获异常变成 500。
            profile_arn = await self._first_profile_arn(
                client, access_token, session.region
            )

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
        self,
        client: httpx.AsyncClient,
        oidc_base: str,
        start_url: str,
        redirect_uri: str,
    ) -> tuple[str, str]:
        payload = {
            "clientName": "Kiro API Proxy",
            "clientType": "public",
            "scopes": list(SCOPES),
            "grantTypes": ["authorization_code", "refresh_token"],
            "redirectUris": [redirect_uri],
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
            # 带上 AWS 返回体（截断），便于定位 redirect_uri/scope 等校验失败原因。
            raise SsoError(
                f"注册客户端失败: HTTP {resp.status_code} {resp.text[:300]}"
            )
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
            # 必须与注册/授权时一致，否则 AWS 换 token 会拒绝。
            "redirectUri": session.redirect_uri,
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
        self, client: httpx.AsyncClient, access_token: str, region_hint: str = ""
    ) -> str:
        """跨候选区域调用 ListAvailableProfiles，返回第一个可用 profile ARN。

        profile 可能不在登录区域，优先尝试凭据自身 region，再退回
        us-east-1 / eu-central-1。请求必须带完整 Kiro 合规头，否则
        CodeWhisperer 端点会对缺头请求返回 HTTP 400。单个区域网络失败或
        返回非 200 时继续尝试下一个，全部失败才报错。
        """
        candidates: list[str] = []
        for region in (region_hint, *_PROFILE_REGION_CANDIDATES):
            region = (region or "").strip()
            if region and region not in candidates:
                candidates.append(region)
        last_error = ""
        for region in candidates:
            endpoint = (
                REST_ENDPOINT
                if region == "us-east-1"
                else f"https://q.{region}.amazonaws.com"
            )
            try:
                resp = await client.post(
                    f"{endpoint}/ListAvailableProfiles",
                    # CodeWhisperer 只接受空 body；带 maxResults 会被判
                    # REQUEST_BODY_INVALID（HTTP 400）。
                    json={},
                    headers=kiro_headers(access_token, runtime=True),
                )
            except httpx.HTTPError as exc:
                last_error = f"{region}: {type(exc).__name__}"
                continue
            if resp.status_code != 200:
                # 带上上游返回体（截断），便于定位区域不支持/校验失败/鉴权。
                last_error = f"{region}: HTTP {resp.status_code} {resp.text[:300]}"
                continue
            profiles = resp.json().get("profiles", [])
            if isinstance(profiles, list) and profiles:
                arn = profiles[0].get("arn", "")
                if arn:
                    return arn
            last_error = f"{region}: 返回空 profile 列表"
        raise SsoError(
            f"获取 Profile 失败（{last_error or '账户下无 profile'}），"
            "可尝试改用从本机 Kiro 登录导入方式"
        )


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
