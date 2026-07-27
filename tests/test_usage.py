import pytest

from kiro_api_proxy.usage import (
    LARGE_CONTEXT_WINDOW,
    STANDARD_CONTEXT_WINDOW,
    TokenUsage,
    context_window_for_model,
    estimate_tokens,
)


def test_estimate_tokens_handles_chinese_and_ascii():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello world") == 4
    assert estimate_tokens("") == 1


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


def test_ensure_estimates_keeps_upstream_value_over_char_estimate():
    prompt = "这是一段很长的提示。" * 200
    usage = TokenUsage(input_tokens=15, context_tokens=0)
    usage.ensure_estimates(prompt, "输出")
    # 上游给了真实值就照用，字符估算只是兜底，不能反过来覆盖上游值——
    # 客户端靠 input_tokens 判断压缩时机，注入估算值会让时机偏离真实占用。
    assert usage.input_tokens == 15


def test_ensure_estimates_keeps_upstream_value_when_higher():
    prompt = "短"
    usage = TokenUsage(input_tokens=99999, context_tokens=0)
    usage.ensure_estimates(prompt, "输出")
    # 上游值高于字符估算时不被拉低。
    assert usage.input_tokens == 99999


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Claude 4.6 及更新为 1M 窗口（点号与短横线两种写法都要认）。
        ("claude-opus-4.8", LARGE_CONTEXT_WINDOW),
        ("claude-opus-4-8", LARGE_CONTEXT_WINDOW),
        ("claude-opus-4.7", LARGE_CONTEXT_WINDOW),
        ("claude-opus-4.6", LARGE_CONTEXT_WINDOW),
        ("claude-sonnet-4.6", LARGE_CONTEXT_WINDOW),
        ("claude-opus-4.8-thinking", LARGE_CONTEXT_WINDOW),
        ("CLAUDE-OPUS-4.8", LARGE_CONTEXT_WINDOW),
        # 4.5 及更早为 200K。
        ("claude-opus-4.5", STANDARD_CONTEXT_WINDOW),
        ("claude-sonnet-4.5", STANDARD_CONTEXT_WINDOW),
        ("claude-sonnet-4", STANDARD_CONTEXT_WINDOW),
        ("claude-haiku-4.5", STANDARD_CONTEXT_WINDOW),
        ("claude-3-5-sonnet", STANDARD_CONTEXT_WINDOW),
        ("unknown-model", STANDARD_CONTEXT_WINDOW),
        ("auto", STANDARD_CONTEXT_WINDOW),
    ],
)
def test_context_window_for_model_classifies_by_version(model, expected):
    # 档位判错会把占比换算成错误的绝对值：opus-4.8 当成 200K 会低估 5 倍。
    assert context_window_for_model(model) == expected


def test_context_usage_tokens_converts_percentage_by_model_window():
    usage = TokenUsage(context_percent=12.5)
    assert usage.context_usage_tokens("claude-opus-4.8") == 125_000
    assert usage.context_usage_tokens("claude-sonnet-4.5") == 25_000


def test_context_usage_tokens_prefers_upstream_window():
    # 上游 usageEvent 给了 size 就以它为准，不再按模型名猜档位。
    usage = TokenUsage(context_percent=10.0, context_window=400_000)
    assert usage.context_usage_tokens("claude-opus-4.8") == 40_000


def test_context_usage_tokens_zero_without_percentage():
    assert TokenUsage().context_usage_tokens("claude-opus-4.8") == 0


def test_ensure_estimates_prefers_percentage_over_turn_input():
    usage = TokenUsage(input_tokens=15, context_percent=30.0)
    usage.ensure_estimates("很短的提示", "输出", "claude-opus-4.8")
    # 占比换算出的累计占用远大于本轮 inputTokens，应当胜出。
    assert usage.input_tokens == 300_000


def test_ensure_estimates_ignores_zero_percentage():
    usage = TokenUsage(input_tokens=1200, context_percent=0.0)
    usage.ensure_estimates("很短的提示", "输出", "claude-opus-4.8")
    # 上游没给占比时不能换算出 0 覆盖已有真实值。
    assert usage.input_tokens == 1200


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
