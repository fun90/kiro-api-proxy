import json
import os
from pathlib import Path
from types import SimpleNamespace

import kiro_api_proxy.prompts as prompts
from kiro_api_proxy.config import Settings
from kiro_api_proxy.prompts import (
    claude_session_working_directory,
    content_text,
    messages_to_prompt,
    prompt_working_directory,
)
from kiro_api_proxy.schemas import Message


def test_content_text_excludes_structured_tool_blocks():
    content = [
        {"type": "text", "text": "先读取文件。"},
        {
            "type": "tool_use",
            "id": "tool_1",
            "name": "Read",
            "input": {"file_path": "/tmp/example.py"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "tool_1",
            "content": "敏感的工具输出",
        },
    ]

    assert content_text(content) == "先读取文件。"


def test_messages_to_prompt_does_not_leak_tool_protocol_into_text():
    messages = [
        Message(role="user", content="检查文件"),
        Message(
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/example.py"},
                }
            ],
        ),
        Message(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "工具执行结果",
                }
            ],
        ),
        Message(
            role="tool",
            content="OpenAI 工具执行结果",
            tool_call_id="call_1",
        ),
    ]

    prompt = messages_to_prompt(messages)

    assert "检查文件" in prompt
    assert "Read" not in prompt
    assert "tool_1" not in prompt
    assert "工具执行结果" not in prompt
    assert prompt.count("### 用户\n") == 1
    assert prompt.endswith("### 助手\n")


def test_prompt_working_directory_accepts_project_under_configured_root(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    monkeypatch.setattr(
        prompts,
        "settings",
        SimpleNamespace(working_directory=str(workspace)),
    )

    prompt = (
        "### 系统指令\n"
        "<env>\n"
        f"Working directory: {project}\n"
        "</env>\n"
    )

    assert prompt_working_directory(prompt) == str(project.resolve())


def test_prompt_working_directory_rejects_path_outside_configured_root(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        prompts,
        "settings",
        SimpleNamespace(working_directory=str(workspace)),
    )

    assert (
        prompt_working_directory(f"Working directory: {outside}")
        is None
    )


def test_claude_session_working_directory_reads_transcript(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    projects_root = tmp_path / ".claude" / "projects"
    transcript_dir = projects_root / "-workspace-project"
    transcript_dir.mkdir(parents=True)
    session_id = "12345678-1234-4234-8234-123456789abc"
    transcript = transcript_dir / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "cwd": str(project)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        prompts,
        "settings",
        SimpleNamespace(working_directory=str(workspace)),
    )

    assert (
        claude_session_working_directory(session_id, projects_root)
        == str(project.resolve())
    )
