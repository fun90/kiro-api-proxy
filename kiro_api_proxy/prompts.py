from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import settings
from .schemas import AnthropicRequest, Message, ResponsesRequest

WORKING_DIRECTORY_PATTERN = re.compile(
    r"(?im)^(?:current\s+)?working directory:\s*([^\r\n<]+)"
)
SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def content_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    output: list[str] = []
    for part in content:
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            output.append(str(part.get("text", "")))
        elif part_type == "image_url":
            output.append(f"[图片：{part.get('image_url')}]")
        elif part_type in {"tool_use", "tool_result"}:
            # 工具调用与结果由结构化 history/toolResults 通道传递。把它们写入
            # prompt 会为模型提供可模仿的伪工具语法，导致工具调用退化成普通文本。
            continue
        else:
            output.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(output)


def messages_to_prompt(messages: list[Message]) -> str:
    labels = {
        "system": "系统指令",
        "developer": "开发者指令",
        "user": "用户",
        "assistant": "助手",
        "tool": "工具结果",
    }
    sections: list[str] = []
    for item in messages:
        # OpenAI 的 role=tool 消息也由结构化 toolResults 通道传递。
        if item.role == "tool":
            continue
        text = content_text(item.content)
        # 工具专用消息去除结构化块后为空，不应留下可干扰模型的空角色段。
        if not text:
            continue
        sections.append(
            f"### {labels[item.role]}"
            f"{f'（{item.name}）' if item.name else ''}\n"
            f"{text}"
        )
    return (
        f"所有面向用户的自然语言必须使用{settings.response_language}，包括计划、"
        "分析摘要、进度说明、工具调用前后说明和最终答复；"
        "代码、命令、路径和标识符保持原样。\n\n"
        "下面是完整对话。请生成下一条助手回复，不要输出角色标题或解释这些格式。\n\n"
        + "\n\n".join(sections)
        + "\n\n### 助手\n"
    )


def validated_working_directory(raw_path: str) -> str | None:
    raw_path = raw_path.strip().strip("`'\"")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        allowed_root = Path(settings.working_directory).resolve(strict=True)
        resolved.relative_to(allowed_root)
    except (OSError, ValueError):
        return None
    return str(resolved) if resolved.is_dir() else None


def prompt_working_directory(prompt: str) -> str | None:
    match = WORKING_DIRECTORY_PATTERN.search(prompt)
    if match is None:
        return None
    return validated_working_directory(match.group(1))


def claude_session_working_directory(
    session_id: str,
    projects_root: Path | None = None,
) -> str | None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        return None
    root = projects_root or Path.home() / ".claude" / "projects"
    candidates = sorted(
        root.glob(f"*/{session_id}.jsonl"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for candidate in candidates:
        try:
            with candidate.open(encoding="utf-8") as handle:
                for _ in range(10):
                    line = handle.readline()
                    if not line:
                        break
                    cwd = json.loads(line).get("cwd")
                    if cwd:
                        return validated_working_directory(str(cwd))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def responses_to_messages(request: ResponsesRequest) -> list[Message]:
    messages: list[Message] = []
    if request.instructions:
        messages.append(Message(role="system", content=request.instructions))
    if isinstance(request.input, str):
        messages.append(Message(role="user", content=request.input))
        return messages
    valid_roles = {"system", "developer", "user", "assistant", "tool"}
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if not pending_calls:
            return
        messages.append(
            Message(role="assistant", content="", tool_calls=list(pending_calls))
        )
        pending_calls.clear()

    for item in request.input:
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            if not call_id or not item.get("name"):
                continue
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            pending_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": arguments,
                    },
                }
            )
            continue

        flush_calls()
        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not call_id:
                continue
            output = item.get("output", "")
            if isinstance(output, dict):
                output = [{"json": output}]
            elif not isinstance(output, (str, list)):
                output = str(output)
            messages.append(
                Message(
                    role="tool",
                    content=output,
                    tool_call_id=call_id,
                    status=item.get("status"),
                    is_error=item.get("is_error"),
                )
            )
            continue

        if item_type not in (None, "message"):
            continue
        role = item.get("role", "user")
        if role not in valid_roles:
            role = "user"
        messages.append(Message(role=role, content=item.get("content", "")))
    flush_calls()
    return messages


def anthropic_to_messages(request: AnthropicRequest) -> list[Message]:
    messages: list[Message] = []
    if request.system:
        messages.append(Message(role="system", content=request.system))
    messages.extend(
        Message(role=item.role, content=item.content) for item in request.messages
    )
    return messages


def anthropic_upstream_model(request: AnthropicRequest) -> str:
    if request.model.endswith("-thinking"):
        return request.model
    if request.thinking and request.thinking.get("type") in {"enabled", "adaptive"}:
        return f"{request.model}-thinking"
    return request.model
