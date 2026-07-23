from types import SimpleNamespace

from kiro_api_proxy.transports import EventType
from kiro_api_proxy.transports.acp import (
    _event_from_prompt_usage,
    _event_from_update,
)
from kiro_api_proxy.usage import TokenUsage, estimate_tokens


class Dumpable(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def test_estimate_tokens_handles_chinese_and_ascii():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello world") == 4
    assert estimate_tokens("") == 1


def test_acp_context_usage_is_normalized():
    event = _event_from_update(
        Dumpable(session_update="usage_update", used=1234, size=200000)
    )
    assert event is not None
    assert event.type is EventType.USAGE
    assert event.data == {
        "context_tokens": 1234,
        "context_window": 200000,
    }


def test_acp_prompt_usage_is_normalized():
    event = _event_from_prompt_usage(
        Dumpable(
            inputTokens=100,
            outputTokens=20,
            totalTokens=120,
            cachedReadTokens=80,
            cachedWriteTokens=10,
        )
    )
    assert event is not None
    usage = TokenUsage()
    usage.update(event.data)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_read_input_tokens == 80
    assert usage.cache_creation_input_tokens == 10
