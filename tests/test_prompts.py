import json
import os
from pathlib import Path
from types import SimpleNamespace

import kiro_api_proxy.prompts as prompts
from kiro_api_proxy.config import Settings
from kiro_api_proxy.prompts import (
    claude_session_working_directory,
    prompt_working_directory,
)
from kiro_api_proxy.transports.base import kiro_environment
from kiro_api_proxy.transports.cli import CliTransport


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


def test_kiro_environment_uses_layered_tool_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    project = tmp_path / "workspace" / "project"

    environment = kiro_environment(
        ("~/custom/bin", "/opt/team-tools"),
        str(project),
    )

    path_items = environment["PATH"].split(":")
    assert path_items[:6] == [
        str(tmp_path / "custom" / "bin"),
        "/opt/team-tools",
        str(project / ".venv" / "bin"),
        str(project / "venv" / "bin"),
        str(project / "node_modules" / ".bin"),
        str(tmp_path / ".local" / "bin"),
    ]
    assert str(tmp_path / ".npm-global" / "bin") in path_items
    assert str(tmp_path / ".local" / "share" / "pnpm") in path_items
    assert path_items[-1] == "/usr/bin"


def test_settings_parses_extra_path(monkeypatch):
    monkeypatch.setenv(
        "KIRO_EXTRA_PATH",
        os.pathsep.join(("/opt/team-tools", "~/private-tools")),
    )

    loaded = Settings.from_env()

    assert loaded.extra_path == ("/opt/team-tools", "~/private-tools")


def test_cli_environment_uses_request_project_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    project = tmp_path / "workspace" / "project"
    transport = CliTransport(
        SimpleNamespace(
            max_concurrency=1,
            extra_path=("/opt/team-tools",),
            working_directory=str(tmp_path / "workspace"),
        )
    )

    path_items = transport._environment(str(project))["PATH"].split(":")

    assert path_items[:4] == [
        "/opt/team-tools",
        str(project / ".venv" / "bin"),
        str(project / "venv" / "bin"),
        str(project / "node_modules" / ".bin"),
    ]


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
