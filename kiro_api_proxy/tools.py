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
    """OpenAI Chat/Responses `tools` → Kiro toolSpecification 列表。"""
    specs: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") not in (None, "function"):
            continue
        # Chat Completions 将定义放在 function 下，Responses API 使用扁平结构。
        function = tool.get("function") or tool
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


def _openai_result_status(message: Message) -> str:
    extra = message.model_extra or {}
    status = str(extra.get("status", "")).lower()
    failed_statuses = {"error", "failed", "failure", "cancelled", "incomplete"}
    return "error" if extra.get("is_error") is True or status in failed_statuses else "success"


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


def _anthropic_results_from_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    results: list[dict[str, Any]] = []
    for block in content:
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
    if last.role != "user":
        return []
    return _anthropic_results_from_content(last.content)


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


def _anthropic_tool_uses(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [
        {
            "toolUseId": block["id"],
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        }
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("id")
    ]


def anthropic_tool_history(
    messages: list[AnthropicMessage],
) -> list[dict[str, Any]]:
    """重建 Runtime 接受当前 toolResults 所需的完整工具历史。"""
    current_results = anthropic_tool_results(messages)
    current_result_ids = {item["toolUseId"] for item in current_results}
    if not current_result_ids or len(messages) < 2:
        return []
    history: list[dict[str, Any]] = []
    pending_tool_ids: set[str] = set()

    # 最后一条 user 消息作为 currentMessage 的 toolResults 发送；之前的所有
    # 工具往返都必须保留在 history，否则长 Agent 回路会遗失早期读取结果。
    for message in messages[:-1]:
        if message.role == "user":
            results = _anthropic_results_from_content(message.content)
            result_ids = {item["toolUseId"] for item in results}
            if result_ids:
                if result_ids != pending_tool_ids:
                    return []
            elif pending_tool_ids:
                return []
            payload: dict[str, Any] = {
                "content": _block_text(message.content) or ".",
                "origin": "AI_EDITOR",
            }
            if results:
                payload["userInputMessageContext"] = {
                    "toolResults": results,
                }
            history.append({"userInputMessage": payload})
            pending_tool_ids = set()
        elif message.role == "assistant":
            if pending_tool_ids:
                return []
            tool_uses = _anthropic_tool_uses(message.content)
            payload = {
                "content": _block_text(message.content),
            }
            if tool_uses:
                payload["toolUses"] = tool_uses
                pending_tool_ids = {
                    item["toolUseId"] for item in tool_uses
                }
            history.append({"assistantResponseMessage": payload})

    if pending_tool_ids != current_result_ids:
        return []
    return history


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
                        "status": _openai_result_status(message),
                    }
                )
        else:
            break
    collected.reverse()
    return collected


def openai_tool_history(messages: list[Message]) -> list[dict[str, Any]]:
    """重建 OpenAI 工具结果对应的 Runtime 完整工具历史。"""
    current_results = openai_tool_results(messages)
    current_result_ids = {item["toolUseId"] for item in current_results}
    if not current_result_ids:
        return []
    history: list[dict[str, Any]] = []
    pending_tool_ids: set[str] = set()
    history_messages = messages[: -len(current_results)]
    index = 0

    while index < len(history_messages):
        message = history_messages[index]
        if message.role in {"system", "developer"}:
            index += 1
            continue
        if message.role == "user":
            if pending_tool_ids:
                return []
            history.append(
                {
                    "userInputMessage": {
                        "content": _block_text(message.content) or ".",
                        "origin": "AI_EDITOR",
                    }
                }
            )
        elif message.role == "assistant":
            if pending_tool_ids:
                return []
            tool_uses = _openai_tool_uses(message)
            payload: dict[str, Any] = {
                "content": _block_text(message.content),
            }
            if tool_uses:
                payload["toolUses"] = tool_uses
                pending_tool_ids = {
                    item["toolUseId"] for item in tool_uses
                }
            history.append({"assistantResponseMessage": payload})
        elif message.role == "tool":
            results: list[dict[str, Any]] = []
            while (
                index < len(history_messages)
                and history_messages[index].role == "tool"
            ):
                tool_message = history_messages[index]
                if tool_message.tool_call_id:
                    results.append(
                        {
                            "toolUseId": tool_message.tool_call_id,
                            "content": _result_content(
                                tool_message.content
                            ),
                            "status": _openai_result_status(tool_message),
                        }
                    )
                index += 1
            result_ids = {item["toolUseId"] for item in results}
            if not result_ids or result_ids != pending_tool_ids:
                return []
            history.append(
                {
                    "userInputMessage": {
                        "content": ".",
                        "origin": "AI_EDITOR",
                        "userInputMessageContext": {
                            "toolResults": results,
                        },
                    }
                }
            )
            pending_tool_ids = set()
            continue
        index += 1

    if pending_tool_ids != current_result_ids:
        return []
    return history


def _openai_tool_uses(message: Message) -> list[dict[str, Any]]:
    raw_calls = (message.model_extra or {}).get("tool_calls") or []
    tool_uses: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, dict) or not call.get("id"):
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
    return tool_uses


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
