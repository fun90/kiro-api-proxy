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
            thoughtTokens=5,
        )
    )
    assert event is not None
    usage = TokenUsage()
    usage.update(event.data)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_read_input_tokens == 80
    assert usage.cache_creation_input_tokens == 10
    assert usage.reasoning_tokens == 5


def test_ensure_estimates_prefers_context_tokens_for_input():
    usage = TokenUsage(context_tokens=4096)
    usage.ensure_estimates("很短的提示", "输出")
    # 缺少独立 input_tokens 时优先采用上游上报的真实上下文用量。
    assert usage.input_tokens == 4096
    assert usage.output_tokens > 0


def test_ensure_estimates_prefers_larger_context_over_turn_input():
    usage = TokenUsage(input_tokens=12, context_tokens=4096)
    usage.ensure_estimates("很短的提示", "输出")
    # 持久会话的本轮输入不能覆盖累计上下文占用。
    assert usage.input_tokens == 4096


def test_ensure_estimates_falls_back_to_char_estimate():
    usage = TokenUsage()
    usage.ensure_estimates("你好世界", "输出")
    # 上游既无 input_tokens 也无 context_tokens 时才退回字符估算。
    assert usage.input_tokens == estimate_tokens("你好世界")


def test_anthropic_usage_includes_cache_fields():
    usage = TokenUsage(
        input_tokens=120,
        output_tokens=8,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=5,
    )
    assert usage.anthropic() == {
        "input_tokens": 120,
        "output_tokens": 8,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 5,
    }
