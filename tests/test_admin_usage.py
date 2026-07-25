"""额度查询解析单元测试（仅覆盖无网络的纯解析部分）。"""

from __future__ import annotations

from kiro_api_proxy.admin.usage import _parse_snapshot, _parse_subscription_type


def test_parse_subscription_type():
    assert _parse_subscription_type("KIRO PRO+") == "PRO_PLUS"
    assert _parse_subscription_type("Kiro Pro Plus") == "PRO_PLUS"
    assert _parse_subscription_type("PRO_PLUS") == "PRO_PLUS"
    assert _parse_subscription_type("POWER") == "POWER"
    assert _parse_subscription_type("") == "FREE"


def test_parse_snapshot_full():
    data = {
        "userInfo": {"email": "u@example.com", "userId": "uid-1"},
        "subscriptionInfo": {
            "subscriptionTitle": "KIRO PRO",
            "subscriptionType": "PRO",
        },
        "usageBreakdownList": [
            {
                "resourceType": "AGENTIC_REQUEST",
                "currency": "USD",
                "currentUsage": 30.0,
                "usageLimit": 100.0,
                "freeTrialInfo": {
                    "freeTrialStatus": "ACTIVE",
                    "currentUsage": 5.0,
                    "usageLimit": 50.0,
                },
            }
        ],
        "nextDateReset": "1793491200",
    }
    snap = _parse_snapshot(data)
    assert snap.email == "u@example.com"
    assert snap.user_id == "uid-1"
    assert snap.subscription_title == "KIRO PRO"
    assert snap.usage_current == 30.0
    assert snap.usage_limit == 100.0
    assert abs(snap.usage_percent - 0.3) < 1e-9
    assert snap.resource_type == "AGENTIC_REQUEST"
    assert snap.trial_status == "ACTIVE"
    assert snap.trial_usage_limit == 50.0
    assert snap.next_reset_date  # 已格式化为 YYYY-MM-DD
    assert snap.checked_at > 0


def test_parse_snapshot_handles_missing_sections():
    snap = _parse_snapshot({})
    assert snap.email == ""
    assert snap.usage_current == 0.0
    assert snap.usage_percent == 0.0
    assert snap.next_reset_date == ""


def test_parse_snapshot_zero_limit_no_divide():
    data = {"usageBreakdownList": [{"currentUsage": 5.0, "usageLimit": 0.0}]}
    snap = _parse_snapshot(data)
    assert snap.usage_percent == 0.0
    assert snap.usage_current == 5.0
