"""runtime_credentials 模块单元测试。"""

import json
import os
import stat
from pathlib import Path

import pytest

from kiro_api_proxy.runtime_credentials import (
    CredentialLoadError,
    RuntimeCredentials,
    load_credentials,
)

VALID_CREDENTIALS = {
    "refresh_token": "rt-abc123",
    "client_id": "client-id-xyz",
    "client_secret": "client-secret-000",
    "auth_region": "us-east-1",
    "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/PROF1",
    "access_token": "at-existing",
    "expires_at": 9999999999.0,
}


@pytest.fixture()
def creds_file(tmp_path: Path) -> Path:
    """写入有效凭据文件并返回路径。"""
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(VALID_CREDENTIALS), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def test_load_success(creds_file: Path):
    creds = load_credentials(str(creds_file))
    assert isinstance(creds, RuntimeCredentials)
    assert creds.refresh_token == "rt-abc123"
    assert creds.client_id == "client-id-xyz"
    assert creds.client_secret == "client-secret-000"
    assert creds.auth_region == "us-east-1"
    assert creds.profile_arn == VALID_CREDENTIALS["profile_arn"]
    assert creds.access_token == "at-existing"
    assert creds.expires_at == 9999999999.0


def test_load_success_without_optional_fields(tmp_path: Path):
    data = {k: v for k, v in VALID_CREDENTIALS.items() if k not in ("access_token", "expires_at")}
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    creds = load_credentials(str(path))
    assert creds.access_token == ""
    assert creds.expires_at == 0


def test_load_file_not_found(tmp_path: Path):
    with pytest.raises(CredentialLoadError, match="不存在"):
        load_credentials(str(tmp_path / "no-such-file.json"))


def test_load_invalid_json(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CredentialLoadError, match="JSON 格式无效"):
        load_credentials(str(path))


def test_load_not_object(tmp_path: Path):
    path = tmp_path / "array.json"
    path.write_text('["a","b"]', encoding="utf-8")
    with pytest.raises(CredentialLoadError, match="必须是 JSON 对象"):
        load_credentials(str(path))


def test_load_kiro_account_manager_array(tmp_path: Path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            [
                {"enabled": False, "status": "invalid"},
                {
                    "enabled": True,
                    "status": "active",
                    "refreshToken": "refresh",
                    "clientId": "client",
                    "clientSecret": "secret",
                    "region": "ap-southeast-2",
                    "profileArn": (
                        "arn:aws:codewhisperer:us-east-1:123:profile/P"
                    ),
                    "accessToken": "access",
                    "expiresAt": "2026/07/24 18:00:00",
                },
            ]
        ),
        encoding="utf-8",
    )

    credentials = load_credentials(str(path))

    assert credentials.refresh_token == "refresh"
    assert credentials.auth_region == "ap-southeast-2"
    assert credentials.endpoint_region == "us-east-1"
    assert credentials.source_index == 1

    selected = load_credentials(str(path), account_index=1)
    assert selected.source_index == 1

    with pytest.raises(CredentialLoadError, match="指定可用账户"):
        load_credentials(str(path), account_index=0)


def test_load_missing_required_fields(tmp_path: Path):
    data = {"refresh_token": "rt", "client_id": "ci"}
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CredentialLoadError, match="缺少必需字段.*client_secret.*auth_region.*profile_arn"):
        load_credentials(str(path))


def test_load_empty_required_field(tmp_path: Path):
    data = dict(VALID_CREDENTIALS)
    data["refresh_token"] = ""
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CredentialLoadError, match="缺少必需字段.*refresh_token"):
        load_credentials(str(path))


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限检查")
def test_load_warns_on_world_readable(tmp_path: Path, caplog):
    path = tmp_path / "wide.json"
    path.write_text(json.dumps(VALID_CREDENTIALS), encoding="utf-8")
    path.chmod(0o644)
    with caplog.at_level("WARNING"):
        creds = load_credentials(str(path))
    assert creds.refresh_token == "rt-abc123"
    assert "权限过宽" in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限检查")
def test_load_no_warning_on_strict_permissions(tmp_path: Path, caplog):
    path = tmp_path / "strict.json"
    path.write_text(json.dumps(VALID_CREDENTIALS), encoding="utf-8")
    path.chmod(0o600)
    with caplog.at_level("WARNING"):
        load_credentials(str(path))
    assert "权限过宽" not in caplog.text


def test_parse_expires_at_iso_with_millis(tmp_path: Path):
    """kiro cli/ide 的 ISO 带毫秒 expires_at（单对象路径）能正确解析，不崩溃。"""
    data = dict(VALID_CREDENTIALS)
    data["expires_at"] = "2026-07-25T14:10:01.899Z"
    path = tmp_path / "iso.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    creds = load_credentials(str(path))
    # 2026-07-25T14:10:01Z 的 Unix 秒约为 1784., 只要求成功解析为正数。
    assert creds.expires_at > 1_700_000_000


def test_endpoint_region_from_profile_arn():
    creds = RuntimeCredentials(
        refresh_token="rt",
        client_id="ci",
        client_secret="cs",
        auth_region="us-east-1",
        profile_arn="arn:aws:codewhisperer:ap-southeast-1:123:profile/P",
        access_token="",
        expires_at=0,
    )
    assert creds.endpoint_region == "ap-southeast-1"
