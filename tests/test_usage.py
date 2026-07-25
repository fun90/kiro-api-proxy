from kiro_api_proxy.usage import TokenUsage, estimate_tokens


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
