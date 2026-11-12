"""Per-model request options for chat.completions.

Reasoning models (gpt-5*, gpt-6*, o-series) reject the options the older
gpt-4o-mini path used (measured 2026-11-11 with gpt-6-luna and gpt-5.6-luna):
- ``max_tokens`` -> 400, they take ``max_completion_tokens``;
- ``temperature`` other than the default 1 -> 400;
- ``reasoning_effort="minimal"`` -> 400, "low" works.
``max_completion_tokens`` also covers the hidden reasoning tokens: with 50 the
answer came back empty, so the visible-answer budget gets extra headroom.
Which model runs is still chosen only by OPENAI_MODEL.
"""
from __future__ import annotations

from typing import Any

REASONING_MODEL_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
# Headroom for reasoning tokens on top of the visible-answer budget. Only used
# tokens are billed; "low" effort used 0-80 reasoning tokens in the 11-11 test.
REASONING_TOKEN_HEADROOM = 1000
DEFAULT_REASONING_EFFORT = "low"


def is_reasoning_model(model: str) -> bool:
    return model.strip().lower().startswith(REASONING_MODEL_PREFIXES)


def _configured_reasoning_effort() -> str:
    try:
        from .config import get_settings

        return get_settings().openai_reasoning_effort
    except Exception:  # settings unavailable (e.g. unit tests without .env)
        return DEFAULT_REASONING_EFFORT


def completion_options(
    model: str,
    *,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Keyword arguments for chat.completions.create other than model/messages."""
    if not is_reasoning_model(model):
        return {"temperature": temperature, "max_tokens": max_tokens}
    effort = reasoning_effort if reasoning_effort is not None else _configured_reasoning_effort()
    options: dict[str, Any] = {"max_completion_tokens": max_tokens + REASONING_TOKEN_HEADROOM}
    if effort:
        options["reasoning_effort"] = effort
    return options
