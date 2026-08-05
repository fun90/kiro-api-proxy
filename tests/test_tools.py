"""工具转换模块单元测试。"""

from __future__ import annotations

from kiro_api_proxy.schemas import AnthropicMessage, Message
from kiro_api_proxy.tools import (
    anthropic_tool_history,
    anthropic_tool_results,
    anthropic_tools_to_specs,
    openai_tool_history,
    openai_tool_results,
    openai_tools_to_specs,
)


class TestAnthropicToolsToSpecs:
    def test_converts_tool_definition(self):
        tools = [
            {
                "name": "get_weather",
                "description": "查询天气",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]
        specs = anthropic_tools_to_specs(tools)
        assert len(specs) == 1
        spec = specs[0]["toolSpecification"]
        assert spec["name"] == "get_weather"
        assert spec["description"] == "查询天气"
        assert spec["inputSchema"]["json"]["required"] == ["city"]

    def test_missing_schema_defaults_to_object(self):
        specs = anthropic_tools_to_specs([{"name": "noop"}])
        assert specs[0]["toolSpecification"]["inputSchema"]["json"] == {
            "type": "object",
            "properties": {},
        }

    def test_skips_nameless_and_empty(self):
        assert anthropic_tools_to_specs(None) == []
        assert anthropic_tools_to_specs([{"description": "x"}]) == []


class TestOpenAIToolsToSpecs:
    def test_converts_function_tool(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "description": "相加",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}},
                    },
                },
            }
        ]
        specs = openai_tools_to_specs(tools)
        assert specs[0]["toolSpecification"]["name"] == "add"
        assert specs[0]["toolSpecification"]["description"] == "相加"
        assert "a" in specs[0]["toolSpecification"]["inputSchema"]["json"]["properties"]

    def test_skips_non_function_type(self):
        assert openai_tools_to_specs([{"type": "retrieval"}]) == []


class TestAnthropicToolResults:
    def test_extracts_from_last_user_message(self):
        messages = [
            AnthropicMessage(role="user", content="北京天气？"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "tuse_1",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "tuse_1",
                        "content": "晴 25℃",
                    }
                ],
            ),
        ]
        results = anthropic_tool_results(messages)
        assert results == [
            {
                "toolUseId": "tuse_1",
                "content": [{"text": "晴 25℃"}],
                "status": "success",
            }
        ]

    def test_is_error_maps_to_error_status(self):
        messages = [
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "tuse_2",
                        "content": [{"type": "text", "text": "boom"}],
                        "is_error": True,
                    }
                ],
            )
        ]
        results = anthropic_tool_results(messages)
        assert results[0]["status"] == "error"
        assert results[0]["content"] == [{"text": "boom"}]

    def test_no_tool_result_returns_empty(self):
        messages = [AnthropicMessage(role="user", content="hi")]
        assert anthropic_tool_results(messages) == []

    def test_builds_matching_active_tool_history(self):
        messages = [
            AnthropicMessage(role="user", content="北京天气？"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {"type": "text", "text": "我来查询。"},
                    {
                        "type": "tool_use",
                        "id": "tuse_1",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    },
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "tuse_1",
                        "content": "晴",
                    }
                ],
            ),
        ]
        history = anthropic_tool_history(messages)
        assert history[0]["userInputMessage"]["content"] == "北京天气？"
        assistant = history[1]["assistantResponseMessage"]
        assert assistant["content"] == "我来查询。"
        assert assistant["toolUses"] == [
            {
                "toolUseId": "tuse_1",
                "name": "get_weather",
                "input": {"city": "北京"},
            }
        ]

    def test_preserves_completed_tool_turns_in_history(self):
        messages = [
            AnthropicMessage(role="user", content="依次读取两个文件"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "read_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/one.py"},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_1",
                        "content": "第一个文件的内容",
                    }
                ],
            ),
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "read_2",
                        "name": "Read",
                        "input": {"file_path": "/tmp/two.py"},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_2",
                        "content": "第二个文件的内容",
                    }
                ],
            ),
        ]

        history = anthropic_tool_history(messages)

        assert len(history) == 4
        historical_results = history[2]["userInputMessage"][
            "userInputMessageContext"
        ]["toolResults"]
        assert historical_results[0]["toolUseId"] == "read_1"
        assert historical_results[0]["content"] == [
            {"text": "第一个文件的内容"}
        ]
        assert history[3]["assistantResponseMessage"]["toolUses"][0][
            "toolUseId"
        ] == "read_2"

    def test_orphaned_result_has_no_structured_history(self):
        messages = [
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "missing",
                        "content": "晴",
                    }
                ],
            )
        ]
        assert anthropic_tool_history(messages) == []

    def test_tool_only_assistant_gets_placeholder_content(self):
        """只发 tool_use、无前置文本的 assistant 消息不能留空 content。

        Claude Code 的常见形态（含 thinking + tool_use，无 text 块）。
        Runtime 拒收空 content，会返回 HTTP 400。
        """
        messages = [
            AnthropicMessage(role="user", content="看下 README"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {"type": "thinking", "thinking": "我该读文件"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    },
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "# hi",
                    }
                ],
            ),
        ]

        history = anthropic_tool_history(messages)

        assert history[1]["assistantResponseMessage"]["content"] == "."

    def test_consecutive_user_messages_drop_history(self):
        """连续 user 消息破坏 history 交替性，应放弃结构化历史。

        客户端会在一轮里追加多条 user 消息（提醒注入、中断提示），
        Runtime 不接受连续同角色历史。
        """
        messages = [
            AnthropicMessage(role="user", content="第一句"),
            AnthropicMessage(role="user", content="补充一句"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "Read",
                        "input": {},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_3",
                        "content": "x",
                    }
                ],
            ),
        ]

        assert anthropic_tool_history(messages) == []


class TestOpenAIToolResults:
    def test_extracts_trailing_tool_messages(self):
        messages = [
            Message(role="user", content="15*23?"),
            Message(role="assistant", content=""),
            Message(role="tool", content="345", tool_call_id="call_1"),
            Message(role="tool", content="ok", tool_call_id="call_2"),
        ]
        results = openai_tool_results(messages)
        assert [r["toolUseId"] for r in results] == ["call_1", "call_2"]
        assert results[0]["content"] == [{"text": "345"}]

    def test_stops_at_non_tool_message(self):
        messages = [
            Message(role="tool", content="stale", tool_call_id="old"),
            Message(role="user", content="new question"),
        ]
        assert openai_tool_results(messages) == []

    def test_builds_matching_active_tool_history(self):
        messages = [
            Message(role="user", content="15*23?"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": '{"a":15,"b":23}',
                        },
                    }
                ],
            ),
            Message(role="tool", content="345", tool_call_id="call_1"),
        ]
        history = openai_tool_history(messages)
        tool_use = history[1]["assistantResponseMessage"]["toolUses"][0]
        assert tool_use == {
            "toolUseId": "call_1",
            "name": "multiply",
            "input": {"a": 15, "b": 23},
        }

    def test_preserves_completed_tool_turns_in_history(self):
        messages = [
            Message(role="user", content="依次计算"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": '{"a":2,"b":3}',
                        },
                    }
                ],
            ),
            Message(
                role="tool",
                content="6",
                tool_call_id="call_1",
            ),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": '{"a":6,"b":4}',
                        },
                    }
                ],
            ),
            Message(
                role="tool",
                content="24",
                tool_call_id="call_2",
            ),
        ]

        history = openai_tool_history(messages)

        assert len(history) == 4
        historical_results = history[2]["userInputMessage"][
            "userInputMessageContext"
        ]["toolResults"]
        assert historical_results == [
            {
                "toolUseId": "call_1",
                "content": [{"text": "6"}],
                "status": "success",
            }
        ]
        assert history[3]["assistantResponseMessage"]["toolUses"][0][
            "toolUseId"
        ] == "call_2"

    def test_tool_only_assistant_gets_placeholder_content(self):
        """只带 tool_calls、content 为空的 assistant 消息需补占位符。"""
        messages = [
            Message(role="user", content="15*23?"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": '{"a":15,"b":23}',
                        },
                    }
                ],
            ),
            Message(role="tool", content="345", tool_call_id="call_1"),
        ]

        history = openai_tool_history(messages)

        assert history[1]["assistantResponseMessage"]["content"] == "."

    def test_consecutive_user_messages_drop_history(self):
        """连续 user 消息破坏交替性，应放弃结构化历史。"""
        messages = [
            Message(role="user", content="第一句"),
            Message(role="user", content="补充一句"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": "{}",
                        },
                    }
                ],
            ),
            Message(role="tool", content="345", tool_call_id="call_1"),
        ]

        assert openai_tool_history(messages) == []
