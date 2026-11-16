"""OpenAI chat helper with a hard call budget and bounded retries.

Every LLM call in the layout eval scripts goes through ``BudgetedLLM`` so a bug
cannot turn into an unbounded API bill: total requests (retries included) are
capped by ``max_calls`` and each logical call retries at most ``max_retries``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_RETRIES = 3


class BudgetExceeded(RuntimeError):
    pass


class FatalLLMError(RuntimeError):
    """Retrying cannot help (bad key, no permission, bad request): stop the run."""


class InvalidLLMOutput(ValueError):
    """A refused or incomplete JSON response must not become a data label."""


FATAL_STATUS_CODES = {400, 401, 403, 404}


def load_backend_env() -> None:
    """Load backend/.env without overriding variables already set."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / "backend" / ".env", override=False)


class BudgetedLLM:
    def __init__(self, *, model: str | None = None, max_calls: int, max_retries: int = MAX_RETRIES,
                 timeout: float = 30.0, client: Any = None):
        load_backend_env()
        if client is None:
            from openai import OpenAI

            # The SDK has its own retry loop; disable it so our cap is the only one.
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0)
        self.client = client
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        self.max_calls = max_calls
        self.max_retries = min(max_retries, MAX_RETRIES)
        self.timeout = timeout
        self.requests = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.0,
             max_tokens: int = 500, json_mode: bool = False) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            if self.requests >= self.max_calls:
                raise BudgetExceeded(f"LLM call budget exhausted ({self.max_calls} requests)")
            self.requests += 1
            try:
                kwargs: dict[str, Any] = {}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, timeout=self.timeout, **kwargs,
                )
                usage = getattr(response, "usage", None)
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
                choice = response.choices[0]
                if json_mode and (getattr(choice, "finish_reason", None) not in (None, "stop")
                                  or getattr(choice.message, "refusal", None)):
                    raise InvalidLLMOutput("JSON output was incomplete or refused")
                return (choice.message.content or "").strip()
            except (BudgetExceeded, InvalidLLMOutput):
                raise
            except Exception as exc:  # network/rate-limit/API errors
                last_error = exc
                self.failures += 1
                status = getattr(exc, "status_code", None)
                if status in FATAL_STATUS_CODES:
                    raise FatalLLMError(f"HTTP {status}: {type(exc).__name__}") from exc
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

    def chat_json(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        text = self.chat(messages, json_mode=True, **kwargs)
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("LLM did not return a JSON object")
        return value

    def usage(self) -> dict[str, Any]:
        return {"model": self.model, "requests": self.requests, "failed_requests": self.failures,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}
