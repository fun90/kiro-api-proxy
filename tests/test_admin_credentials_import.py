"""凭据导入模块单元测试。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from kiro_api_proxy.admin.credentials_import import (
    CredentialImportError,
    import_credentials,
)

VALID = {
    "refresh_token": "rt-abc",
    "client_id": "cid",
    "client_secret": "csecret",
    "auth_region": "us-east-1",
    "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/P1",
}


def test_import_object_success(tmp_path: Path):
    target = tmp_path / "creds.json"
    path, source_index = import_credentials(json.dumps(VALID), str(target))
    assert path == target
    assert source_index is None
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "rt-abc"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限")
def test_import_sets_strict_permissions(tmp_path: Path):
    target = tmp_path / "creds.json"
    import_credentials(json.dumps(VALID), str(target))
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_import_account_array_with_index(tmp_path: Path):
    array = [
        {"enabled": False, "status": "invalid"},
        {
            "enabled": True,
            "status": "active",
            "refreshToken": "refresh",
            "clientId": "client",
            "clientSecret": "secret",
            "region": "ap-southeast-2",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/P",
        },
    ]
    target = tmp_path / "accounts.json"
    path, source_index = import_credentials(
        json.dumps(array), str(target), account_index=1
    )
    assert source_index == 1


def test_import_empty_content(tmp_path: Path):
    with pytest.raises(CredentialImportError, match="为空"):
        import_credentials("   ", str(tmp_path / "x.json"))


def test_import_invalid_json(tmp_path: Path):
    with pytest.raises(CredentialImportError, match="JSON 格式无效"):
        import_credentials("{not json", str(tmp_path / "x.json"))


def test_import_missing_fields_does_not_overwrite(tmp_path: Path):
    """坏凭据校验失败时，不应覆盖已有的有效凭据文件。"""
    target = tmp_path / "creds.json"
    import_credentials(json.dumps(VALID), str(target))
    original = target.read_text(encoding="utf-8")

    incomplete = {"refresh_token": "only"}
    with pytest.raises(CredentialImportError, match="校验失败"):
        import_credentials(json.dumps(incomplete), str(target))

    # 目标文件保持原样，临时文件已清理。
    assert target.read_text(encoding="utf-8") == original
    assert not (target.with_suffix(target.suffix + ".tmp")).exists()


def test_import_rejects_scalar(tmp_path: Path):
    with pytest.raises(CredentialImportError, match="必须是 JSON 对象或账户数组"):
        import_credentials("123", str(tmp_path / "x.json"))
