from httpx import ASGITransport, AsyncClient
import logging

from kiro_api_proxy import main
from kiro_api_proxy.transports import EventType, GenerationEvent


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


async def test_health_supports_head():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.head("/health")
    assert response.status_code == 200


async def test_root_supports_head():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.head("/")
    assert response.status_code == 200


async def test_chat(monkeypatch):
    async def fake_events(
        model, prompt, effort=None, client_request=None, session_id=None
    ):
        assert model == "auto"
        assert "### 用户\n你好" in prompt
        assert "所有面向用户的自然语言必须使用简体中文" in prompt
        assert effort == "medium"
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好！")
        yield GenerationEvent(
            EventType.USAGE,
            data={"input_tokens": 42, "output_tokens": 3},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", fake_events)
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
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "你好！"
    # 非流式响应回传上游真实用量，而非字符估算。
    assert body["usage"]["prompt_tokens"] == 42
    assert body["usage"]["completion_tokens"] == 3


async def test_responses(monkeypatch):
    async def fake_events(
        model, prompt, effort=None, client_request=None, session_id=None
    ):
        yield GenerationEvent(EventType.TEXT_DELTA, text="OK")
        yield GenerationEvent(
            EventType.USAGE,
            data={"input_tokens": 10, "output_tokens": 1},
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", fake_events)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "auto", "input": "测试"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == "OK"
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 1


async def test_anthropic_messages(monkeypatch):
    async def fake_events(
        model, prompt, effort=None, client_request=None, session_id=None
    ):
        assert model == "claude-sonnet-5-thinking"
        assert "### 系统指令\n使用中文" in prompt
        yield GenerationEvent(EventType.TEXT_DELTA, text="你好！")
        yield GenerationEvent(
            EventType.USAGE,
            data={
                "input_tokens": 55,
                "output_tokens": 4,
                "cache_read_input_tokens": 20,
            },
        )
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", fake_events)
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
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "你好！"
    # 非流式 Anthropic 响应回传上游真实用量（含缓存 token）。
    assert body["usage"]["input_tokens"] == 55
    assert body["usage"]["output_tokens"] == 4
    assert body["usage"]["cache_read_input_tokens"] == 20


async def test_anthropic_system_role_in_messages(monkeypatch):
    async def fake_events(
        model, prompt, effort=None, client_request=None, session_id=None
    ):
        assert "### 系统指令\n系统消息" in prompt
        yield GenerationEvent(EventType.TEXT_DELTA, text="OK")
        yield GenerationEvent(EventType.DONE)

    monkeypatch.setattr(main, "_events", fake_events)
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
