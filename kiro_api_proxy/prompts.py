from __future__ import annotations

import json
from typing import Any

from .config import settings
from .schemas import AnthropicRequest, Message, ResponsesRequest


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
    sections = [
        f"### {labels[item.role]}"
        f"{f'（{item.name}）' if item.name else ''}\n"
        f"{content_text(item.content)}"
        for item in messages
    ]
    return (
        f"所有面向用户的自然语言必须使用{settings.response_language}，包括计划、"
        "分析摘要、进度说明、工具调用前后说明和最终答复；"
        "代码、命令、路径和标识符保持原样。\n\n"
        "下面是完整对话。请生成下一条助手回复，不要输出角色标题或解释这些格式。\n\n"
        + "\n\n".join(sections)
        + "\n\n### 助手\n"
    )


def responses_to_messages(request: ResponsesRequest) -> list[Message]:
    messages: list[Message] = []
    if request.instructions:
        messages.append(Message(role="system", content=request.instructions))
    if isinstance(request.input, str):
        messages.append(Message(role="user", content=request.input))
        return messages
    valid_roles = {"system", "developer", "user", "assistant", "tool"}
    for item in request.input:
        role = item.get("role", "user")
        if role not in valid_roles:
            role = "user"
        messages.append(Message(role=role, content=item.get("content", "")))
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
