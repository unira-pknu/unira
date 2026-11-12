"""Reasoning models reject the options gpt-4o-mini used (400 on 2026-11-11)."""

from unittest.mock import MagicMock

from app.generation import _complete
from app.openai_params import REASONING_TOKEN_HEADROOM, completion_options, is_reasoning_model


def test_older_models_keep_temperature_and_max_tokens():
    assert completion_options("gpt-4o-mini", temperature=0.1, max_tokens=700) == {"temperature": 0.1, "max_tokens": 700}
    assert not is_reasoning_model("gpt-4.1-mini")


def test_reasoning_models_get_completion_budget_and_effort_without_temperature():
    for model in ("gpt-6-luna", "gpt-5.6-luna", "GPT-6.1-sol", "o3-mini"):
        options = completion_options(model, temperature=0.1, max_tokens=700, reasoning_effort="low")
        assert options == {"max_completion_tokens": 700 + REASONING_TOKEN_HEADROOM, "reasoning_effort": "low"}


def test_empty_effort_leaves_the_model_default():
    assert "reasoning_effort" not in completion_options("gpt-6-luna", temperature=0, max_tokens=450, reasoning_effort="")


def test_generation_sends_reasoning_options_to_the_api():
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [MagicMock(message=MagicMock(content="답"))]
    _complete(client, "gpt-6-luna", [{"role": "user", "content": "q"}], temperature=0.1, max_tokens=700, timeout=5)
    kwargs = client.chat.completions.create.call_args.kwargs
    assert "temperature" not in kwargs and "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == 700 + REASONING_TOKEN_HEADROOM
    assert kwargs["model"] == "gpt-6-luna"
