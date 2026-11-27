"""Model request compatibility for completion, streaming and query planning."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import openai_params
from app.config import Settings
from app.generation import CompletionLimitReached, _complete, _stream_complete, generate_answer, stream_answer
from app.openai_params import REASONING_TOKEN_HEADROOM, completion_options, is_reasoning_model
from app.query_rewrite import plan_question


def test_older_models_keep_temperature_and_max_tokens():
    assert completion_options("gpt-4o-mini", temperature=0.1, max_tokens=700) == {
        "temperature": 0.1, "max_tokens": 700,
    }
    assert not is_reasoning_model("gpt-4.1-mini")


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna", "GPT-6.1-sol", "o3-mini"])
def test_reasoning_models_get_completion_budget_and_effort_without_temperature(model):
    assert completion_options(model, temperature=0.1, max_tokens=700, reasoning_effort="medium") == {
        "max_completion_tokens": 700 + REASONING_TOKEN_HEADROOM, "reasoning_effort": "medium",
    }


def test_empty_effort_leaves_the_model_default():
    assert "reasoning_effort" not in completion_options(
        "gpt-6-luna", temperature=0, max_tokens=450, reasoning_effort="",
    )


def test_effort_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    settings = Settings(
        _env_file=None, SUPABASE_URL="https://example.supabase.co",
        supabase_service_role_key="test", openai_api_key="test",
    )
    from app import config

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    options = completion_options("gpt-6-luna", temperature=0.1, max_tokens=700)
    assert options["reasoning_effort"] == "high"


@pytest.fixture
def medium_effort(monkeypatch):
    # Isolate request assertions from local .env and deployment credentials.
    monkeypatch.setattr(openai_params, "_configured_reasoning_effort", lambda: "medium")


def assert_reasoning_options(kwargs, budget):
    assert kwargs["model"] == "gpt-6-luna"
    assert "temperature" not in kwargs and "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == budget + REASONING_TOKEN_HEADROOM
    assert kwargs["reasoning_effort"] == "medium"


def test_generation_sends_reasoning_options_to_the_api(medium_effort):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="답"))], usage=None,
    )
    answer = _complete(client, "gpt-6-luna", [{"role": "user", "content": "q"}],
                       temperature=0.1, max_tokens=700, timeout=5)
    assert answer == "답"
    kwargs = client.chat.completions.create.call_args.kwargs
    assert_reasoning_options(kwargs, 700)
    assert kwargs["timeout"] == 5


def test_streaming_sends_reasoning_options_to_the_api(medium_effort):
    client = MagicMock()
    client.chat.completions.create.return_value = iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="답"))]),
    ])
    assert list(_stream_complete(client, "gpt-6-luna", [{"role": "user", "content": "q"}],
                                 temperature=0.1, max_tokens=700, timeout=5)) == ["답"]
    kwargs = client.chat.completions.create.call_args.kwargs
    assert_reasoning_options(kwargs, 700)
    assert kwargs["stream"] is True


def test_query_planning_sends_reasoning_options_and_keeps_json_mode(medium_effort):
    question = "도서관 운영시간 알려줘"
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=json.dumps({"queries": [question]}))),
    ])
    plan = plan_question(openai_client=client, model="gpt-6-luna", question=question, chat_history=[])
    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert_reasoning_options(kwargs, 450)
    assert kwargs["response_format"] == {"type": "json_object"}
    assert plan.route == "search" and plan.queries == [question]


def completion(text, reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=reason)], usage=None)


def generation_options(client):
    return dict(openai_client=client, model="gpt-6-luna", system_prompt="s", question="셔틀버스 이용 방법",
                rows=[{"content": "공학1관 앞에서 셔틀버스에 탑승합니다."}], max_chars_per_chunk=500, timeout=5)


@pytest.mark.parametrize("partial", [None, "탑승 장소는"])
def test_empty_or_partial_length_limited_answers_get_one_bounded_retry(partial, medium_effort):
    client = MagicMock()
    client.chat.completions.create.side_effect = [completion(partial, "length"), completion("공학1관 앞에서 탑승합니다[1].")]
    answer = generate_answer(**generation_options(client))
    assert answer == "공학1관 앞에서 탑승합니다[1]."
    calls = client.chat.completions.create.call_args_list
    assert len(calls) == 2
    assert calls[1].kwargs["max_completion_tokens"] == calls[0].kwargs["max_completion_tokens"] * 2


def test_second_length_limited_answer_does_not_trigger_unbounded_retries(medium_effort):
    client = MagicMock()
    client.chat.completions.create.side_effect = [completion(None, "length"), completion("잘린 답", "length")]
    with pytest.raises(CompletionLimitReached):
        generate_answer(**generation_options(client))
    assert client.chat.completions.create.call_count == 2


def test_output_limit_recovery_still_checks_facts_without_a_third_generation(medium_effort):
    client = MagicMock()
    client.chat.completions.create.side_effect = [completion(None, "length"), completion("운임은 130원입니다[1].")]
    assert generate_answer(**generation_options(client)) == "관련 정보를 찾을 수 없습니다."
    assert client.chat.completions.create.call_count == 2


def test_provider_empty_output_is_an_error_but_explicit_no_information_is_preserved(medium_effort):
    client = MagicMock()
    client.chat.completions.create.return_value = completion(None)
    with pytest.raises(RuntimeError, match="empty answer"):
        generate_answer(**generation_options(client))
    client.chat.completions.create.return_value = completion("관련 정보를 찾을 수 없습니다.")
    assert generate_answer(**generation_options(client)) == "관련 정보를 찾을 수 없습니다."


def test_stream_length_limit_replaces_the_partial_answer_after_one_retry(medium_effort):
    client = MagicMock()
    client.chat.completions.create.side_effect = [iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="탑승 장소는"), finish_reason=None)]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="length")]),
    ]), completion("공학1관 앞에서 탑승합니다[1].")]
    events = list(stream_answer(**generation_options(client)))
    assert [kind for kind, _ in events] == ["token", "revising", "replace", "final"]
    assert events[-1] == ("final", "공학1관 앞에서 탑승합니다[1].")
    assert client.chat.completions.create.call_count == 2


def test_empty_stream_does_not_become_a_no_information_answer(medium_effort):
    client = MagicMock()
    client.chat.completions.create.return_value = iter([])
    with pytest.raises(RuntimeError, match="empty answer stream"):
        list(stream_answer(**generation_options(client)))
