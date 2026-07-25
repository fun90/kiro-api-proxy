"""IdC SSO 登录模块单元测试（仅覆盖无网络的纯函数部分）。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from kiro_api_proxy.admin.sso import (
    SsoError,
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
