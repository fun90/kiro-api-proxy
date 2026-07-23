from collections.abc import AsyncIterator

from kiro_api_proxy.config import Settings
from kiro_api_proxy.transports import (
    AdaptiveTransport,
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)
from kiro_api_proxy.transports.acp import AcpTransport


class FakeTransport:
    def __init__(self, name: str, error: TransportError | None = None):
        self.name = name
        self.error = error
        self.calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def models(self):
        return [{"model_id": "auto"}]

    async def generate(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.name

    async def stream(
        self, request
    ) -> AsyncIterator[GenerationEvent]:
        self.calls += 1
        if self.error:
            yield GenerationEvent(
                EventType.ERROR,
                text=str(self.error),
                data={"category": self.error.category.value},
            )
            return
        yield GenerationEvent(EventType.TEXT_DELTA, text=self.name)
        yield GenerationEvent(EventType.DONE)

    async def cancel(self, request_id):
        return None


async def test_adaptive_transport_falls_back_before_output():
    acp = FakeTransport(
        "acp",
        TransportError("协议损坏", ErrorCategory.PROTOCOL, retryable=True),
    )
    cli = FakeTransport("cli")
    router = AdaptiveTransport([acp, cli])
    result = await router.generate(GenerationRequest("auto", "提示"))
    assert result == "cli"
    assert router.actual_name == "cli"


async def test_adaptive_stream_does_not_mix_after_output():
    class Partial(FakeTransport):
        async def stream(self, request):
            yield GenerationEvent(EventType.TEXT_DELTA, text="部分")
            yield GenerationEvent(
                EventType.ERROR,
                text="EOF",
                data={"category": ErrorCategory.PROTOCOL.value},
            )

    cli = FakeTransport("cli")
    router = AdaptiveTransport([Partial("acp"), cli])
    events = [
        event
        async for event in router.stream(GenerationRequest("auto", "提示"))
    ]
    assert [event.text for event in events] == ["部分", "EOF"]
    assert cli.calls == 0


def test_acp_extracts_latest_user_turn():
    prompt = (
        "### 系统指令\n中文\n\n"
        "### 用户\n第一问\n\n"
        "### 助手\n第一答\n\n"
        "### 用户\n第二问\n\n### 助手\n"
    )
    assert AcpTransport._latest_user_turn(prompt) == "第二问"
