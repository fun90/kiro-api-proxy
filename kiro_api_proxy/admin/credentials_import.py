"""凭据导入。

校验粘贴/上传的凭据 JSON（普通对象或 Kiro Account Manager 账户数组），
先写入临时文件并用 load_credentials 做完整校验，通过后再原子替换目标
文件，避免坏凭据覆盖已有的有效凭据。写入后收紧权限为 0600。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ..runtime_credentials import CredentialLoadError, load_credentials


class CredentialImportError(Exception):
    """凭据导入失败。"""


def default_credentials_path() -> Path:
    """未指定目标文件时的默认凭据路径。"""
    base = os.getenv("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "kiro-api-proxy" / "runtime-credentials.json"


def import_credentials(
    raw_json: str,
    target_file: str = "",
    account_index: int | None = None,
) -> tuple[Path, int | None]:
    """校验并写入凭据 JSON。

    Args:
        raw_json: 凭据 JSON 文本（对象或账户数组）。
        target_file: 目标文件路径，为空时使用默认路径。
        account_index: 账户数组时指定使用的零基索引。

    Returns:
        (写入路径, 生效的 source_index)。

    Raises:
        CredentialImportError: JSON 非法、结构错误或必需字段缺失。
    """
    text = raw_json.strip()
    if not text:
        raise CredentialImportError("凭据内容为空")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialImportError(f"JSON 格式无效: {exc}") from exc

    if not isinstance(data, (dict, list)):
        raise CredentialImportError("凭据内容必须是 JSON 对象或账户数组")

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


__all__ = [
    "CredentialImportError",
    "default_credentials_path",
    "import_credentials",
]
