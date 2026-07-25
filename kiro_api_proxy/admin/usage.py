"""额度查询。

调用 Kiro 数据面 `GET /getUsageLimits` 获取当前凭据的订阅信息、已用/上限
用量、重置日期与试用配额。复用 runtime_credentials 加载凭据、TokenProvider
拿有效 access token，端点区域从 profile_arn 推导。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..runtime_credentials import RuntimeCredentials, load_credentials
from ..token_provider import TokenProvider

REST_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"


class UsageQueryError(Exception):
    """额度查询失败。"""


@dataclass(slots=True)
class UsageSnapshot:
    """一次额度查询的结构化结果。"""

    email: str = ""
    user_id: str = ""
    subscription_type: str = ""
    subscription_title: str = ""
    usage_current: float = 0.0
    usage_limit: float = 0.0
    usage_percent: float = 0.0
    next_reset_date: str = ""
    trial_status: str = ""
    trial_usage_current: float = 0.0
    trial_usage_limit: float = 0.0
    resource_type: str = ""
    currency: str = ""
    checked_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "user_id": self.user_id,
            "subscription_type": self.subscription_type,
            "subscription_title": self.subscription_title,
            "usage_current": self.usage_current,
            "usage_limit": self.usage_limit,
            "usage_percent": self.usage_percent,
            "next_reset_date": self.next_reset_date,
            "trial_status": self.trial_status,
            "trial_usage_current": self.trial_usage_current,
            "trial_usage_limit": self.trial_usage_limit,
            "resource_type": self.resource_type,
            "currency": self.currency,
            "checked_at": self.checked_at,
        }


def _endpoint_for(credentials: RuntimeCredentials, override: str = "") -> str:
    if override:
        return override.rstrip("/")
    region = credentials.endpoint_region
    return (
        REST_ENDPOINT
        if region == "us-east-1"
        else f"https://q.{region}.amazonaws.com"
    )


def _float(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _pick_float(source: dict[str, Any], *keys: str) -> float:
    """按顺序取第一个有效数字字段；带精度字段（WithPrecision）应排在前。"""
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _reset_date(value: Any) -> str:
    """把 nextDateReset（Unix 秒，可能是字符串）转为 YYYY-MM-DD。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_subscription_type(raw: str) -> str:
    # 归一化：把 '+' 视作 ' PLUS '，兼容 "PRO+"/"PRO PLUS"/"PRO_PLUS"/"PROPLUS"
    upper = raw.upper().replace("+", " PLUS ")
    if "POWER" in upper:
        return "POWER"
    if "PRO" in upper and "PLUS" in upper:
        return "PRO_PLUS"
    if "PRO" in upper:
        return "PRO"
    return "FREE"


def _parse_snapshot(data: dict[str, Any]) -> UsageSnapshot:
    snap = UsageSnapshot(checked_at=int(datetime.now(tz=timezone.utc).timestamp()))

    user_info = data.get("userInfo")
    if isinstance(user_info, dict):
        snap.email = str(user_info.get("email", ""))
        snap.user_id = str(user_info.get("userId", ""))

    sub = data.get("subscriptionInfo")
    if isinstance(sub, dict):
        title = (
            sub.get("subscriptionTitle")
            or sub.get("subscriptionName")
            or sub.get("subscriptionType")
            or ""
        )
        snap.subscription_title = str(
            sub.get("subscriptionTitle") or sub.get("subscriptionName") or ""
        )
        snap.subscription_type = _parse_subscription_type(str(title))

    breakdown_list = data.get("usageBreakdownList")
    if isinstance(breakdown_list, list) and breakdown_list:
        breakdown = breakdown_list[0]
        if isinstance(breakdown, dict):
            snap.resource_type = str(breakdown.get("resourceType", ""))
            snap.currency = str(breakdown.get("currency", ""))
            # 优先取带精度字段（如 currentUsageWithPrecision=4946.35），
            # 退回整数字段（currentUsage=4946）。
            snap.usage_current = _pick_float(
                breakdown, "currentUsageWithPrecision", "currentUsage"
            )
            snap.usage_limit = _pick_float(
                breakdown, "usageLimitWithPrecision", "usageLimit"
            )
            if snap.usage_limit > 0:
                snap.usage_percent = snap.usage_current / snap.usage_limit
            trial = breakdown.get("freeTrialInfo")
            if isinstance(trial, dict):
                snap.trial_status = str(trial.get("freeTrialStatus", ""))
                snap.trial_usage_current = _pick_float(
                    trial, "currentUsageWithPrecision", "currentUsage"
                )
                snap.trial_usage_limit = _pick_float(
                    trial, "usageLimitWithPrecision", "usageLimit"
                )

    snap.next_reset_date = _reset_date(data.get("nextDateReset"))
    return snap


async def fetch_usage(
    credentials_file: str,
    account_index: int | None = None,
    endpoint_override: str = "",
) -> UsageSnapshot:
    """加载凭据并查询额度。

    Raises:
        UsageQueryError: 凭据加载失败、认证失败或上游返回错误。
    """
    try:
        credentials = load_credentials(credentials_file, account_index)
    except Exception as exc:  # CredentialLoadError 等
        raise UsageQueryError(f"凭据加载失败: {exc}") from exc

    provider = TokenProvider(credentials, credentials_file)
    try:
        token = await provider.get_token()
    except Exception as exc:  # TokenRefreshError 等
        raise UsageQueryError(f"获取 Token 失败: {exc}") from exc

    endpoint = _endpoint_for(credentials, endpoint_override)
    url = f"{endpoint}/getUsageLimits"
    params = {
        "origin": "AI_EDITOR",
        "resourceType": "AGENTIC_REQUEST",
        "isEmailRequired": "true",
        "profileArn": credentials.profile_arn,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise UsageQueryError(
            f"额度查询网络错误: {type(exc).__name__}"
        ) from exc

    if resp.status_code in (401, 403):
        raise UsageQueryError(f"额度查询认证失败: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise UsageQueryError(f"额度查询失败: HTTP {resp.status_code}")

    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        raise UsageQueryError("额度响应格式无效") from exc

    return _parse_snapshot(data)


__all__ = ["UsageQueryError", "UsageSnapshot", "fetch_usage"]
