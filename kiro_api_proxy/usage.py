from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


TOKEN_PART_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]|\S")

# Claude 模型版本号（点号或短横线两种写法），用于判定上下文窗口档位。
CLAUDE_VERSION_PATTERN = re.compile(r"claude-(?:opus|sonnet|haiku)-(\d+)[.-](\d+)")

LARGE_CONTEXT_WINDOW = 1_000_000
STANDARD_CONTEXT_WINDOW = 200_000
# 版本号解析失败时的兜底子串（覆盖 4.6+ 的非标准标识符）。
_LARGE_CONTEXT_TAGS = ("4.6", "4-6", "4.7", "4-7", "4.8", "4-8", "4.9", "4-9")


def is_large_context_model(model: str) -> bool:
    """判断模型是否属于 1M 上下文窗口档位。

    按 Kiro 的 ListAvailableModels：Claude 4.6 及更新（sonnet-4.6、opus-4.6、
    opus-4.7、opus-4.8 以及后续 4.x）为 1M 窗口，4.5 及更早为 200K。
    """
    normalized = model.lower()
    match = CLAUDE_VERSION_PATTERN.search(normalized)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        if major > 4:
            return True
        return major == 4 and minor >= 6
    return any(tag in normalized for tag in _LARGE_CONTEXT_TAGS)


def context_window_for_model(model: str, default: int = 0) -> int:
    """返回模型的上下文窗口 token 数。

    该值用于把上游的 contextUsagePercentage 换算成绝对 token 数——客户端靠这个
    数字决定何时压缩上下文，窗口取小了会低估占用、导致压缩不及时。
    """
    if is_large_context_model(model):
        return LARGE_CONTEXT_WINDOW
    return default if default > 0 else STANDARD_CONTEXT_WINDOW


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

# 上下文占用百分比是浮点数，与整型 token 字段分开提取。
_PERCENT_ALIASES = (
    "context_percent",
    "contextUsagePercentage",
    "context_usage_percentage",
    "contextUsagePercent",
)


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    context_tokens: int = 0
    context_window: int = 0
    context_percent: float = 0.0

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
        self.context_percent = _non_negative_float(
            source, *_PERCENT_ALIASES, default=self.context_percent
        )

    def context_usage_tokens(self, model: str = "") -> int:
        """按上游上报的占比换算出的上下文占用 token 数。

        上游的 contextUsageEvent 只给百分比，绝对值要乘模型窗口才能得到。
        window 优先用上游 usageEvent 里的 size，其次按模型版本判定档位。
        """
        if self.context_percent <= 0:
            return 0
        window = self.context_window or context_window_for_model(model)
        return int(self.context_percent * window / 100.0)

    def ensure_estimates(self, prompt: str, output: str, model: str = "") -> None:
        # input_tokens 优先级：按 contextUsagePercentage 换算的上下文占用 >
        # 上游累计占用（used）> 上游本轮 inputTokens > prompt 字符估算。
        # 前三者都是上游真实值，取最大者；字符估算只在三者皆缺时兜底，绝不用来
        # 覆盖上游真实值——客户端靠 input_tokens 决定何时压缩上下文，注入估算值
        # 会让压缩时机偏离真实占用。
        upstream = max(
            self.context_usage_tokens(model),
            self.context_tokens,
            self.input_tokens,
        )
        self.input_tokens = upstream if upstream > 0 else estimate_tokens(prompt)
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


def _non_negative_float(
    source: dict[str, Any], *keys: str, default: float = 0.0
) -> float:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value >= 0:
                return float(value)
    return default
