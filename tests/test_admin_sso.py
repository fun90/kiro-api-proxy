"""IdC SSO 登录模块单元测试（仅覆盖无网络的纯函数部分）。"""

from __future__ import annotations

import base64
import hashlib
import time
from types import SimpleNamespace

import pytest

from kiro_api_proxy.admin.routes import _loopback_callback_uri
from kiro_api_proxy.admin.sso import (
    SsoError,
    SsoLoginManager,
    SsoSession,
    _parse_callback,
    _pkce_pair,
)


def test_pkce_pair_challenge_is_s256_of_verifier():
    verifier, challenge = _pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    # verifier/challenge 均为无填充 base64url，不含 '='。
    assert "=" not in verifier
    assert "=" not in challenge


def test_pkce_pair_is_random():
    assert _pkce_pair()[0] != _pkce_pair()[0]


def test_parse_callback_extracts_code_and_state():
    code, state = _parse_callback(
        "http://127.0.0.1/oauth/callback?code=abc123&state=xyz"
    )
    assert code == "abc123"
    assert state == "xyz"


def test_parse_callback_surfaces_error_param():
    with pytest.raises(SsoError, match="授权失败"):
        _parse_callback("http://127.0.0.1/oauth/callback?error=access_denied")


def test_parse_callback_missing_code():
    with pytest.raises(SsoError, match="未包含授权码"):
        _parse_callback("http://127.0.0.1/oauth/callback?state=only")


# ---- 会话查询 / 轮询状态流转 ----


def _make_session(manager: SsoLoginManager, state: str = "st-1") -> SsoSession:
    """构造一个进行中的会话并塞入 manager，供无网络的状态流转测试使用。"""
    session = SsoSession(
        session_id="sid-1",
        client_id="cid",
        client_secret="secret",
        code_verifier="verifier",
        state=state,
        region="us-east-1",
        start_url="https://view.awsapps.com/start",
        redirect_uri="http://127.0.0.1:3458/admin/api/sso/callback",
        expires_at=time.time() + 600,
    )
    manager._sessions[session.session_id] = session
    return session


def test_session_by_state_found_and_missing():
    manager = SsoLoginManager()
    session = _make_session(manager, state="abc")
    assert manager.session_by_state("abc") is session
    assert manager.session_by_state("nope") is None


def test_session_by_state_ignores_expired():
    manager = SsoLoginManager()
    session = _make_session(manager, state="abc")
    session.expires_at = time.time() - 1  # 已过期，_cleanup 应先剔除
    assert manager.session_by_state("abc") is None


def test_poll_pending_then_success_consumes_session():
    manager = SsoLoginManager()
    session = _make_session(manager)
    assert manager.poll("sid-1") == {"status": "pending"}

    manager.mark_success(session, "/tmp/cred.json", "arn:aws:codewhisperer:profile")
    result = manager.poll("sid-1")
    assert result == {
        "status": "success",
        "path": "/tmp/cred.json",
        "profile_arn": "arn:aws:codewhisperer:profile",
    }
    # 终态读取后即清理，再次轮询变 not_found。
    assert manager.poll("sid-1") == {"status": "not_found"}


def test_poll_error_consumes_session():
    manager = SsoLoginManager()
    session = _make_session(manager)
    manager.mark_error(session, "换取 Token 失败: HTTP 400")
    assert manager.poll("sid-1") == {
        "status": "error",
        "error": "换取 Token 失败: HTTP 400",
    }
    assert manager.poll("sid-1") == {"status": "not_found"}


def test_poll_unknown_session_is_not_found():
    manager = SsoLoginManager()
    assert manager.poll("ghost") == {"status": "not_found"}


# ---- loopback 回调地址判定 ----


def _request_stub(url: str):
    """伪造 Request：仅暴露 _loopback_callback_uri 用到的 url/base_url。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    return SimpleNamespace(
        url=SimpleNamespace(hostname=parsed.hostname),
        base_url=base,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3458/admin/",
        "http://localhost:3458/admin/",
        "http://127.0.0.5:8080/admin/",
    ],
)
def test_loopback_callback_uri_for_loopback_hosts(url):
    result = _loopback_callback_uri(_request_stub(url))
    # AWS 白名单要求 loopback 回调路径精确等于 /oauth/callback。
    assert result.endswith("/oauth/callback")
    assert result.startswith("http://")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:3458/admin/",
        "http://proxy.example.com/admin/",
    ],
)
def test_loopback_callback_uri_empty_for_remote_hosts(url):
    assert _loopback_callback_uri(_request_stub(url)) == ""
