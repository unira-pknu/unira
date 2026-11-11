"""Turn negative feedback into review drafts, never positive training labels."""

from __future__ import annotations

import hashlib
import re
from typing import Any


def question_key(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip()).lower()
    normalized = re.sub(r"[\s?!.~…]+$", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_training_candidate(row: dict[str, Any], questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Questions must be the latest two user messages before this answer, newest first.

    The schema has no explicit reply-to ID. Equal timestamps cannot establish
    message order, so ambiguous pairs remain blocked from training export.
    """
    answer = row.get("chat_messages") or {}
    status = "matched"
    question = ""
    if answer.get("role") != "assistant" or not answer.get("content"):
        status = "missing_answer"
    elif not questions or not (questions[0].get("content") or "").strip():
        status = "missing_question"
    elif (questions[0]["created_at"] == answer.get("created_at")
          or (len(questions) > 1 and questions[0]["created_at"] == questions[1]["created_at"])):
        status = "ambiguous_question"
    else:
        question = questions[0]["content"]
    return {
        "schema_version": 1,
        "id": row["id"],
        "rating": row["rating"],
        "feedback_updated_at": row["updated_at"],
        "pairing_status": status,
        "question": question,
        "original_question_key": question_key(question) if question else None,
        "rejected_answer": answer.get("content") or "",
        "answer_sources": answer.get("sources") or [],
        "reasons": row.get("reasons") or [],
        "comment": row.get("comment"),
        "review": {
            "status": "pending",
            "split": None,
            "corrected_answer": "",
            "verified_sources": [],
            "required_facts": [],
            "forbidden_claims": [],
            "reviewed_by": "",
            "reviewed_at": "",
            "privacy_checked": False,
            "context_checked": False,
        },
    }
