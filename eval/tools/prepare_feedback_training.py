"""Collect admin feedback and optionally build reviewed training/evaluation JSONL.

Run from the project root. See docs/evaluation/FEEDBACK_TRAINING.md.
Only GET requests are made; no DB writes, model calls, uploads or training jobs.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from getpass import getpass
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from backend.app.feedback_training import question_key
from .build_feedback_training import DEFAULT_HOLDOUTS, build_datasets, read_candidates
from .eval_ask import _read_jsonl
from .llm_budget import load_backend_env


class NoRedirect(HTTPRedirectHandler):
    """Never forward an admin bearer token through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def feedback_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("base-url must be an HTTP(S) URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("use HTTPS for remote servers, or a localhost SSH tunnel")
    return base_url.rstrip("/") + "/admin/feedback/training-candidates"


def _candidate_id(row: dict[str, Any]) -> str:
    value = row.get("id")
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("feedback id must be a canonical UUID")
    return value


def validate_candidates(rows: list[dict[str, Any]]) -> None:
    """Require unreviewed exports as provenance, not already edited labels."""
    seen = set()
    for row in rows:
        candidate_id = _candidate_id(row)
        if candidate_id in seen:
            raise ValueError(f"duplicate feedback id: {candidate_id}")
        seen.add(candidate_id)
        if row.get("schema_version") != 1 or row.get("rating") != "down":
            raise ValueError(f"{candidate_id}: expected schema_version=1 downvote export")
        if not isinstance(row.get("review"), dict) or row["review"].get("status") != "pending":
            raise ValueError(f"{candidate_id}: input must be an unreviewed export; use --reviewed for labels")
        if row.get("pairing_status") not in (
            "matched", "missing_answer", "missing_question", "ambiguous_question",
        ):
            raise ValueError(f"{candidate_id}: invalid pairing_status")
        for field in ("question", "rejected_answer", "feedback_updated_at"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"{candidate_id}: missing or invalid {field}")
        updated_at = datetime.fromisoformat(row["feedback_updated_at"].replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            raise ValueError(f"{candidate_id}: feedback_updated_at must include a timezone")
        for field in ("answer_sources", "reasons"):
            if not isinstance(row.get(field), list):
                raise ValueError(f"{candidate_id}: missing or invalid {field}")
        if "comment" not in row or (row["comment"] is not None and not isinstance(row["comment"], str)):
            raise ValueError(f"{candidate_id}: missing or invalid comment")
        question = row["question"]
        if row.get("original_question_key") != (question_key(question) if question else None):
            raise ValueError(f"{candidate_id}: original_question_key does not match the exported question")
        if row["pairing_status"] == "matched" and (not question.strip() or not row["rejected_answer"].strip()):
            raise ValueError(f"{candidate_id}: matched export requires a question and answer")


def fetch_candidates(
    base_url: str, token: str, *, page_size: int = 20, timeout: float = 60,
) -> list[dict[str, Any]]:
    endpoint = feedback_endpoint(base_url)
    if not token.strip() or any(character.isspace() for character in token):
        raise ValueError("a valid admin access_token is required")
    if not 1 <= page_size <= 50 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("page-size must be 1..50 and timeout must be positive and finite")
    opener = build_opener(NoRedirect())
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {"limit": str(page_size)}
        if cursor:
            params["after_id"] = cursor
        request = Request(endpoint + "?" + urlencode(params), headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "Cache-Control": "no-store",
        })
        try:
            with opener.open(request, timeout=timeout) as response:
                page = json.load(response)
        except HTTPError as exc:
            # Do not echo server bodies/headers, which could contain private content.
            hints = {
                401: "admin login expired or invalid; obtain a new access_token",
                403: "account must be in the backend ADMIN_EMAILS allowlist",
                404: "check base-url and deploy the admin training-candidates endpoint",
                503: "check backend deployment and availability",
            }
            code = exc.code
            exc.close()
            raise ValueError(f"feedback API HTTP {code}: {hints.get(code, 'request failed; no output written')}") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ValueError("feedback API connection failed or timed out; no output written") from None
        except (ValueError, UnicodeError):
            raise ValueError("feedback API returned invalid JSON; check base-url") from None
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) or "nextCursor" not in page:
            raise ValueError("invalid feedback API page")
        items, next_cursor = page["items"], page["nextCursor"]
        if len(items) > page_size or any(not isinstance(item, dict) for item in items):
            raise ValueError("invalid feedback API items")
        validate_candidates(items)
        previous_id = cursor
        for item in items:
            if previous_id is not None and item["id"] <= previous_id:
                raise ValueError("feedback API ids did not advance; no output written")
            previous_id = item["id"]
        if next_cursor is not None:
            if not items or next_cursor != items[-1]["id"] or next_cursor == cursor:
                raise ValueError("feedback API cursor did not advance; no output written")
        rows.extend(items)
        if next_cursor is None:
            return rows
        cursor = next_cursor


def merge_reviews(
    current: list[dict[str, Any]], reviewed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carry labels over only when their exported provenance is unchanged.

    Reviewers can rewrite question for context/privacy. Other export fields must
    remain intact. Missing/changed approved rows abort instead of silently training
    on a withdrawn vote or silently dropping a reviewed correction.
    """
    by_id = {_candidate_id(row): row for row in current}
    merged = {key: deepcopy(row) for key, row in by_id.items()}
    seen = set()
    provenance_fields = (
        "schema_version", "rating", "feedback_updated_at", "pairing_status",
        "original_question_key", "rejected_answer", "answer_sources", "reasons", "comment",
    )
    for row in reviewed:
        candidate_id = _candidate_id(row)
        if candidate_id in seen:
            raise ValueError(f"duplicate reviewed feedback id: {candidate_id}")
        seen.add(candidate_id)
        review = row.get("review")
        if not isinstance(review, dict) or review.get("status") not in ("pending", "rejected", "approved"):
            raise ValueError(f"{candidate_id}: invalid review.status")
        fresh = by_id.get(candidate_id)
        unchanged = fresh is not None and all(
            field in row and row[field] == fresh[field] for field in provenance_fields
        )
        if not unchanged:
            if review["status"] == "approved":
                raise ValueError(f"{candidate_id}: approved feedback changed or disappeared; collect and review again")
            continue
        merged[candidate_id]["question"] = row.get("question")
        merged[candidate_id]["review"] = deepcopy(review)
    return list(merged.values())


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-url", help="Backend origin, or frontend origin with /api suffix")
    source.add_argument("--input", type=Path, help="Unmodified admin download; offline, without live freshness checks")
    parser.add_argument("--output-dir", type=Path, required=True, help="New output directory; never overwritten")
    parser.add_argument("--reviewed", type=Path, help="Reviewed JSONL; also build train.jsonl and evaluation.jsonl")
    parser.add_argument("--holdout", type=Path, action="append", default=[], help="Additional evaluation JSONL to reserve")
    parser.add_argument("--page-size", type=int, choices=range(1, 51), default=20, metavar="1..50")
    parser.add_argument("--timeout", type=float, default=60, help="Per-page timeout in seconds (default: 60)")
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError("output-dir already exists; choose a new directory")
        if args.holdout and not args.reviewed:
            raise ValueError("--holdout requires --reviewed")
        if args.base_url:
            feedback_endpoint(args.base_url)
            load_backend_env()
            token = os.getenv("FEEDBACK_API_TOKEN") or getpass("Admin access_token (hidden): ")
            current = fetch_candidates(args.base_url, token, page_size=args.page_size, timeout=args.timeout)
        else:
            current = read_candidates(args.input)
        validate_candidates(current)
        if not current:
            raise ValueError("no current downvote candidates; no output written")
        prepared = merge_reviews(current, read_candidates(args.reviewed)) if args.reviewed else deepcopy(current)
        train, evaluation, skipped = [], [], len(prepared)
        if args.reviewed:
            holdouts = [case for path in [*DEFAULT_HOLDOUTS, *args.holdout] for case in _read_jsonl(path)]
            train, evaluation, skipped = build_datasets(prepared, holdouts)
            if not train and not evaluation:
                raise ValueError("no approved examples; review the candidates before building datasets")
        summary = {
            "schema_version": 1,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source": "api" if args.base_url else "file",
            "freshness_checked": bool(args.base_url and args.reviewed),
            "candidates": len(current), "train": len(train), "evaluation": len(evaluation), "skipped": skipped,
            "dataset_built": bool(args.reviewed),
        }
        # Publish only complete results; failures cannot leave a partial dataset.
        args.output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".feedback-", dir=args.output_dir.parent) as temporary:
            staged = Path(temporary) / "prepared"
            staged.mkdir()
            _write_jsonl(staged / "candidates.jsonl", current)
            _write_jsonl(staged / "review.jsonl", prepared)
            if args.reviewed:
                _write_jsonl(staged / "train.jsonl", train)
                _write_jsonl(staged / "evaluation.jsonl", evaluation)
            (staged / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            staged.rename(args.output_dir)
    except (ValueError, OSError, EOFError) as exc:
        parser.exit(2, f"{exc}\n")
    print(f"candidates={len(current)} train={len(train)} evaluation={len(evaluation)} skipped={skipped}; output={args.output_dir}")
    if not args.reviewed:
        print(f"Review {args.output_dir / 'review.jsonl'}, then rerun with --reviewed and a new --output-dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
