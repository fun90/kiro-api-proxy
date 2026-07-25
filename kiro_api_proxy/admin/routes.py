"""管理界面 HTTP 路由。

挂载在 /admin/api/*，鉴权动态读取 config_store 的 api_key（与主服务
PROXY_API_KEY 同源）。凭据写入后通过 reload 钩子通知 main.py 重载
Runtime 传输，避免本模块直接依赖 transport 造成循环导入。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from ..runtime_credentials import CredentialLoadError, load_credentials
from .config_store import store
from .credentials_import import (
    CredentialImportError,
    default_credentials_path,
    import_credentials,
)
from .sso import SsoCredentials, SsoError, manager
from .usage import UsageQueryError, fetch_usage

router = APIRouter(prefix="/admin/api")

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
    runtime_credentials_file: str | None = None
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
    target_file: str = ""
    account_index: int | None = None


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
    return {
        "api_host": cfg.api_host,
        "api_port": cfg.api_port,
        "has_api_key": bool(cfg.api_key),
        "runtime_credentials_file": cfg.runtime_credentials_file,
        "runtime_account_index": cfg.runtime_account_index,
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
    # 凭据文件/账户索引变更需重载传输。
    if "runtime_credentials_file" in changes or "runtime_account_index" in changes:
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
    if not cfg.runtime_credentials_file:
        raise HTTPException(status_code=400, detail="尚未配置凭据文件")
    try:
        snapshot = await fetch_usage(
            cfg.runtime_credentials_file, cfg.runtime_account_index
        )
    except UsageQueryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post("/sso/start")
async def sso_start(
    req: SsoStartRequest,
    _: None = Depends(require_admin_auth),
) -> dict:
    try:
        session, authorize_url = await manager.start(req.start_url, req.region)
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "session_id": session.session_id,
        "authorize_url": authorize_url,
        "redirect_uri": "http://127.0.0.1/oauth/callback",
    }


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
    try:
        path, source_index = import_credentials(
            req.content, req.target_file, req.account_index
        )
    except CredentialImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 记录凭据文件路径与账户索引到 config，并重载传输。
    store.update(
        runtime_credentials_file=str(path),
        runtime_account_index=source_index,
    )
    await _trigger_reload()
    return {"success": True, "path": str(path), "source_index": source_index}


def _persist_sso_credentials(credentials: SsoCredentials) -> Path:
    """把 SSO 换取的凭据写入凭据文件并同步 config。"""
    cfg = store.get()
    target = cfg.runtime_credentials_file or str(default_credentials_path())
    payload = {
        "refresh_token": credentials.refresh_token,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "auth_region": credentials.auth_region,
        "profile_arn": credentials.profile_arn,
        "access_token": credentials.access_token,
        "expires_at": credentials.expires_at,
    }
    path, _ = import_credentials(json.dumps(payload), target)
    # SSO 写入的是单凭据对象，清除账户索引避免指向不存在的数组项。
    store.update(runtime_credentials_file=str(path), runtime_account_index=None)
    return path


__all__ = ["router", "set_reload_hook"]
