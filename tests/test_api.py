from httpx import ASGITransport, AsyncClient
import logging

from kiro_api_proxy import main


def test_structured_log_redacts_sensitive_fields(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        main._log(
            "test",
            transport="cli",
            authorization="Bearer secret",
            api_key="secret",
            token="secret",
        )
    message = caplog.records[-1].message
    assert '"transport": "cli"' in message
    assert "secret" not in message


def test_thinking_model_alias():
    assert main.resolve_model("gpt-5.6-luna-thinking") == (
        "gpt-5.6-luna",
        "high",
    )
    assert main.resolve_model("gpt-5.6-luna") == ("gpt-5.6-luna", "")
    assert main.resolve_model("claude-opus-4-8[1m]") == (
        "claude-opus-4.8",
        "",
    )
    assert main.resolve_model("claude-opus-4-8-thinking[1m]") == (
        "claude-opus-4.8",
        "high",
    )


async def test_health():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_chat(monkeypatch):
    async def fake_call(
        model: str, prompt: str, effort: str | None = None
    ) -> str:
        assert model == "auto"
        assert "### 用户\n你好" in prompt
        assert "所有面向用户的自然语言必须使用简体中文" in prompt
        assert effort == "medium"
        return "你好！"

    monkeypatch.setattr(main, "call_kiro", fake_call)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "reasoningEffort": "medium",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "你好！"


async def test_responses(monkeypatch):
    async def fake_call(
        model: str, prompt: str, effort: str | None = None
    ) -> str:
        return "OK"

    monkeypatch.setattr(main, "call_kiro", fake_call)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "auto", "input": "测试"},
        )
    assert response.status_code == 200
    assert response.json()["output_text"] == "OK"


async def test_anthropic_messages(monkeypatch):
    async def fake_call(model: str, prompt: str) -> str:
        assert model == "claude-sonnet-5-thinking"
        assert "### 系统指令\n使用中文" in prompt
        return "你好！"

    monkeypatch.setattr(main, "call_kiro", fake_call)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-5",
                "system": "使用中文",
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "你好"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["type"] == "message"
    assert response.json()["content"][0]["text"] == "你好！"


async def test_anthropic_system_role_in_messages(monkeypatch):
    async def fake_call(model: str, prompt: str) -> str:
        assert "### 系统指令\n系统消息" in prompt
        return "OK"

    monkeypatch.setattr(main, "call_kiro", fake_call)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "你好"},
                    {"role": "system", "content": "系统消息"},
                ],
            },
        )
    assert response.status_code == 200


async def test_anthropic_count_tokens():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "你好"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0
