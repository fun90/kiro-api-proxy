"""凭据导入。

支持三类来源，最终都归一化为 RuntimeCredentials 所需的 snake_case 字段：
1. 粘贴/上传的凭据 JSON（snake_case 或 camelCase 单对象、Kiro Account
   Manager 账户数组）；
2. kiro cli / kiro ide 的 AWS SSO 缓存（token 文件 + client 文件配对，
   见 local_import）。

写入前先在临时文件上用 load_credentials 完整校验，通过后再原子替换目标
文件，避免坏凭据覆盖已有的有效凭据。写入后收紧权限为 0600。

profile_arn 缺失时（kiro cli/ide 的缓存不含它），用 refresh_token 做一次
OIDC 刷新，并调用 ListAvailableProfiles 补全，顺带验证凭据可用。
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import httpx

from ..runtime_credentials import CredentialLoadError, load_credentials
from ..transports.runtime import kiro_headers

REST_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"
# ListAvailableProfiles 探测区域：profile 通常在 us-east-1 或 eu-central-1，
# 与登录区域可能不同（详见 runtime-support 文档的区域说明）。
_PROFILE_REGION_CANDIDATES = ("us-east-1", "eu-central-1")

# 各必需字段 → 可接受的输入别名（snake_case 优先，兼容 camelCase 与 region）。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "refresh_token": ("refresh_token", "refreshToken"),
    "client_id": ("client_id", "clientId"),
    "client_secret": ("client_secret", "clientSecret"),
    "auth_region": ("auth_region", "authRegion", "region"),
    "profile_arn": ("profile_arn", "profileArn"),
    "access_token": ("access_token", "accessToken"),
    "expires_at": ("expires_at", "expiresAt"),
}


class CredentialImportError(Exception):
    """凭据导入失败。"""


def default_credentials_path() -> Path:
    """未指定目标文件时的默认凭据路径：固定在 config.json 同目录。

    复用 config_store.credentials_path，随 KIRO_PROXY_CONFIG_DIRECTORY 一起移动，
    避免与 config.json 位置不一致。函数内 import 规避潜在的加载顺序问题。
    """
    from .config_store import store

    return store.credentials_path


def normalize_fields(data: dict) -> dict:
    """把单个凭据对象归一化为 snake_case（丢弃空值与未知键）。"""
    out: dict[str, object] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            value = data.get(alias)
            if value not in (None, ""):
                out[canonical] = value
                break
    return out


def write_credentials(
    data: dict | list,
    target_file: str = "",
    account_index: int | None = None,
) -> tuple[Path, int | None]:
    """把已就绪的凭据（单对象或账户数组）写入并校验。

    Raises:
        CredentialImportError: 结构错误或必需字段缺失。
    """
    path = (
        Path(target_file).expanduser()
        if target_file.strip()
        else default_credentials_path()
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    # 先在临时文件上校验，通过后才替换目标文件。
    try:
        credentials = load_credentials(str(temp_path), account_index)
    except CredentialLoadError as exc:
        temp_path.unlink(missing_ok=True)
        raise CredentialImportError(f"凭据校验失败: {exc}") from exc

    temp_path.replace(path)
    return path, credentials.source_index


def import_credentials(
    raw_json: str,
    target_file: str = "",
    account_index: int | None = None,
) -> tuple[Path, int | None]:
    """校验并写入凭据 JSON（同步、无网络）。

    单对象会先做 camelCase → snake_case 归一化；账户数组原样交由
    load_credentials 处理。profile_arn 缺失时不在此补全（见
    import_credentials_completing）。

    Raises:
        CredentialImportError: JSON 非法、结构错误或必需字段缺失。
    """
    data = _parse_raw(raw_json)
    if isinstance(data, dict):
        data = normalize_fields(data)
    return write_credentials(data, target_file, account_index)


async def import_credentials_completing(
    raw_json: str,
    target_file: str = "",
    account_index: int | None = None,
) -> tuple[Path, int | None]:
    """校验并写入凭据 JSON；单对象缺 profile_arn 时刷新补全后再写入。

    Raises:
        CredentialImportError: JSON 非法、结构错误、补全失败或校验失败。
    """
    data = _parse_raw(raw_json)
    if isinstance(data, dict):
        data = normalize_fields(data)
        if not data.get("profile_arn"):
            data = await complete_profile(data)
    return write_credentials(data, target_file, account_index)


def _parse_raw(raw_json: str) -> dict | list:
    text = raw_json.strip()
    if not text:
        raise CredentialImportError("凭据内容为空")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialImportError(f"JSON 格式无效: {exc}") from exc
    if not isinstance(data, (dict, list)):
        raise CredentialImportError("凭据内容必须是 JSON 对象或账户数组")
    return data


async def complete_profile(fields: dict) -> dict:
    """用 refresh_token 刷新，并补全 profile_arn / access_token / expires_at。

    fields 需已含 refresh_token、client_id、client_secret（auth_region 可选，
    默认 us-east-1）。返回补全后的 snake_case 字段字典。

    Raises:
        CredentialImportError: 缺少刷新所需字段、刷新失败或无可用 profile。
    """
    refresh_token = str(fields.get("refresh_token") or "")
    client_id = str(fields.get("client_id") or "")
    client_secret = str(fields.get("client_secret") or "")
    region = str(fields.get("auth_region") or "us-east-1")
    if not (refresh_token and client_id and client_secret):
        raise CredentialImportError(
            "凭据缺少 refresh_token/client_id/client_secret，无法补全 profile_arn"
        )

    async with httpx.AsyncClient(timeout=30) as client:
        access_token, new_refresh, expires_in, profile_arn = await _oidc_refresh(
            client, region, client_id, client_secret, refresh_token
        )
        if not profile_arn:
            profile_arn = await _first_profile_arn(client, access_token, region)

    completed = dict(fields)
    completed["refresh_token"] = new_refresh or refresh_token
    completed["client_id"] = client_id
    completed["client_secret"] = client_secret
    completed["auth_region"] = region
    completed["access_token"] = access_token
    completed["expires_at"] = time.time() + expires_in
    completed["profile_arn"] = profile_arn
    return completed


async def _oidc_refresh(
    client: httpx.AsyncClient,
    region: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> tuple[str, str, int, str]:
    """OIDC refresh_token 交换。返回 (access_token, refresh_token, expires_in, profile_arn)。"""
    endpoint = f"https://oidc.{region}.amazonaws.com/token"
    try:
        resp = await client.post(
            endpoint,
            json={
                "grantType": "refresh_token",
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
            },
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise CredentialImportError(
            f"凭据刷新网络错误: {type(exc).__name__}"
        ) from exc
    if resp.status_code != 200:
        raise CredentialImportError(
            f"凭据刷新失败: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    access_token = data.get("accessToken", data.get("access_token", ""))
    if not access_token:
        raise CredentialImportError("刷新响应缺少 accessToken，凭据可能已失效")
    new_refresh = data.get("refreshToken", data.get("refresh_token", ""))
    expires_in = int(data.get("expiresIn", data.get("expires_in", 3600)))
    profile_arn = data.get("profileArn", data.get("profile_arn", ""))
    return access_token, new_refresh, expires_in, profile_arn


async def _first_profile_arn(
    client: httpx.AsyncClient, access_token: str, region_hint: str = ""
) -> str:
    """跨候选区域调用 ListAvailableProfiles，返回第一个可用 profile ARN。

    profile 可能不在登录区域，优先尝试凭据自身 region，再退回
    us-east-1 / eu-central-1。请求必须带完整 Kiro 合规头，否则 CodeWhisperer
    端点会对缺头请求返回 HTTP 400。
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
            # 带上上游返回体（截断），便于定位是区域不支持、校验失败还是鉴权。
            last_error = f"{region}: HTTP {resp.status_code} {resp.text[:300]}"
            continue
        profiles = resp.json().get("profiles", [])
        if isinstance(profiles, list) and profiles:
            arn = profiles[0].get("arn", "")
            if arn:
                return arn
        last_error = f"{region}: 返回空 profile 列表"
    raise CredentialImportError(
        f"未能获取可用 profile（{last_error or '账户下无 profile'}）"
    )


__all__ = [
    "CredentialImportError",
    "complete_profile",
    "default_credentials_path",
    "import_credentials",
    "import_credentials_completing",
    "normalize_fields",
    "write_credentials",
]
