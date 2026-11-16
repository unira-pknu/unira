"""Frozen feedback experiment: prepare, capture, submit, status, compare.

All commands are explicit. Preparation is offline; no command deploys a model.
See docs/evaluation/FEEDBACK_TRAINING_AUTOMATION.md for costs and recovery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import uuid4

from backend.app.feedback_training import question_key
from backend.app.generation import build_user_message
from .automate_feedback_review import EXTRA_HOLDOUT, audit_and_build, feedback_fingerprint
from .build_feedback_training import DEFAULT_HOLDOUTS, read_candidates, source_key
from .eval_ask import _read_jsonl
from .llm_budget import load_backend_env
from .prepare_feedback_training import NoRedirect, feedback_endpoint

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "backend/prompts/system.txt"
TRAINING_DEADLINE = "2027-01-06"
KST = timezone(timedelta(hours=9))
TERMINAL = {"succeeded", "failed", "cancelled"}
DECISION_LABELS = {
    "incomplete_evaluation": "일부 케이스의 생성·채점 미완료 또는 비교 제외",
    "insufficient_evaluation_cases": "최소 평가 문항 수 미달",
    "unverified_quality": "근거·거절·학과·시점 중 확인되지 않은 판정 존재",
    "strict_accuracy_improvement_below_threshold": "엄격 정답률 개선 기준 미달",
    "fact_coverage_regression": "필수 사실 충족도 하락",
    "forbidden_regression": "금지 주장 위반 증가",
    "unsupported_regression": "근거 없는 주장 또는 출처 모순 증가",
    "over_refusal_regression": "과도한 거절 증가",
    "department_regression": "학과 범위 오류 증가",
    "temporal_regression": "시점 오류 증가",
    "previously_correct_case_regressed": "기존 정답 케이스의 퇴보",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_rows(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


@contextmanager
def lock(run: Path):
    """One writer per experiment; a crash leaves an explicit recovery marker."""
    marker = run / ".experiment.lock"
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
    except FileExistsError:
        raise ValueError("Experiment is locked; see recovery instructions before removing .experiment.lock") from None
    try:
        yield
    finally:
        marker.unlink()


def load_run(run: Path) -> tuple[dict, dict]:
    manifest = read_json(run / "manifest.json")
    state = read_json(run / "state.json")
    if state.get("manifest_sha256") != digest(manifest):
        raise ValueError("Manifest changed; create a new experiment")
    for name, expected in manifest["artifacts"].items():
        if file_hash(run / name) != expected:
            raise ValueError(f"Frozen artifact changed: {name}")
    for name, expected in state.get("frozen_artifacts", {}).items():
        if file_hash(run / name) != expected:
            raise ValueError(f"Captured artifact changed: {name}")
    for name, expected in state.get("progress_artifacts", {}).items():
        if file_hash(run / name) != expected:
            raise ValueError(f"Progress artifact changed: {name}")
    for name, expected in manifest["code_sha256"].items():
        if file_hash(ROOT / name) != expected:
            raise ValueError(f"Experiment code changed: {name}; prepare a new run")
    return manifest, state


def save_state(run: Path, state: dict) -> None:
    state["updated_at"] = now()
    write_json(run / "state.json", state)


def build_training_messages(ready: list[dict], evidence: list[dict], system: str) -> tuple[list[dict], list[str]]:
    """Only reviewed answers and verified saved bodies enter the training upload."""
    snapshots = {row["feedback_id"]: row for row in evidence}
    result, problems = [], []
    for row in ready:
        if row["review"]["split"] != "train":
            continue
        parent = row.get("parent_feedback_id") or row["id"]
        snapshot = snapshots.get(parent)
        if not snapshot:
            problems.append(f"missing_training_evidence:{row['id']}")
            continue
        expected_key = row.get("parent_question_key") or row["original_question_key"]
        if snapshot.get("original_question_key") != expected_key:
            raise ValueError("Training evidence question anchor changed")
        sources = snapshot.get("sources") or []
        urls = {source_key(source["uri"]) for source in sources}
        if not {source_key(url) for url in row["review"]["verified_sources"]}.issubset(urls):
            problems.append(f"verified_source_body_missing:{row['id']}")
            continue
        for source in sources:
            if hashlib.sha256(source["content"].encode()).hexdigest() != source["content_sha256"]:
                raise ValueError("Training evidence body hash mismatch")
        # Stage-one evidence IDs and source numbering are kept in their original order.
        block = "\n\n".join(f"[{i}] {source.get('title', '')}\nURL: {source['uri']}\n{source['content']}"
                              for i, source in enumerate(sources, 1))
        answer = row["review"]["corrected_answer"].strip()
        if any(not 1 <= int(index) <= len(sources) for index in re.findall(r"\[(\d+)\]", answer)):
            problems.append(f"invalid_training_citation:{row['id']}")
            continue
        captured = datetime.fromisoformat(snapshot["captured_at"])
        if captured.tzinfo is None:
            raise ValueError("Training evidence timestamp must have a timezone")
        user = build_user_message(row["question"].strip(), [], 1200, source_block=block, as_of=captured.astimezone(KST).date())
        result.append({"messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user},
                                    {"role": "assistant", "content": answer}]})
    return result, problems


def prepare(args: argparse.Namespace) -> None:
    if args.run_dir.exists():
        raise ValueError("Run directory already exists; choose a new directory")
    if not 1 <= args.epochs <= 20 or not 1 <= args.max_tokens <= 4096:
        raise ValueError("epochs must be 1..20; max-tokens must be 1..4096")
    if (not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.temperature)
            or not 0 <= args.temperature <= 2 or not 0 <= args.min_improvement <= 1 or args.min_cases < 1):
        raise ValueError("Invalid timeout, temperature or acceptance threshold")
    for model in (args.base_model, args.baseline_model, args.judge_model):
        if not model.strip():
            raise ValueError("Explicit, non-empty model IDs are required")
    summary = read_json(args.dataset_dir / "summary.json")
    if summary.get("schema_version") != 2 or not summary.get("dataset_built"):
        raise ValueError("Expected a built stage-two dataset")
    if summary.get("freshness_checked") is not True:
        raise ValueError("Re-run stage two with --base-url before preparing training")
    ready = read_candidates(args.dataset_dir / "ready.jsonl")
    current = read_candidates(args.dataset_dir / "current_candidates.jsonl")
    if digest(current) != summary["current_candidates_sha256"]:
        raise ValueError("Stage-two current feedback snapshot hash mismatch")
    current_by_id = {row["id"]: row for row in current}
    for row in ready:
        parent = current_by_id.get(row.get("parent_feedback_id") or row["id"])
        fingerprint = row.get("parent_feedback_fingerprint") if row.get("synthetic") else feedback_fingerprint(row)
        if parent is None or fingerprint != feedback_fingerprint(parent):
            raise ValueError("Feedback or synthetic parent differs from the stage-two snapshot")
    case_paths = args.cases or [ROOT / "eval/cases/regression.jsonl", EXTRA_HOLDOUT]
    cases = [case for path in case_paths for case in _read_jsonl(path)]
    # Include stage-two held-out feedback without moving it into training.
    cases += _read_jsonl(args.dataset_dir / "evaluation.jsonl")
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Evaluation cases must be non-empty with globally unique IDs")
    if len({question_key(case["question"]) for case in cases}) != len(cases):
        raise ValueError("Duplicate evaluation questions; choose non-overlapping case files")
    if any(type(case.get("answerable")) is not bool or case.get("label_status") != "ready" for case in cases):
        raise ValueError("All evaluation cases need ready human labels and answerable")
    holdout_paths = list(dict.fromkeys([*DEFAULT_HOLDOUTS, EXTRA_HOLDOUT, *args.holdout, *case_paths]))
    protected = {path.name: file_hash(path) for path in holdout_paths}
    for recorded in summary["holdouts"]:
        if protected.get(recorded["file"]) != recorded["sha256"]:
            raise ValueError("Stage-two holdout missing or changed; pass the original custom --holdout and re-run stage two")
    holdouts = [case for path in holdout_paths for case in _read_jsonl(path)]
    evidence = read_candidates(args.dataset_dir / "evidence.jsonl")
    # Do not treat evaluation-split feedback as overlap with itself in this re-audit.
    checked = audit_and_build(ready, {}, evidence, holdouts)
    if len(checked["ready"]) != len(ready):
        raise ValueError("Stage-two approval/evidence/split audit failed; re-run stage two")
    if checked["train"] != read_candidates(args.dataset_dir / "train.jsonl") or checked["evaluation"] != _read_jsonl(args.dataset_dir / "evaluation.jsonl"):
        raise ValueError("Stage-two exported datasets differ from approved rows")
    # Also protect newly selected comparison cases from the training families/sources.
    training = [row for row in ready if row["review"]["split"] == "train"]
    if len(audit_and_build(training, {}, evidence, cases)["ready"]) != len(training):
        raise ValueError("Training overlaps selected comparison questions or sources")
    system = PROMPT.read_text(encoding="utf-8")
    upload_rows, blockers = build_training_messages(ready, evidence, system)
    if len(upload_rows) < 10:
        blockers.append(f"minimum_training_examples:10;available:{len(upload_rows)}")
    max_calls = args.max_calls if args.max_calls is not None else 4 * len(cases) + 2 * sum(len(c.get("forbidden_claims") or []) for c in cases)
    if max_calls < 4 * len(cases):
        raise ValueError("max-calls must cover both generations and both judges per case")
    args.run_dir.mkdir(parents=True)
    write_rows(args.run_dir / "train.jsonl", upload_rows)
    write_rows(args.run_dir / "cases.jsonl", cases)
    (args.run_dir / "system.txt").write_text(system, encoding="utf-8")
    write_rows(args.run_dir / "approved.jsonl", ready)
    names = ("train.jsonl", "cases.jsonl", "system.txt", "approved.jsonl")
    code_paths = ("eval/tools/automate_feedback_training.py", "eval/tools/grade_eval_results.py",
                  "eval/tools/quality.py", "backend/app/generation.py", "backend/app/freshness.py")
    manifest = {"schema_version": 1, "run_id": uuid4().hex, "created_at": now(),
                "base_model": args.base_model, "baseline_model": args.baseline_model, "judge_model": args.judge_model,
                "epochs": args.epochs, "seed": 42, "temperature": args.temperature, "max_tokens": args.max_tokens,
                "timeout": args.timeout, "max_calls": max_calls, "min_improvement": args.min_improvement,
                "min_cases": args.min_cases, "train_count": len(upload_rows), "case_count": len(cases),
                "training_blockers": blockers, "stage2_created_at": summary["created_at"],
                "stage2_freshness_checked": True, "holdouts": protected,
                "artifacts": {name: file_hash(args.run_dir / name) for name in names},
                "code_sha256": {name: file_hash(ROOT / name) for name in code_paths},
                "source_sha256": {name: file_hash(args.dataset_dir / name) for name in
                                  ("summary.json", "ready.jsonl", "train.jsonl", "evaluation.jsonl", "evidence.jsonl", "current_candidates.jsonl")}}
    write_json(args.run_dir / "manifest.json", manifest)
    save_state(args.run_dir, {"manifest_sha256": digest(manifest), "phase": "prepared", "requests": 0, "capture_requests": 0})
    text = ["# 학습·비교 평가 준비", "", f"학습 {len(upload_rows)}건 / 고정 평가 {len(cases)}건",
            f"학습 제출: {'보류' if blockers else '데이터 조건 충족 (계정·모델 이용 가능 여부는 별도 확인)'}",
            *[f"- {item}" for item in blockers], "", "평가 데이터는 학습/validation 파일로 업로드하지 않습니다.",
            "검색 캡처 → 학습 제출/상태 확인 → 동일 근거 비교 → 보고서 검토 순서입니다.",
            "OpenAI 신규 파인튜닝 사용은 제한됩니다. 기존 이용 가능 계정만 submit을 실행하세요."]
    (args.run_dir / "preflight.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    print(f"Prepared: train={len(upload_rows)} cases={len(cases)} training_blocked={bool(blockers)}; {args.run_dir}")


def freeze_case(case: dict, result: dict, system: str) -> dict:
    """Fail closed on routes that cannot be replayed as isolated model generation."""
    if result.get("error") or result.get("question") != case["question"] or result.get("id") != case["id"]:
        raise ValueError(f"Invalid captured result: {case['id']}")
    snapshot = deepcopy(result.get("evaluation_evidence"))
    if not isinstance(snapshot, dict) or snapshot.get("body_scope") != "retrieved_chunks":
        raise ValueError(f"Missing full evidence snapshot: {case['id']}")
    if snapshot.get("system_prompt_sha256") != hashlib.sha256(system.encode()).hexdigest():
        raise ValueError(f"Backend/local system prompt mismatch: {case['id']}")
    if (snapshot.get("retrieval") or {}).get("status") not in (None, "complete"):
        return {"id": case["id"], "eligible": False, "exclusion": "retrieval_degraded"}
    if snapshot.get("answer_mode") != "search" or not isinstance(snapshot.get("prompt_source_block"), str):
        return {"id": case["id"], "eligible": False, "exclusion": "non_search_route:" + str(snapshot.get("answer_mode"))}
    sources = snapshot.get("sources") or []
    if [source.get("index") for source in sources] != list(range(1, len(sources) + 1)):
        raise ValueError("Captured source numbering is invalid")
    if case["answerable"] and not sources:
        return {"id": case["id"], "eligible": False, "exclusion": "answerable_case_without_evidence"}
    for source in sources:
        if hashlib.sha256(source["content"].encode()).hexdigest() != source["content_sha256"]:
            raise ValueError("Captured evidence hash mismatch")
        shown = source.get("shown_content")
        if not isinstance(shown, str) or not shown.strip():
            raise ValueError("Missing source text actually shown to the model")
        # Judges see only the body the models saw, rather than hidden full chunks.
        source.update(content=shown, content_sha256=hashlib.sha256(shown.encode()).hexdigest())
        source.pop("context_chunks", None)
        source.pop("cited_in_answer", None)
    captured = datetime.fromisoformat(snapshot["captured_at"])
    if captured.tzinfo is None:
        raise ValueError("Captured timestamp needs a timezone")
    plan = snapshot.get("plan") or {}
    question = plan.get("resolved_question") or case["question"]
    rows = [{"metadata": source.get("metadata") or {}} for source in sources]
    user = build_user_message(question, rows, 1200, source_block=snapshot["prompt_source_block"], as_of=captured.astimezone(KST).date())
    snapshot["generation"] = {"status": "frozen_for_comparison"}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return {"id": case["id"], "eligible": True, "messages": messages, "messages_sha256": digest(messages), "evaluation_evidence": snapshot}


def freeze(run: Path, manifest: dict, state: dict, results: list[dict], data_snapshot: str) -> None:
    if state.get("frozen_artifacts"):
        raise ValueError("Context already frozen; use a new experiment to replace it")
    if not data_snapshot.strip():
        raise ValueError("Supply an operator-assigned database/ingestion snapshot label")
    cases = read_candidates(run / "cases.jsonl")
    if len({row["id"] for row in results}) != len(results) or {row["id"] for row in results} != {row["id"] for row in cases}:
        raise ValueError("Captured results must cover every frozen case exactly once")
    by_id = {row["id"]: row for row in results}
    system = (run / "system.txt").read_text(encoding="utf-8")
    frozen = [freeze_case(case, by_id[case["id"]], system) for case in cases]
    write_rows(run / "contexts.jsonl", frozen)
    state.update(frozen_artifacts={"contexts.jsonl": file_hash(run / "contexts.jsonl")}, data_snapshot=data_snapshot,
                 excluded_cases=[{"id": row["id"], "reason": row["exclusion"]} for row in frozen if not row["eligible"]])
    save_state(run, state)


def capture(run: Path, manifest: dict, state: dict, base_url: str, data_snapshot: str) -> None:
    if state.get("frozen_artifacts"):
        raise ValueError("Context already frozen")
    feedback_endpoint(base_url)  # Validate transport and reject credentials/query/fragment.
    endpoint = base_url.rstrip("/") + "/ask"
    token = os.getenv("ASK_API_TOKEN") or os.getenv("FEEDBACK_API_TOKEN")
    if not token:
        raise ValueError("Set ASK_API_TOKEN or FEEDBACK_API_TOKEN in backend/.env")
    cases = read_candidates(run / "cases.jsonl")
    partial = run / "capture.jsonl"
    results = read_candidates(partial) if partial.exists() else []
    done = {row["id"] for row in results}
    opener = build_opener(NoRedirect())
    for case in cases:
        if case["id"] in done:
            continue
        # No retries. An interrupted/uncertain call consumes its slot; use freeze with
        # a separately collected result or a new run instead of silently re-spending.
        attempted = state.setdefault("capture_attempted_ids", [])
        if case["id"] in attempted:
            raise ValueError(f"Capture outcome uncertain for {case['id']}; see recovery instructions")
        attempted.append(case["id"])
        state["capture_requests"] += 1
        save_state(run, state)
        request = Request(endpoint, data=json.dumps({"question": case["question"], "stream": False,
                                                   "include_evidence": True}).encode(),
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=manifest["timeout"]) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise ValueError(f"Capture API HTTP {exc.code}; no token or response body logged") from None
        except (URLError, TimeoutError):
            raise ValueError("Capture API connection failed or timed out") from None
        row = {**case, "evaluation_evidence": payload.get("evaluation_evidence"), "error": payload.get("error")}
        results.append(row)
        write_rows(partial, results)
        state.setdefault("progress_artifacts", {})["capture.jsonl"] = file_hash(partial)
        save_state(run, state)
        freeze_case(case, row, (run / "system.txt").read_text(encoding="utf-8"))
    freeze(run, manifest, state, results, data_snapshot)


def api_client(timeout: float):
    from openai import OpenAI
    # This workflow targets existing eligible OpenAI fine-tuning accounts only.
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url="https://api.openai.com/v1", max_retries=0, timeout=timeout)


def submit(run: Path, manifest: dict, state: dict, *, eligible: bool, client: Any = None) -> None:
    if state.get("job_id"):
        print(f"Existing job: {state['job_id']} (no new submission)")
        return
    if manifest["training_blockers"]:
        raise ValueError("Training blocked: " + "; ".join(manifest["training_blockers"]))
    if datetime.now(timezone.utc).date().isoformat() >= TRAINING_DEADLINE:
        raise ValueError("Documented OpenAI fine-tuning creation window has ended; verify current provider availability")
    if not eligible:
        raise ValueError("Only an existing eligible OpenAI fine-tuning account may use --eligible-account")
    if state.get("phase") in {"upload_intent", "submit_intent"}:
        raise ValueError("Previous API outcome uncertain; attach the existing file/job rather than resubmitting")
    client = client or api_client(manifest["timeout"])
    if not state.get("training_file_id"):
        state["phase"] = "upload_intent"
        save_state(run, state)
        with (run / "train.jsonl").open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="fine-tune")
        state.update(training_file_id=uploaded.id, phase="uploaded")
        save_state(run, state)
    state["phase"] = "submit_intent"
    save_state(run, state)
    job = client.fine_tuning.jobs.create(
        model=manifest["base_model"], training_file=state["training_file_id"], seed=manifest["seed"],
        suffix="feedback-" + manifest["run_id"][:12],
        method={"type": "supervised", "supervised": {"hyperparameters": {"n_epochs": manifest["epochs"]}}},
        extra_body={"metadata": {"feedback_experiment": manifest["run_id"]}},
    )
    state.update(job_id=job.id, phase="submitted", job_status=job.status)
    save_state(run, state)
    print(f"Submitted: {job.id}")


def attach(run: Path, manifest: dict, state: dict, *, file_id: str | None, job_id: str | None, client: Any = None) -> None:
    client = client or api_client(manifest["timeout"])
    if file_id:
        if state.get("job_id"):
            raise ValueError("Cannot replace the file of a submitted job")
        content = client.files.content(file_id).read()
        if hashlib.sha256(content).hexdigest() != manifest["artifacts"]["train.jsonl"]:
            raise ValueError("Uploaded training file content differs from this experiment")
        state.update(training_file_id=file_id, phase="uploaded")
    if job_id:
        if state.get("job_id") not in (None, job_id):
            raise ValueError("A different job is already attached")
        job = client.fine_tuning.jobs.retrieve(job_id)
        if job.model != manifest["base_model"] or job.training_file != state.get("training_file_id"):
            raise ValueError("Job model/training file does not match this experiment")
        state.update(job_id=job.id, phase="submitted", job_status=job.status)
    save_state(run, state)


def status(run: Path, manifest: dict, state: dict, *, polls: int, interval: float, client: Any = None) -> None:
    if not state.get("job_id"):
        raise ValueError("No submitted job; submit or attach first")
    if not 1 <= polls <= 100 or not math.isfinite(interval) or not 0 <= interval <= 60 or (polls > 1 and interval < 1):
        raise ValueError("polls=1..100; polling intervals=1..60 seconds")
    client = client or api_client(manifest["timeout"])
    for index in range(polls):
        job = client.fine_tuning.jobs.retrieve(state["job_id"])
        state.update(job_status=job.status, trained_model=job.fine_tuned_model, trained_tokens=job.trained_tokens)
        save_state(run, state)
        print(f"{job.id}: {job.status}; model={job.fine_tuned_model or '(pending)'}", flush=True)
        if job.status in TERMINAL or index == polls - 1:
            break
        time.sleep(interval)


class ModelRequestError(ValueError):
    """Never expose provider error bodies through the existing judge logger."""
    def __init__(self, kind: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(f"Model request failed ({kind}, HTTP {status_code})")


class MeteredClient:
    """SDK-shaped wrapper: one global, durable budget includes judge confirmations."""
    def __init__(self, run: Path, manifest: dict, state: dict, client: Any):
        self.run, self.manifest, self.state, self.client = run, manifest, state, client
        self.chat = SimpleNamespace(completions=self)
        self.failures = 0
        self.last = {}

    def create(self, **kwargs):
        if self.state["requests"] >= self.manifest["max_calls"]:
            self.failures += 1
            raise ValueError("Experiment request budget exhausted")
        self.state["requests"] += 1
        save_state(self.run, self.state)
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(**kwargs)
            usage = getattr(response, "usage", None)
            record = {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                      "completion_tokens": getattr(usage, "completion_tokens", 0) or 0}
            totals = self.state.setdefault("usage_by_model", {}).setdefault(kwargs["model"], {"prompt_tokens": 0, "completion_tokens": 0})
            for key, value in record.items():
                totals[key] += value
            self.last = {**record, "elapsed_seconds": round(time.monotonic() - started, 4),
                         "response_model": getattr(response, "model", kwargs["model"])}
            save_state(self.run, self.state)
            choice = response.choices[0]
            if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None) or not (choice.message.content or "").strip():
                raise ValueError("Incomplete, empty or refused model response")
            return response
        except Exception as exc:
            self.failures += 1
            raise ModelRequestError(type(exc).__name__, getattr(exc, "status_code", None)) from None


def decision_report(baseline: list[dict], candidate: list[dict], manifest: dict) -> dict:
    from .grade_eval_results import summarize
    a, b = summarize(baseline), summarize(candidate)
    reasons = []
    if any(summary["graded_case_count"] != manifest["case_count"] or summary["api_error_case_ids"]
           or summary["judge_error_case_ids"] or summary["unverified_forbidden_case_ids"] for summary in (a, b)):
        reasons.append("incomplete_evaluation")
    if manifest["case_count"] < manifest["min_cases"]:
        reasons.append("insufficient_evaluation_cases")
    for summary in (a, b):
        quality = summary["quality"]
        if (quality["claims"]["unverifiable"] or quality["claimless_nonrefusal_case_ids"]
                or any(quality[key]["unverifiable"] for key in ("department", "temporal"))
                or quality["over_refusal"]["evaluated"] != quality["over_refusal"]["answerable_cases"]):
            reasons.append("unverified_quality")
    if any((row.get("judged") or {}).get("refusal", {}).get("verdict") == "unverifiable" for row in [*baseline, *candidate]):
        reasons.append("unverified_quality")
    ar, br = a["answer_accuracy"]["strict_rate_all_labeled"], b["answer_accuracy"]["strict_rate_all_labeled"]
    delta = round(br - ar, 4) if ar is not None and br is not None else None
    if delta is None or delta < manifest["min_improvement"]:
        reasons.append("strict_accuracy_improvement_below_threshold")
    af, bf = a["fact_coverage_score"], b["fact_coverage_score"]
    if af is not None and (bf is None or bf < af):
        reasons.append("fact_coverage_regression")
    for name, getter in {
        "forbidden": lambda s: s["forbidden_claims_violated"],
        "unsupported": lambda s: s["quality"]["claims"]["unsupported"] + s["quality"]["claims"]["contradicted"],
        "over_refusal": lambda s: s["quality"]["over_refusal"]["count"],
        "department": lambda s: s["quality"]["department"]["errors"],
        "temporal": lambda s: s["quality"]["temporal"]["errors"],
    }.items():
        if getter(b) > getter(a):
            reasons.append(name + "_regression")
    ac = set(a["answer_accuracy"]["correct_case_ids"])
    bc = set(b["answer_accuracy"]["correct_case_ids"])
    if ac - bc:
        reasons.append("previously_correct_case_regressed")
    return {"recommendation": "review_candidate" if not reasons else "hold", "reasons": sorted(set(reasons)),
            "strict_accuracy_delta": delta, "improved_case_ids": sorted(bc - ac), "regressed_case_ids": sorted(ac - bc),
            "baseline": a, "candidate": b, "automatic_deployment": False,
            "scope": "single-turn generation with frozen retrieval; excludes routing, retrieval and production repair passes"}


def compare(run: Path, manifest: dict, state: dict, *, candidate_model: str | None, client: Any = None) -> dict:
    from .grade_eval_results import grade_case
    candidate_model = candidate_model or (state.get("trained_model") if state.get("job_status") == "succeeded" else None)
    if (not candidate_model or not candidate_model.startswith("ft:")
            or candidate_model == manifest["baseline_model"] or candidate_model == manifest["judge_model"]):
        raise ValueError("Need an OpenAI ft: candidate distinct from baseline/judge, or a succeeded training job")
    if not state.get("frozen_artifacts"):
        raise ValueError("Capture or freeze comparison evidence first")
    if state.get("candidate_model") not in (None, candidate_model):
        raise ValueError("Candidate model cannot change within an experiment")
    state["candidate_model"] = candidate_model
    save_state(run, state)
    cases = read_candidates(run / "cases.jsonl")
    contexts = {row["id"]: row for row in read_candidates(run / "contexts.jsonl")}
    path = run / "comparison.jsonl"
    rows = read_candidates(path) if path.exists() else []
    completed = {(row["id"], row["variant"]) for row in rows}
    if len(completed) != len(rows):
        raise ValueError("Duplicate comparison records")
    def checkpoint(row: dict) -> None:
        rows.append(row)
        write_rows(path, rows)
        state.setdefault("progress_artifacts", {})["comparison.jsonl"] = file_hash(path)
        save_state(run, state)

    metered = MeteredClient(run, manifest, state, client or api_client(manifest["timeout"]))
    halted = False
    for index, case in enumerate(cases):
        variants = [("baseline", manifest["baseline_model"]), ("candidate", candidate_model)]
        if index % 2:
            variants.reverse()
        for variant, model in variants:
            if (case["id"], variant) in completed:
                continue
            context = contexts[case["id"]]
            if not context["eligible"]:
                checkpoint({**deepcopy(case), "variant": variant, "model": model,
                            "skipped": context["exclusion"], "error": None})
                continue
            if digest(context["messages"]) != context["messages_sha256"]:
                raise ValueError("Comparison prompt changed")
            # A crash during a paid generation/judgment is not retried automatically.
            key = f"{case['id']}:{variant}"
            if key in state.setdefault("comparison_attempted", []):
                raise ValueError(f"Uncertain comparison outcome for {key}; create a new experiment")
            state["comparison_attempted"].append(key)
            save_state(run, state)
            row = {**deepcopy(case), "variant": variant, "model": model, "messages_sha256": context["messages_sha256"],
                   "evaluation_evidence": deepcopy(context["evaluation_evidence"]), "error": None}
            failures_before = metered.failures
            try:
                response = metered.create(model=model, messages=deepcopy(context["messages"]), temperature=manifest["temperature"],
                                          max_tokens=manifest["max_tokens"], timeout=manifest["timeout"])
                row.update(answer=response.choices[0].message.content.strip(), generation=metered.last)
                cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", row["answer"])})
                row["evaluation_evidence"]["generation"] = {"status": "compared", "cited_source_indices": cited,
                                                           "answer_sha256": hashlib.sha256(row["answer"].encode()).hexdigest()}
                # Model identity is not included in build_user_prompt; judge is fixed.
                row["judged"] = grade_case(metered, manifest["judge_model"], row, manifest["timeout"], schema_retries=0)
                if metered.failures != failures_before:
                    row["judged"]["judge_error"] = "confirmation_request_failed"
            except Exception as exc:
                row["error"] = {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
                halted = getattr(exc, "status_code", None) in {400, 401, 403, 404} or state["requests"] >= manifest["max_calls"]
            checkpoint(row)
            if halted:
                break
        if halted:
            break
    report = decision_report([r for r in rows if r["variant"] == "baseline"], [r for r in rows if r["variant"] == "candidate"], manifest)
    report.update(run_id=manifest["run_id"], created_at=now(), candidate_model=candidate_model,
                  baseline_model=manifest["baseline_model"], judge_model=manifest["judge_model"],
                  requests=state["requests"], capture_requests=state["capture_requests"], usage_by_model=state.get("usage_by_model", {}),
                  contexts_sha256=state["frozen_artifacts"]["contexts.jsonl"],
                  comparison_sha256=file_hash(path), data_snapshot=state["data_snapshot"])
    report["excluded_cases"] = state.get("excluded_cases", [])
    report["reason_labels"] = [DECISION_LABELS.get(reason, reason) for reason in report["reasons"]]
    report["incomplete_case_ids"] = [case["id"] for case in cases if any(
        not any(row["id"] == case["id"] and row["variant"] == variant and not row.get("error") and not row.get("skipped")
                and row.get("judged") and not row["judged"].get("judge_error") for row in rows)
        for variant in ("baseline", "candidate"))]
    write_json(run / "report.json", report)
    lines = ["# 학습 모델 비교 평가", "", f"판정: {report['recommendation']}",
             f"기존 모델: {manifest['baseline_model']} / 후보 모델: {candidate_model}",
             f"엄격 정답률 변화: {report['strict_accuracy_delta']}", "", "| 지표 | 기존 | 후보 |", "| --- | ---: | ---: |"]
    for title, getter in (("엄격 정답률", lambda s: s["answer_accuracy"]["strict_rate_all_labeled"]),
                          ("필수 사실 충족도", lambda s: s["fact_coverage_score"]),
                          ("금지 주장 위반", lambda s: s["forbidden_claims_violated"]),
                          ("과도한 거절", lambda s: s["quality"]["over_refusal"]["count"])):
        lines.append(f"| {title} | {getter(report['baseline'])} | {getter(report['candidate'])} |")
    lines += ["", "판정 사유: " + (", ".join(report["reason_labels"]) or "설정한 검토 기준 충족"),
              "개선 케이스: " + (", ".join(report["improved_case_ids"]) or "없음"),
              "퇴보 케이스: " + (", ".join(report["regressed_case_ids"]) or "없음"),
              "미완료·제외 케이스: " + (", ".join(report["incomplete_case_ids"]) or "없음"),
              "", "동일 검색 자료를 이용한 1회 생성 비교입니다. 운영 경로/검색/수치 재작성까지 평가한 결과는 아닙니다.",
              "비용은 토큰 사용량으로 기록합니다. 실제 과금액은 제공자 청구 내역으로 확인하세요.",
              "자동 배포하지 않습니다. 최종 적용 전 별도 고정 평가와 서비스 전체 검증이 필요합니다."]
    (run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Comparison: {report['recommendation']}; {run / 'report.md'}")
    return report


def advance(args: argparse.Namespace, manifest: dict, state: dict) -> int:
    """Advance a bounded portion of the workflow; rerun while training is queued."""
    feedback_endpoint(args.base_url)
    if not args.data_snapshot.strip():
        raise ValueError("data-snapshot cannot be empty")
    if not 1 <= args.max_polls <= 100 or not math.isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 60:
        raise ValueError("max-polls=1..100; poll-seconds=1..60")
    if state.get("data_snapshot") not in (None, args.data_snapshot):
        raise ValueError("Cannot change the data snapshot label within an experiment")
    if not args.candidate_model:
        submit(args.run_dir, manifest, state, eligible=args.eligible_account)
        status(args.run_dir, manifest, state, polls=args.max_polls, interval=args.poll_seconds)
        if state["job_status"] != "succeeded":
            print("Training not succeeded; comparison has not started. Re-run run to check again.")
            return 2 if state["job_status"] in TERMINAL else 3
    if not state.get("frozen_artifacts"):
        capture(args.run_dir, manifest, state, args.base_url, args.data_snapshot)
    report = compare(args.run_dir, manifest, state, candidate_model=args.candidate_model)
    return 2 if "incomplete_evaluation" in report["reasons"] else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Offline dataset audit and experiment manifest")
    p.add_argument("--dataset-dir", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    for flag in ("base-model", "baseline-model", "judge-model"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--cases", type=Path, action="append", default=[])
    p.add_argument("--holdout", type=Path, action="append", default=[])
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--max-tokens", type=int, default=700)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--max-calls", type=int)
    p.add_argument("--min-improvement", type=float, default=0.02)
    p.add_argument("--min-cases", type=int, default=20)
    for name in ("capture", "freeze", "submit", "attach", "status", "compare", "run"):
        p = commands.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        if name in {"capture", "run"}:
            p.add_argument("--base-url", required=True, help="Direct backend URL, not frontend /api proxy")
        if name in {"capture", "freeze", "run"}:
            p.add_argument("--data-snapshot", required=True)
        if name == "freeze":
            p.add_argument("--results", type=Path, action="append", required=True)
        if name in {"submit", "run"}:
            p.add_argument("--eligible-account", action="store_true")
        if name == "attach":
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument("--file-id")
            group.add_argument("--job-id")
        if name in {"status", "run"}:
            p.add_argument("--max-polls", type=int, default=1)
            p.add_argument("--poll-seconds", type=float, default=15)
        if name in {"compare", "run"}:
            p.add_argument("--candidate-model", help="Optional existing OpenAI fine-tuned model ID (ft:...)")
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    load_backend_env()
    try:
        if args.command == "prepare":
            prepare(args)
            return 0
        with lock(args.run_dir):
            manifest, state = load_run(args.run_dir)
            if args.command == "run":
                return advance(args, manifest, state)
            elif args.command == "capture":
                capture(args.run_dir, manifest, state, args.base_url, args.data_snapshot)
            elif args.command == "freeze":
                freeze(args.run_dir, manifest, state, [r for p in args.results for r in read_candidates(p)], args.data_snapshot)
            elif args.command == "submit":
                submit(args.run_dir, manifest, state, eligible=args.eligible_account)
            elif args.command == "attach":
                attach(args.run_dir, manifest, state, file_id=args.file_id, job_id=args.job_id)
            elif args.command == "status":
                status(args.run_dir, manifest, state, polls=args.max_polls, interval=args.poll_seconds)
            elif args.command == "compare":
                report = compare(args.run_dir, manifest, state, candidate_model=args.candidate_model)
                return 2 if "incomplete_evaluation" in report["reasons"] else 0
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # SDK error strings can contain response data. Only known local validation
        # messages are printed; provider bodies/credentials never become CLI logs.
        cli.exit(2, f"{type(exc).__name__}: {exc}\n")
    except Exception as exc:
        cli.exit(2, f"API operation failed: {type(exc).__name__}; inspect state.json before retrying\n")


if __name__ == "__main__":
    raise SystemExit(main())
