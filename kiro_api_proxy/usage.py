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
        usage = data.get("usage")
        source = usage if isinstance(usage, dict) else data
        self.input_tokens = _non_negative(
            source, "input_tokens", "inputTokens", default=self.input_tokens
        )
        self.output_tokens = _non_negative(
            source, "output_tokens", "outputTokens", default=self.output_tokens
        )
        self.cache_read_input_tokens = _non_negative(
            source,
            "cache_read_input_tokens",
            "cached_read_tokens",
            "cachedReadTokens",
            default=self.cache_read_input_tokens,
        )
        self.cache_creation_input_tokens = _non_negative(
            source,
            "cache_creation_input_tokens",
            "cached_write_tokens",
            "cachedWriteTokens",
            default=self.cache_creation_input_tokens,
        )
        self.reasoning_tokens = _non_negative(
            source,
            "reasoning_tokens",
            "thought_tokens",
            "thoughtTokens",
            default=self.reasoning_tokens,
        )
        self.context_tokens = _non_negative(
            source,
            "context_tokens",
            "used",
            default=self.context_tokens,
        )
        self.context_window = _non_negative(
            source,
            "context_window",
            "size",
            default=self.context_window,
        )

    def ensure_estimates(self, prompt: str, output: str) -> None:
        if self.input_tokens <= 0:
            # 上游未给出 input_tokens 时，优先采用其上报的真实上下文用量
            # （usage_update 的 used），否则退回字符级估算。
            self.input_tokens = self.context_tokens or estimate_tokens(prompt)
        if self.output_tokens <= 0:
            self.output_tokens = estimate_tokens(output)

    def chat_completions(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "prompt_tokens_details": {
                "cached_tokens": self.cache_read_input_tokens,
                "cache_write_tokens": self.cache_creation_input_tokens,
            },
            "completion_tokens_details": {
                "reasoning_tokens": self.reasoning_tokens,
            },
        }

    def responses(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "input_tokens_details": {
                "cached_tokens": self.cache_read_input_tokens,
                "cache_write_tokens": self.cache_creation_input_tokens,
            },
            "output_tokens": self.output_tokens,
            "output_tokens_details": {
                "reasoning_tokens": self.reasoning_tokens,
            },
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
