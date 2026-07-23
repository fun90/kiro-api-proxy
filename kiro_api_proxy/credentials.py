from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import Settings
from .transports import ErrorCategory, TransportError

PROFILE_ARN = re.compile(
    r"arn:(?P<partition>aws(?:-[a-z]+)*):codewhisperer:"
    r"(?P<region>[a-z0-9-]+):(?P<account>\d+):profile/(?P<profile>[A-Za-z0-9_-]+)"
)


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    account_type: str
    identity_center_region: str
    runtime_region: str
    start_url: str
    profile_arn: str


def parse_whoami(output: str) -> CredentialMetadata:
    json_match = re.search(r"\{.*?\}", output, re.DOTALL)
    arn_match = PROFILE_ARN.search(output)
    if not json_match or not arn_match:
        raise TransportError(
            "Kiro 身份信息缺少区域或 Profile ARN",
            ErrorCategory.AUTHENTICATION,
        )
    try:
        identity = json.loads(json_match.group(0))
    except json.JSONDecodeError as exc:
        raise TransportError(
            "Kiro 身份信息格式异常", ErrorCategory.PROTOCOL
        ) from exc
    return CredentialMetadata(
        account_type=str(identity.get("accountType", "")),
        identity_center_region=str(identity.get("region", "")),
        runtime_region=arn_match.group("region"),
        start_url=str(identity.get("startUrl", "")),
        profile_arn=arn_match.group(0),
    )


async def discover_metadata(settings: Settings) -> CredentialMetadata:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        settings.kiro_cli,
        "whoami",
        "--format",
        "json-pretty",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=settings.working_directory,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise TransportError(
            "读取 Kiro 身份元数据失败", ErrorCategory.AUTHENTICATION
        )
    # Profile 信息在当前 CLI 版本可能写入 stderr，因此仅在内存中合并解析。
    return parse_whoami(
        stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")
    )
