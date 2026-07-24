"""客户端工具定义与历史工具消息 → Kiro 工具契约的转换。

覆盖 Anthropic（`tool_use`/`tool_result` content block）与 OpenAI
（`tools`/`assistant.tool_calls`/`role:tool`）两种形态，产出：

- toolSpecification 列表：填入 Kiro `userInputMessageContext.tools`
- toolResults 列表：填入 Kiro `userInputMessageContext.toolResults`
"""

from __future__ import annotations

import json
from typing import Any

from .schemas import AnthropicMessage, Message

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _tool_spec(name: str, description: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "toolSpecification": {
            "name": name,
            "description": description or "",
            "inputSchema": {"json": schema or _EMPTY_SCHEMA},
        }
    }


def anthropic_tools_to_specs(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Anthropic `tools` → Kiro toolSpecification 列表。"""
    specs: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        specs.append(
            _tool_spec(name, tool.get("description", ""), tool.get("input_schema"))
        )
    return specs


def openai_tools_to_specs(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """OpenAI `tools`（function）→ Kiro toolSpecification 列表。"""
    specs: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") not in (None, "function"):
            continue
        function = tool.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        specs.append(
            _tool_spec(
                name,
                function.get("description", ""),
                function.get("parameters"),
            )
        )
    return specs


def _result_content(content: Any) -> list[dict[str, Any]]:
    """规范化工具结果内容为 Kiro `content` 数组（`text`/`json` 块）。"""
    if content is None:
        return [{"text": ""}]
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, dict):
        return [{"json": content}]
    normalized: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "text":
                normalized.append({"text": str(block.get("text", ""))})
            elif block_type == "json" or "json" in block:
                normalized.append({"json": block.get("json", block)})
            else:
                normalized.append({"text": json.dumps(block, ensure_ascii=False)})
        else:
            normalized.append({"text": str(block)})
    return normalized or [{"text": ""}]


def anthropic_tool_results(
    messages: list[AnthropicMessage],
) -> list[dict[str, Any]]:
    """从最近一轮 user 消息提取 `tool_result` block → Kiro toolResults。

    Agent 回路中，客户端会在新一轮请求的最后一条 user 消息里携带上一轮
    assistant `tool_use` 的执行结果，按 `tool_use_id` 关联。
    """
    if not messages:
        return []
    last = messages[-1]
    if last.role != "user" or not isinstance(last.content, list):
        return []
    results: list[dict[str, Any]] = []
    for block in last.content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if not tool_use_id:
            continue
        results.append(
            {
                "toolUseId": tool_use_id,
                "content": _result_content(block.get("content")),
                "status": "error" if block.get("is_error") else "success",
            }
        )
    return results


def _block_text(content: Any) -> str:
    """提取历史消息中的可读文本，不把结构化工具块重复写入内容。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def anthropic_tool_history(
    messages: list[AnthropicMessage],
) -> list[dict[str, Any]]:
    """重建 Runtime 接受当前 toolResults 所需的活动工具历史。"""
    results = anthropic_tool_results(messages)
    result_ids = {item["toolUseId"] for item in results}
    if not result_ids or len(messages) < 2:
        return []
    assistant_index = len(messages) - 2
    assistant = messages[assistant_index]
    if assistant.role != "assistant" or not isinstance(assistant.content, list):
        return []
    tool_uses = [
        {
            "toolUseId": block["id"],
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        }
        for block in assistant.content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("id") in result_ids
    ]
    if {item["toolUseId"] for item in tool_uses} != result_ids:
        return []
    user_content = "."
    if assistant_index > 0 and messages[assistant_index - 1].role == "user":
        user_content = _block_text(messages[assistant_index - 1].content) or "."
    return [
        {
            "userInputMessage": {
                "content": user_content,
                "origin": "AI_EDITOR",
            }
        },
        {
            "assistantResponseMessage": {
                "content": _block_text(assistant.content),
                "toolUses": tool_uses,
            }
        },
    ]


def openai_tool_results(messages: list[Message]) -> list[dict[str, Any]]:
    """从尾部 `role:tool` 消息提取工具结果 → Kiro toolResults。

    OpenAI 回路把每个工具结果作为独立的 `role:tool` 消息追加在
    `assistant.tool_calls` 之后，按 `tool_call_id` 关联。
    """
    collected: list[dict[str, Any]] = []
    for message in reversed(messages):
        if message.role == "tool":
            tool_call_id = message.tool_call_id
            if tool_call_id:
                collected.append(
                    {
                        "toolUseId": tool_call_id,
                        "content": _result_content(message.content),
                        "status": "success",
                    }
                )
        else:
            break
    collected.reverse()
    return collected


def openai_tool_history(messages: list[Message]) -> list[dict[str, Any]]:
    """重建 OpenAI 工具结果对应的 Runtime 活动工具历史。"""
    results = openai_tool_results(messages)
    result_ids = {item["toolUseId"] for item in results}
    if not result_ids:
        return []
    assistant_index = len(messages) - len(results) - 1
    if assistant_index < 0:
        return []
    assistant = messages[assistant_index]
    if assistant.role != "assistant":
        return []
    raw_calls = (assistant.model_extra or {}).get("tool_calls") or []
    tool_uses: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("id") not in result_ids:
            continue
        function = call.get("function") or {}
        raw_input = function.get("arguments", {})
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except (json.JSONDecodeError, ValueError):
                raw_input = {}
        tool_uses.append(
            {
                "toolUseId": call["id"],
                "name": function.get("name", ""),
                "input": raw_input if isinstance(raw_input, dict) else {},
            }
        )
    if {item["toolUseId"] for item in tool_uses} != result_ids:
        return []
    user_content = "."
    if assistant_index > 0 and messages[assistant_index - 1].role == "user":
        user_content = _block_text(messages[assistant_index - 1].content) or "."
    return [
        {
            "userInputMessage": {
                "content": user_content,
                "origin": "AI_EDITOR",
            }
        },
        {
            "assistantResponseMessage": {
                "content": _block_text(assistant.content),
                "toolUses": tool_uses,
            }
        },
    ]


class ToolCallAccumulator:
    """按 `id` 累积分片工具调用（`EventType.TOOL` 的 data）。

    出站层用于把分片的 `name`/`input` 收敛为完整工具调用，供非流式响应
    聚合，或供流式响应查询块顺序与索引。
    """

    def __init__(self) -> None:
        self._order: list[str] = []
        self._calls: dict[str, dict[str, str]] = {}

    def add(self, data: dict[str, Any]) -> None:
        tool_id = data.get("id", "")
        if not tool_id:
            return
        if tool_id not in self._calls:
            self._calls[tool_id] = {"name": data.get("name", ""), "input": ""}
            self._order.append(tool_id)
        if data.get("name"):
            self._calls[tool_id]["name"] = data["name"]
        self._calls[tool_id]["input"] += data.get("input", "")

    @property
    def has_calls(self) -> bool:
        return bool(self._order)

    def calls(self) -> list[dict[str, str]]:
        """按到达顺序返回完整调用：`{"id","name","input"}`（input 为 JSON 文本）。"""
        return [
            {"id": tool_id, "name": self._calls[tool_id]["name"], "input": self._calls[tool_id]["input"]}
            for tool_id in self._order
        ]


def parse_tool_input(raw: str) -> dict[str, Any]:
    """将累积的 input JSON 文本解析为对象；失败时回退空对象。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
