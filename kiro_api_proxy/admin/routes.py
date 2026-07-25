"""管理界面 HTTP 路由。

大部分挂载在 /admin/api/*，鉴权动态读取 config_store 的 api_key（与主服务
PROXY_API_KEY 同源）。凭据写入后通过 reload 钩子通知 main.py 重载
Runtime 传输，避免本模块直接依赖 transport 造成循环导入。

例外：SSO 自动回调端点必须挂在根路径 /oauth/callback（见 public_router）。
AWS SSO OIDC 对 loopback 回调路径做白名单校验，实测只接受精确等于
/oauth/callback 的路径（/admin/api/sso/callback 会被拒 400
invalid_redirect_uri），所以它不能带 /admin/api 前缀。
"""

from __future__ import annotations

import html
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from ..runtime_credentials import CredentialLoadError, load_credentials
from .config_store import store
from .credentials_import import (
    CredentialImportError,
    complete_profile,
    import_credentials,
    import_credentials_completing,
    write_credentials,
)
from .local_import import find_local_credential, scan_local_credentials
from .sso import SsoCredentials, SsoError, manager
from .usage import UsageQueryError, fetch_usage

router = APIRouter(prefix="/admin/api")
# SSO 自动回调专用：无前缀，回调端点必须精确挂在根路径 /oauth/callback
# （AWS SSO OIDC 对 loopback redirect_uri 的白名单约束，见模块 docstring）。
public_router = APIRouter()

# 凭据/配置变更后触发的重载钩子（由 main.py 注入）。
_reload_hook: Callable[[], Awaitable[None]] | None = None


def set_reload_hook(hook: Callable[[], Awaitable[None]]) -> None:
    """注册 Runtime 传输重载钩子。"""
    global _reload_hook
    _reload_hook = hook


async def _trigger_reload() -> None:
    if _reload_hook is not None:
        await _reload_hook()


def require_admin_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """管理接口鉴权：api_key 未配置时放行（本地初始化场景）。"""
    api_key = store.get().api_key
    if not api_key:
        return
    if authorization != f"Bearer {api_key}" and x_api_key != api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "无效的 API Key", "type": "auth_error"}},
        )


def _platform_info() -> dict:
    """运行环境与客户端配置文件位置，供界面提示「粘贴到哪个文件」。

    前端 navigator 检测不可靠（userAgentData 在非安全上下文缺失、platform
    已被部分浏览器冻结或伪装），改由服务端按 sys.platform 判定，并给出
    Path.home() 展开后的绝对路径，避免用户再去猜 ~ 或 %USERPROFILE%。
    """
    home = Path.home()
    if sys.platform.startswith("win"):
        return {
            "os": "windows",
            "label": "Windows",
            "claude_config_path": str(home / ".claude" / "settings.json"),
            "codex_config_path": str(home / ".codex" / "config.toml"),
            # PowerShell 下设置环境变量的写法；{key} 由前端替换为实际密钥。
            "export_command": '$env:KIRO_API_KEY = "{key}"',
        }
    darwin = sys.platform == "darwin"
    return {
        "os": "macos" if darwin else "linux",
        "label": "macOS" if darwin else "Linux",
        "claude_config_path": str(home / ".claude" / "settings.json"),
        "codex_config_path": str(home / ".codex" / "config.toml"),
        "export_command": 'export KIRO_API_KEY="{key}"',
    }


def _credentials_info(path_str: str, account_index: int | None) -> dict:
    """读取当前凭据文件的脱敏信息，供界面展示登录配置。"""
    if not path_str:
        return {"configured": False}
    path = Path(path_str).expanduser()
    if not path.exists():
        return {"configured": False, "path": str(path), "error": "凭据文件不存在"}
    try:
        credentials = load_credentials(path_str, account_index)
    except CredentialLoadError as exc:
        return {"configured": True, "path": str(path), "error": str(exc)}
    return {
        "configured": True,
        "path": str(path),
        "profile_arn": credentials.profile_arn,
        "auth_region": credentials.auth_region,
        "endpoint_region": credentials.endpoint_region,
        "expires_at": credentials.expires_at,
        "source_index": credentials.source_index,
    }


# ---- 请求体模型 ----


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_host: str | None = None
    api_port: int | None = None
    api_key: str | None = None
    # 凭据路径固定为 config.json 同目录，不再可配置；账户索引仍可调。
    runtime_account_index: int | None = None


class SsoStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_url: str = ""
    region: str = "us-east-1"


class SsoCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    callback_url: str


class CredentialsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    account_index: int | None = None


class LocalImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str  # 本机凭据标识（token 文件绝对路径，来自 /credentials/scan）


# ---- 端点 ----


@router.get("/status")
async def status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    """服务状态与鉴权要求。放行以便登录页判断是否需要 API Key。"""
    cfg = store.get()
    require_auth = bool(cfg.api_key)
    # 已配置鉴权时，未带正确 key 只返回最小信息。
    authed = not require_auth or authorization == f"Bearer {cfg.api_key}" or (
        x_api_key == cfg.api_key
    )
    result = {
        "requires_auth": require_auth,
        "authenticated": authed,
        "api_host": cfg.api_host,
        "api_port": cfg.api_port,
    }
    if authed:
        result["credentials"] = _credentials_info(
            cfg.runtime_credentials_file, cfg.runtime_account_index
        )
    return result


@router.get("/settings")
async def get_settings(
    _: None = Depends(require_admin_auth),
) -> dict:
    cfg = store.get()
    # 本端点受 require_admin_auth 保护，回显真实 api_key 供设置页「小眼睛」查看；
    # 未设置时为空串。
    return {
        "api_host": cfg.api_host,
        "api_port": cfg.api_port,
        "api_key": cfg.api_key,
        "has_api_key": bool(cfg.api_key),
        "runtime_account_index": cfg.runtime_account_index,
        "platform": _platform_info(),
    }


@router.post("/settings")
async def update_settings(
    patch: SettingsPatch,
    _: None = Depends(require_admin_auth),
) -> dict:
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        return {"success": True, "changed": []}
    cfg = store.update(**changes)
    # 账户索引变更需重载传输（凭据路径固定，不参与配置变更）。
    if "runtime_account_index" in changes:
        await _trigger_reload()
    return {
        "success": True,
        "changed": list(changes.keys()),
        "restart_required": "api_host" in changes or "api_port" in changes,
        "api_host": cfg.api_host,
        "api_port": cfg.api_port,
    }


@router.get("/usage")
async def get_usage(
    _: None = Depends(require_admin_auth),
) -> dict:
    cfg = store.get()
    # 凭据路径固定（config.json 同目录），改为检查文件是否已生成。
    if not Path(cfg.runtime_credentials_file).exists():
        raise HTTPException(status_code=400, detail="尚未配置凭据，请先登录或导入")
    try:
        snapshot = await fetch_usage(
            cfg.runtime_credentials_file, cfg.runtime_account_index
        )
    except UsageQueryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return snapshot.to_dict()


def _loopback_callback_uri(request: Request) -> str:
    """若管理界面经 loopback 访问，返回指向本服务的自动回调地址，否则空串。

    AWS SSO OIDC 只允许 loopback（127.0.0.1/localhost/::1）用明文 http 回调，
    且回调路径必须精确等于 /oauth/callback（实测其它路径一律 400
    invalid_redirect_uri，端口可带），所以回调端点挂在主 app 根路径而非
    admin 前缀下。非 loopback（局域网 IP/远程）返回空串，触发手动粘贴兜底。
    """
    host = (request.url.hostname or "").strip()
    is_loopback = (
        host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")
    )
    if not is_loopback:
        return ""
    # base_url 形如 http://127.0.0.1:3458/，去掉末尾斜杠后拼固定回调路径。
    base = str(request.base_url).rstrip("/")
    return f"{base}/oauth/callback"


def _callback_page(message: str, *, ok: bool) -> HTMLResponse:
    """渲染回调结果提示页（纯静态，message 中的动态部分须由调用方转义）。"""
    color = "#0a7d33" if ok else "#c0392b"
    title = "登录成功" if ok else "登录未完成"
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title></head>"
        '<body style="font-family:system-ui,-apple-system,sans-serif;'
        'max-width:32rem;margin:4rem auto;padding:0 1rem;text-align:center">'
        f'<h1 style="color:{color};font-size:1.25rem">{title}</h1>'
        f'<p style="color:#333;line-height:1.6">{message}</p>'
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=200 if ok else 400)


@router.post("/sso/start")
async def sso_start(
    req: SsoStartRequest,
    request: Request,
    _: None = Depends(require_admin_auth),
) -> dict:
    redirect_uri = _loopback_callback_uri(request)
    try:
        session, authorize_url = await manager.start(
            req.start_url, req.region, redirect_uri
        )
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "session_id": session.session_id,
        "authorize_url": authorize_url,
        "redirect_uri": session.redirect_uri,
        # auto=True：前端轮询 /sso/poll 自动完成；否则回退手动粘贴回调 URL。
        "auto": bool(redirect_uri),
    }


@public_router.get("/oauth/callback", response_class=HTMLResponse)
async def sso_callback(
    code: str = "",
    state: str = "",
    error: str = "",
) -> HTMLResponse:
    """SSO 自动回调落地端点（redirect_uri 指向此处）。

    路径必须精确为 /oauth/callback（AWS 白名单约束，见模块 docstring），故挂在
    无前缀的 public_router 上。浏览器授权后被重定向到这里，带 code/state。不走
    admin 鉴权（重定向无法携带 x-api-key），改用 state 匹配服务端会话防伪。换取
    凭据后落盘并热重载，结果写回会话供前端 /sso/poll 轮询，最后返回可关闭的提示页。
    """
    session = manager.session_by_state(state) if state else None
    if session is None:
        return _callback_page(
            "登录会话不存在或已过期，请回到管理界面重新发起登录。", ok=False
        )
    if error:
        manager.mark_error(session, f"授权失败: {error}")
        return _callback_page(f"授权失败：{html.escape(error)}", ok=False)
    if not code:
        manager.mark_error(session, "回调缺少授权码 code")
        return _callback_page("回调缺少授权码，请重新发起登录。", ok=False)
    try:
        credentials = await manager.exchange(session, code)
        path = _persist_sso_credentials(credentials)
        await _trigger_reload()
    except SsoError as exc:
        manager.mark_error(session, str(exc))
        return _callback_page(f"换取凭据失败：{html.escape(str(exc))}", ok=False)
    manager.mark_success(session, str(path), credentials.profile_arn)
    return _callback_page(
        "登录成功，凭据已保存，可关闭此页面并返回管理界面。", ok=True
    )


@router.get("/sso/poll")
async def sso_poll(
    session_id: str,
    _: None = Depends(require_admin_auth),
) -> dict:
    """供前端轮询自动回调进度：pending/success/error/not_found。"""
    return manager.poll(session_id)


@router.post("/sso/complete")
async def sso_complete(
    req: SsoCompleteRequest,
    _: None = Depends(require_admin_auth),
) -> dict:
    try:
        credentials = await manager.complete(req.session_id, req.callback_url)
    except SsoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    path = _persist_sso_credentials(credentials)
    await _trigger_reload()
    return {"success": True, "path": str(path), "profile_arn": credentials.profile_arn}


@router.post("/credentials/import")
async def credentials_import(
    req: CredentialsImportRequest,
    _: None = Depends(require_admin_auth),
) -> dict:
    # 单对象缺 profile_arn 时（kiro cli/ide 凭据）用 refresh_token 刷新补全。
    try:
        path, source_index = await import_credentials_completing(
            req.content, str(store.credentials_path), req.account_index
        )
    except CredentialImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 凭据路径固定（config.json 同目录），仅记录账户索引并重载传输。
    store.update(runtime_account_index=source_index)
    await _trigger_reload()
    return {"success": True, "path": str(path), "source_index": source_index}


@router.get("/credentials/scan")
async def credentials_scan(
    _: None = Depends(require_admin_auth),
) -> dict:
    """扫描本机 AWS SSO 缓存，返回可导入的 kiro cli/ide 凭据（脱敏摘要）。"""
    creds = scan_local_credentials()
    return {"credentials": [c.summary() for c in creds]}


@router.post("/credentials/import-local")
async def credentials_import_local(
    req: LocalImportRequest,
    _: None = Depends(require_admin_auth),
) -> dict:
    """一键导入指定的本机凭据：配对两文件、刷新补全 profile_arn 后写入。"""
    cred = find_local_credential(req.id)
    if cred is None:
        raise HTTPException(status_code=404, detail="未找到该本机凭据，请重新扫描")
    try:
        fields = cred.fields
        if not fields.get("profile_arn"):
            fields = await complete_profile(fields)
        path, _index = write_credentials(fields, str(store.credentials_path))
    except CredentialImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 本机导入写入单凭据对象，清除账户索引。
    store.update(runtime_account_index=None)
    await _trigger_reload()
    return {"success": True, "path": str(path), "profile_arn": fields.get("profile_arn", "")}


def _persist_sso_credentials(credentials: SsoCredentials) -> Path:
    """把 SSO 换取的凭据写入固定凭据文件并同步 config。"""
    payload = {
        "refresh_token": credentials.refresh_token,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "auth_region": credentials.auth_region,
        "profile_arn": credentials.profile_arn,
        "access_token": credentials.access_token,
        "expires_at": credentials.expires_at,
    }
    path, _ = import_credentials(json.dumps(payload), str(store.credentials_path))
    # SSO 写入的是单凭据对象，清除账户索引避免指向不存在的数组项。
    store.update(runtime_account_index=None)
    return path


__all__ = ["public_router", "router", "set_reload_hook"]
