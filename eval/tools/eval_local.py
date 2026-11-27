"""Evaluate the real local /ask pipeline without a server or an auth session.

Uses the configured database and OpenAI model, never writes database data, and
captures ordinary evaluation evidence. HTTP/authentication and frontend behavior
are deliberately outside this runner's scope.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
import json
import logging
from pathlib import Path
from threading import Lock, local
import time
from types import SimpleNamespace

from .eval_ask import _read_jsonl, _result_row
from .provenance import code_state, file_sha256, timestamp


class BoundedClient:
    """Count all real planner/generation calls, including repair passes."""
    def __init__(self, client, maximum):
        self.client = client
        self.maximum = maximum
        self.requests = self.prompt_tokens = self.completion_tokens = 0
        self.fatal = None
        self._lock = Lock()
        self._case = local()
        self.responses = []
        self.chat = self.completions = self

    @property
    def case_id(self):
        return getattr(self._case, "id", None)

    @case_id.setter
    def case_id(self, value):
        self._case.id = value

    def create(self, **kwargs):
        with self._lock:
            if self.fatal or self.requests >= self.maximum:
                raise RuntimeError(self.fatal or "Evaluation API request budget exhausted")
            self.requests += 1
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as error:
            if getattr(error, "status_code", None) in {400, 401, 403, 404}:
                with self._lock:
                    self.fatal = f"Provider rejected the request: HTTP {error.status_code}"
            raise
        usage = getattr(response, "usage", None)
        choices = getattr(response, "choices", None) or []
        details = getattr(usage, "completion_tokens_details", None)
        with self._lock:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            self.responses.append({"case_id": self.case_id, "requested_model": kwargs.get("model"),
                "response_model": getattr(response, "model", None),
                "kind": "planning" if kwargs.get("response_format") else "answer",
                "finish_reason": getattr(choices[0], "finish_reason", None) if choices else None,
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "reasoning_tokens": getattr(details, "reasoning_tokens", None)})
        return response

    def usage(self):
        return {"requests": self.requests, "max_requests": self.maximum,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


def select_cases(paths, per_category=None, case_ids=()):
    cases = [row for path in paths for row in _read_jsonl(path)]
    if len({r["id"] for r in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    if case_ids:
        missing = set(case_ids) - {r["id"] for r in cases}
        if missing:
            raise ValueError("Unknown case IDs: " + ", ".join(sorted(missing)))
        cases = [r for r in cases if r["id"] in case_ids]
    if per_category is not None:
        if per_category < 1:
            raise ValueError("per-category must be positive")
        counts, selected = Counter(), []
        for row in cases:
            category = row.get("category", "")
            if counts[category] < per_category:
                selected.append(row)
                counts[category] += 1
        cases = selected
    if not cases:
        raise ValueError("Select at least one evaluation case")
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--per-category", type=int)
    parser.add_argument("--max-calls", type=int, default=90)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1,
                        help="Concurrent questions; local embedding and reranking share one inference lock")
    parser.add_argument("--model", help="Explicit model override for controlled comparisons; does not edit .env")
    parser.add_argument("--data-snapshot", required=True)
    args = parser.parse_args(argv)
    cases = select_cases(args.questions, args.per_category, args.case_id)
    manifest_path = args.output.with_suffix(".manifest.json")
    if args.output.exists() or manifest_path.exists():
        parser.error("Choose a new output; prior evaluation evidence is never overwritten")
    if args.max_calls < 1:
        parser.error("max-calls must be positive")

    import torch
    from fastapi import HTTPException
    from openai import OpenAI
    from starlette.responses import Response
    from backend.app.config import get_settings
    from backend.app.deps import AppState
    from backend.app.embeddings import Embedder
    from backend.app.generation import load_system_prompt
    from backend.app.reranking import CrossEncoderReranker
    from backend.app.routers.ask import ask
    from backend.app.schemas import AskRequest
    from backend.app.supabase_client import ThreadLocalSupabase

    torch.set_num_threads(4)
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    if args.model:
        settings = settings.model_copy(update={"openai_model": args.model})
    client = BoundedClient(OpenAI(api_key=settings.openai_api_key, max_retries=0,
                                 timeout=settings.openai_timeout_seconds), args.max_calls)
    embedder = Embedder(model_name=settings.embedding_model, device=settings.embedding_device,
                        expected_dimensions=settings.expected_dimensions,
                        **({"query_prefix": settings.embedding_query_prefix} if hasattr(settings, "embedding_query_prefix") else {}))
    reranker = (CrossEncoderReranker(model_name=settings.reranker_model, device=settings.reranker_device,
                                   quantize=settings.reranker_quantize) if settings.reranker_enabled else None)
    inference_lock = Lock()

    def encode(query):
        with inference_lock:
            return embedder.encode_query(query)

    def score(query, rows):
        with inference_lock:
            return reranker.score(query, rows)

    state = AppState(settings=settings,
        embedder=SimpleNamespace(encode_query=encode),
        reranker=SimpleNamespace(score=score) if reranker else None,
        supabase=ThreadLocalSupabase(settings.supabase_url, settings.supabase_service_role_key),
        openai=client, system_prompt=load_system_prompt())
    manifest = {"schema_version": 1, "execution_mode": "direct_local_ask", "started_at": timestamp(),
                "questions": [{"path": str(p), "sha256": file_sha256(p)} for p in args.questions],
                "selected_case_ids": [c["id"] for c in cases], "data_snapshot": args.data_snapshot,
                "configuration_override": {"openai_model": args.model} if args.model else {},
                "data_snapshot_scope": "operator ingestion receipt label; no atomic DB snapshot created",
                "local_code": code_state(), "http_auth_tested": False, "database_writes": 0,
                "workers": args.workers}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint():
        manifest["usage"] = client.usage()
        manifest["completions"] = client.responses
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint()
    ok = errors = 0

    def run_case(case):
        started = time.perf_counter()
        client.case_id = case["id"]
        try:
            response = ask(AskRequest(question=case["question"], include_evidence=True), state=state,
                           user_id="local-quality-evaluation")
            if isinstance(response, Response):
                status, payload = response.status_code, json.loads(response.body)
                if status >= 400:
                    payload = {"error": payload}
            else:
                status, payload = 200, response.model_dump(mode="json", exclude_none=True)
        except HTTPException as error:
            status, payload = error.status_code, {"error": {"detail": error.detail}}
        except Exception as error:
            status, payload = 500, {"error": {"type": type(error).__name__}}
        return _result_row(question_row=case, status_code=status, response=payload,
                           elapsed_seconds=time.perf_counter()-started)

    with args.output.open("x", encoding="utf-8", newline="\n") as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for batch in batched(cases, args.workers):
            if client.fatal or client.requests >= client.maximum:
                break
            for row in list(pool.map(run_case, batch)):
                stream.write(json.dumps(row, ensure_ascii=False)+"\n")
                stream.flush()
                if row["error"]:
                    errors += 1
                else:
                    ok += 1
                print(json.dumps({"case": row["id"], "status": row["status_code"], "sources": len(row["sources"]),
                                  "seconds": row["elapsed_seconds"], "api_calls": client.requests,
                                  "answer": row["answer"][:240]}, ensure_ascii=False), flush=True)
            manifest.update(completed_case_count=ok+errors, ok_count=ok, error_count=errors)
            checkpoint()
    manifest.update(finished_at=timestamp(), results_sha256=file_sha256(args.output),
                    status="complete" if ok+errors==len(cases) and not errors else "incomplete")
    checkpoint()
    print(json.dumps({"status": manifest["status"], "ok": ok, "errors": errors, "usage": client.usage()}, ensure_ascii=False))
    return 0 if manifest["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
