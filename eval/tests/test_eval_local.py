"""The real-answer runner must keep external requests bounded."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from eval.tools.eval_local import BoundedClient, select_cases


def test_request_budget_includes_every_provider_call_and_usage():
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3))
    client = BoundedClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), 1)
    client.create(model="test", messages=[])
    with pytest.raises(RuntimeError, match="budget exhausted"):
        client.create(model="test", messages=[])
    assert len(calls) == 1
    assert client.usage() == {"requests": 1, "max_requests": 1, "prompt_tokens": 12, "completion_tokens": 3}


def test_parallel_questions_share_budget_but_keep_separate_traces():
    active = Barrier(3)

    def create(**kwargs):
        active.wait(timeout=5)
        return SimpleNamespace(model=kwargs["model"], choices=[])

    client = BoundedClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), 3)

    def run(case_id):
        client.case_id = case_id
        try:
            client.create(model=case_id)
        except RuntimeError as error:
            assert "budget exhausted" in str(error)
            return False
        return True

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(run, (f"case_{i}" for i in range(5)))) == 3
    assert client.usage()["requests"] == 3
    assert all(row["response_model"] == row["case_id"] for row in client.responses)


def test_fatal_provider_rejection_stops_further_calls_and_does_not_expose_the_error():
    class Rejected(Exception):
        status_code = 401
    def create(**kwargs):
        raise Rejected("private-provider-details")
    client = BoundedClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), 5)
    with pytest.raises(Rejected):
        client.create()
    with pytest.raises(RuntimeError, match="HTTP 401") as caught:
        client.create()
    assert "private-provider-details" not in str(caught.value)
    assert client.requests == 1


def test_completion_trace_distinguishes_empty_length_limited_answers_from_refusals():
    response = SimpleNamespace(model="model-version", choices=[SimpleNamespace(finish_reason="length")],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=1700,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=1700)))
    client = BoundedClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))), 1)
    client.case_id = "bus"
    client.create(model="configured-model")
    assert client.responses[0] == {"case_id": "bus", "requested_model": "configured-model",
        "response_model": "model-version", "kind": "answer", "finish_reason": "length",
        "completion_tokens": 1700, "reasoning_tokens": 1700}


def test_balanced_selection_and_explicit_ids_preserve_the_question_labels(monkeypatch):
    cases = [{"id": "a1", "category": "a", "required_facts": ["unchanged"]},
             {"id": "a2", "category": "a"}, {"id": "b1", "category": "b"}]
    monkeypatch.setattr("eval.tools.eval_local._read_jsonl", lambda path: cases)
    assert [c["id"] for c in select_cases(["cases"], per_category=1)] == ["a1", "b1"]
    assert select_cases(["cases"], case_ids=["a1"])[0] == cases[0]
    with pytest.raises(ValueError, match="Unknown case IDs"):
        select_cases(["cases"], case_ids=["missing"])
    with pytest.raises(ValueError, match="unique"):
        select_cases(["cases", "cases"])
