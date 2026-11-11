"""Evaluate the backend /ask API with JSONL question sets."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .provenance import code_state, file_sha256, timestamp


DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_QUESTIONS_PATH = Path("eval/cases/smoke.jsonl")
DEFAULT_OUTPUT_PATH = Path("eval/results/smoke.jsonl")
STRUCTURED_REQUIRED_FIELDS = {
    "id",
    "question",
    "category",
    "answerable",
    "expected_source",
    "required_facts",
    "forbidden_claims",
    "expected_no_info",
    "tags",
    "difficulty",
}
STRUCTURED_MARKER_FIELDS = STRUCTURED_REQUIRED_FIELDS - {"question", "category"}
CASE_ID_RE = re.compile(r"^[a-z0-9_]+$")
DIFFICULTIES = {"easy", "medium", "hard"}
EXPECTED_SOURCE_FIELDS = {"dataset", "title_keywords", "allowed_urls", "source_ids"}
REQUIRED_FACT_FIELDS = {"id", "description", "keywords"}


def _require_string_list(value: Any, *, location: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{location}: expected a list of non-empty strings")


def _validate_structured_case(row: dict[str, Any], *, location: str) -> None:
    """Validate one structured evaluation case without external dependencies."""
    missing = STRUCTURED_REQUIRED_FIELDS - row.keys()
    if missing:
        raise ValueError(f"{location}: missing fields: {', '.join(sorted(missing))}")

    for key in ("id", "question", "category"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise ValueError(f"{location}: {key} must be a non-empty string")
    if not CASE_ID_RE.fullmatch(row["id"]):
        raise ValueError(f"{location}: id must contain only lowercase letters, digits, and underscores")
    for key in ("answerable", "expected_no_info"):
        if not isinstance(row[key], bool):
            raise ValueError(f"{location}: {key} must be a boolean")
    if row["expected_no_info"] == row["answerable"]:
        raise ValueError(
            f"{location}: expected_no_info must be the inverse of answerable"
        )
    if row["difficulty"] not in DIFFICULTIES:
        raise ValueError(
            f"{location}: difficulty must be one of {sorted(DIFFICULTIES)}"
        )

    _require_string_list(row["forbidden_claims"], location=f"{location}.forbidden_claims")
    _require_string_list(row["tags"], location=f"{location}.tags")

    facts = row["required_facts"]
    if not isinstance(facts, list):
        raise ValueError(f"{location}.required_facts: expected a list")
    fact_ids: set[str] = set()
    for index, fact in enumerate(facts):
        fact_location = f"{location}.required_facts[{index}]"
        if not isinstance(fact, dict):
            raise ValueError(f"{fact_location}: expected an object")
        missing_fact = REQUIRED_FACT_FIELDS - fact.keys()
        if missing_fact:
            raise ValueError(
                f"{fact_location}: missing fields: {', '.join(sorted(missing_fact))}"
            )
        if not isinstance(fact["id"], str) or not fact["id"].strip():
            raise ValueError(f"{fact_location}.id: expected a non-empty string")
        if not CASE_ID_RE.fullmatch(fact["id"]):
            raise ValueError(
                f"{fact_location}.id: use only lowercase letters, digits, and underscores"
            )
        if fact["id"] in fact_ids:
            raise ValueError(f"{fact_location}.id: duplicate fact id {fact['id']!r}")
        fact_ids.add(fact["id"])
        if not isinstance(fact["description"], str) or not fact["description"].strip():
            raise ValueError(f"{fact_location}.description: expected a non-empty string")
        _require_string_list(fact["keywords"], location=f"{fact_location}.keywords")

    expected_source = row["expected_source"]
    if row["answerable"]:
        if not isinstance(expected_source, dict):
            raise ValueError(f"{location}.expected_source: answerable cases require an object")
        missing_source = EXPECTED_SOURCE_FIELDS - expected_source.keys()
        if missing_source:
            raise ValueError(
                f"{location}.expected_source: missing fields: "
                f"{', '.join(sorted(missing_source))}"
            )
        for key in EXPECTED_SOURCE_FIELDS:
            _require_string_list(
                expected_source[key],
                location=f"{location}.expected_source.{key}",
            )
        if not any(expected_source[key] for key in EXPECTED_SOURCE_FIELDS):
            raise ValueError(
                f"{location}.expected_source: at least one source criterion is required"
            )
    elif expected_source is not None:
        raise ValueError(f"{location}.expected_source: unanswerable cases must use null")
    elif facts:
        raise ValueError(f"{location}.required_facts: unanswerable cases must use []")


def _is_structured_case(row: dict[str, Any]) -> bool:
    return bool(STRUCTURED_MARKER_FIELDS & row.keys())


def _validate_unique_ids(rows: list[dict[str, Any]], *, location: str) -> None:
    ids = [str(row["id"]) for row in rows if "id" in row]
    duplicate_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicate_ids:
        raise ValueError(f"{location}: duplicate case ids: {', '.join(duplicate_ids)}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: each JSONL row must be an object")
            if not str(row.get("question") or "").strip():
                raise ValueError(f"{path}:{line_no}: missing question")
            if _is_structured_case(row):
                _validate_structured_case(row, location=f"{path}:{line_no}")
            rows.append(row)
    _validate_unique_ids(rows, location=str(path))
    return rows


def _post_ask(
    base_url: str, question: str, timeout: float, *, token: str | None = None,
    include_evidence: bool = True,
) -> tuple[int | None, dict[str, Any]]:
    url = urljoin(base_url.rstrip("/") + "/", "ask")
    body = json.dumps({"question": question, "stream": False, "include_evidence": include_evidence}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload)
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError:
            data = {"detail": payload}
        return exc.code, {"error": data}
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, {"error": str(exc)}


def _top_similarity(sources: list[Any]) -> float | None:
    if not sources or not isinstance(sources[0], dict):
        return None
    value = sources[0].get("similarity")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result_row(
    *,
    question_row: dict[str, Any],
    status_code: int | None,
    response: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    sources = response.get("sources")
    if not isinstance(sources, list):
        sources = []
    error = response.get("error")
    return {
        "id": question_row.get("id", ""),
        "question": question_row.get("question", ""),
        "category": question_row.get("category", ""),
        "answerable": question_row.get("answerable"),
        "expected_no_info": question_row.get("expected_no_info"),
        "expected_source": question_row.get("expected_source"),
        "required_facts": question_row.get("required_facts", []),
        "forbidden_claims": question_row.get("forbidden_claims", []),
        "tags": question_row.get("tags", []),
        "difficulty": question_row.get("difficulty", ""),
        "expected_behavior": question_row.get("expected_behavior", ""),
        "label_status": question_row.get("label_status"),
        "scenario_tags": question_row.get("scenario_tags", []),
        "answer": response.get("answer", "") if not error else "",
        "sources": sources,
        "response_answerable": response.get("answerable"),
        "retrieval_status": response.get("retrieval_status"),
        "evaluation_evidence": response.get("evaluation_evidence"),
        "source_count": len(sources),
        "top_similarity": _top_similarity(sources),
        "status_code": status_code,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "error": error,
    }


def run_eval(
    *,
    base_url: str,
    questions_path: Path,
    output_path: Path,
    timeout: float,
    limit: int | None,
    sleep_seconds: float,
    token: str | None = None,
    data_snapshot: str | None = None,
    backend_revision: str | None = None,
) -> None:
    if output_path.resolve() == questions_path.resolve():
        raise ValueError("results output must differ from the input question set")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    questions = _read_jsonl(questions_path)
    available_case_count = len(questions)
    if limit is not None:
        questions = questions[:limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    error_count = 0
    endpoint = urlsplit(base_url)
    manifest = {
        "schema_version": 1, "started_at": timestamp(), "finished_at": None,
        "questions_path": str(questions_path), "questions_sha256": file_sha256(questions_path),
        "expected_case_ids": [row.get("id") for row in questions],
        "available_case_count": available_case_count,
        "base_url": urlunsplit((endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", "")),
        "selected_case_count": len(questions), "limit": limit,
        "data_snapshot": data_snapshot, "backend_revision": backend_revision,
        "timeout": timeout, "include_evidence": True, "local_code": code_state(),
    }
    manifest_path = output_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        for index, row in enumerate(questions, start=1):
            question = str(row["question"]).strip()
            started = time.perf_counter()
            status_code, response = _post_ask(base_url, question, timeout, token=token)
            elapsed = time.perf_counter() - started
            result = _result_row(
                question_row=row,
                status_code=status_code,
                response=response,
                elapsed_seconds=elapsed,
            )
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            evidence = result.get("evaluation_evidence")
            if not result["error"] and (not isinstance(evidence, dict) or evidence.get("schema_version") != 1):
                raise RuntimeError("backend did not capture evaluation evidence; run the updated backend before evaluating")

            if result["error"]:
                error_count += 1
                status = f"ERROR {status_code or '-'}"
            else:
                ok_count += 1
                status = f"OK {status_code}"
            print(f"[{index}/{len(questions)}] {status} {question}")

            if sleep_seconds > 0 and index < len(questions):
                time.sleep(sleep_seconds)

    manifest.update(finished_at=timestamp(), results_sha256=file_sha256(output_path),
                    ok_count=ok_count, error_count=error_count)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"saved={output_path} total={len(questions)} ok={ok_count} errors={error_count}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("ASK_API_BASE_URL")
        or os.getenv("API_BASE_URL")
        or DEFAULT_BASE_URL,
        help=f"Backend API base URL. Defaults to {DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS_PATH,
        help=f"Input JSONL question file. Defaults to {DEFAULT_QUESTIONS_PATH}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output JSONL result file. Defaults to {DEFAULT_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Evaluate only the first N questions.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Sleep seconds between requests.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("ASK_API_TOKEN"),
        help=(
            "Supabase access token sent as 'Authorization: Bearer <token>'. "
            "/ask requires login; defaults to the ASK_API_TOKEN env var."
        ),
    )
    parser.add_argument("--data-snapshot", help="Operator-assigned DB/ingestion snapshot identifier; no database access is performed.")
    parser.add_argument("--backend-revision", help="Revision of the evaluated backend (not necessarily the local checkout).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_eval(
        base_url=args.base_url,
        questions_path=args.questions,
        output_path=args.output,
        timeout=args.timeout,
        token=args.token,
        limit=args.limit,
        sleep_seconds=args.sleep,
        data_snapshot=args.data_snapshot,
        backend_revision=args.backend_revision,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
