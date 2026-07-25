"""跨平台本机凭据扫描单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_api_proxy.admin.local_import import (
    find_local_credential,
    scan_local_credentials,
)

TOKEN_FILE = {
    "accessToken": "at-xxx",
    "refreshToken": "rt-xxx",
    "expiresAt": "2026-07-25T14:10:01.899Z",
    "clientIdHash": "abc123hash",
    "authMethod": "IdC",
    "provider": "Enterprise",
    "region": "ap-southeast-2",
}
CLIENT_FILE = {
    "clientId": "client-id-xyz",
    "clientSecret": "client-secret-000",
    "expiresAt": "2026-08-28T19:18:22.000Z",
}


@pytest.fixture()
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    """构造两文件配对的 SSO 缓存目录并指向它。"""
    (tmp_path / "kiro-auth-token.json").write_text(
        json.dumps(TOKEN_FILE), encoding="utf-8"
    )
    (tmp_path / "abc123hash.json").write_text(
        json.dumps(CLIENT_FILE), encoding="utf-8"
    )
    monkeypatch.setenv("KIRO_SSO_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_scan_pairs_token_and_client(cache_dir: Path):
    creds = scan_local_credentials()
    assert len(creds) == 1
    cred = creds[0]
    # 靠 clientIdHash 配对补齐 client 信息。
    assert cred.fields["refresh_token"] == "rt-xxx"
    assert cred.fields["client_id"] == "client-id-xyz"
    assert cred.fields["client_secret"] == "client-secret-000"
    assert cred.fields["auth_region"] == "ap-southeast-2"
    # 两文件均无 profile_arn，扫描阶段不补全。
    assert "profile_arn" not in cred.fields
    assert cred.auth_method == "IdC"
    assert cred.provider == "Enterprise"


def test_scan_summary_hides_secrets(cache_dir: Path):
    summary = scan_local_credentials()[0].summary()
    # 摘要只暴露布尔与非敏感元数据，不含任何 token/secret 值。
    assert summary["has_client_secret"] is True
    assert summary["has_profile_arn"] is False
    assert "client-secret-000" not in json.dumps(summary)
    assert "rt-xxx" not in json.dumps(summary)


def test_find_local_credential_by_id(cache_dir: Path):
    cred = scan_local_credentials()[0]
    assert find_local_credential(cred.id) is not None
    assert find_local_credential("no-such-id") is None


def test_scan_token_without_client_file(tmp_path: Path, monkeypatch):
    """缺配对 client 文件时仍返回（有 refresh_token），但 client 信息为空。"""
    (tmp_path / "kiro-auth-token.json").write_text(
        json.dumps(TOKEN_FILE), encoding="utf-8"
    )
    monkeypatch.setenv("KIRO_SSO_CACHE_DIR", str(tmp_path))
    creds = scan_local_credentials()
    assert len(creds) == 1
    assert not creds[0].fields.get("client_id")
    assert creds[0].summary()["has_client_secret"] is False


def test_scan_empty_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIRO_SSO_CACHE_DIR", str(tmp_path))
    assert scan_local_credentials() == []


def test_scan_missing_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIRO_SSO_CACHE_DIR", str(tmp_path / "nope"))
    assert scan_local_credentials() == []


def test_scan_ignores_non_token_json(tmp_path: Path, monkeypatch):
    """不含 refreshToken/accessToken 的 JSON（如纯 client 文件）不作为凭据条目。"""
    (tmp_path / "abc123hash.json").write_text(
        json.dumps(CLIENT_FILE), encoding="utf-8"
    )
    monkeypatch.setenv("KIRO_SSO_CACHE_DIR", str(tmp_path))
    assert scan_local_credentials() == []
