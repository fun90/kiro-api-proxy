from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


TOKEN_PART_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]|\S")


def estimate_tokens(text: str) -> int:
    """在没有上游 tokenizer 时提供稳定、偏保守的 token 估算。"""
    total = 0
    for part in TOKEN_PART_PATTERN.findall(text):
        if part.isascii() and part.replace("_", "").isalnum():
            total += max(1, math.ceil(len(part) / 4))
        else:
            total += 1
    return max(1, total)


# 各 usage 字段 → 上游可能出现的别名（本地 snake_case 优先，其次上游 camelCase）。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_read_input_tokens": (
        "cache_read_input_tokens",
        "cached_read_tokens",
        "cachedReadTokens",
    ),
    "cache_creation_input_tokens": (
        "cache_creation_input_tokens",
        "cached_write_tokens",
        "cachedWriteTokens",
    ),
    "reasoning_tokens": (
        "reasoning_tokens",
        "thought_tokens",
        "thoughtTokens",
    ),
    "context_tokens": ("context_tokens", "used"),
    "context_window": ("context_window", "size"),
}


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    context_tokens: int = 0
    context_window: int = 0

    def update(self, data: dict[str, Any]) -> None:
        # 上游可能把用量放在顶层或嵌套在 usage 里；缺失字段不覆盖已累积的真实值。
        usage = data.get("usage")
        source = usage if isinstance(usage, dict) else data
        for attr, keys in _FIELD_ALIASES.items():
            setattr(
                self,
                attr,
                _non_negative(source, *keys, default=getattr(self, attr)),
            )

    def ensure_estimates(self, prompt: str, output: str) -> None:
        # 上游（Kiro）上报的 inputTokens 常严重低估：它可能未计入系统提示、
        # 完整历史等实际发送内容，导致与真实 prompt 规模相差数倍。为避免向客户端
        # 透传失真的低值，input_tokens 取「上游上报值、累计上下文占用、prompt
        # 字符估算」三者的最大值——字符估算既是三者皆缺时的兜底，也是上游低估时的纠偏。
        self.input_tokens = max(
            self.input_tokens,
            self.context_tokens,
            estimate_tokens(prompt),
        )
        # output_tokens 数自模型自身生成，上游一般不低估，故仅在缺失时才估算。
        if self.output_tokens <= 0:
            self.output_tokens = estimate_tokens(output)

    def _input_details(self) -> dict[str, int]:
        return {
            "cached_tokens": self.cache_read_input_tokens,
            "cache_write_tokens": self.cache_creation_input_tokens,
        }

    def _output_details(self) -> dict[str, int]:
        return {"reasoning_tokens": self.reasoning_tokens}

    def chat_completions(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "prompt_tokens_details": self._input_details(),
            "completion_tokens_details": self._output_details(),
        }

    def responses(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "input_tokens_details": self._input_details(),
            "output_tokens": self.output_tokens,
            "output_tokens_details": self._output_details(),
            "total_tokens": self.input_tokens + self.output_tokens,
        }

    def anthropic(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


def _non_negative(
    source: dict[str, Any], *keys: str, default: int = 0
) -> int:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return default
