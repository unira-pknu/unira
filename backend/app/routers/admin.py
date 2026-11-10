"""Admin dashboard API: real feedback / question stats instead of mock JSON.

Reads every user's chats and feedback with the service-role client (RLS is
bypassed), so every route here requires an allowlisted admin
(Settings.admin_emails, see deps.require_admin).
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..deps import AppState, get_state, is_admin_request, require_admin, require_user
from ..feedback_training import build_training_candidate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

FEEDBACK_LIMIT = 500  # 대시보드에 보여줄 최근 피드백 수
QUESTION_SAMPLE_LIMIT = 1000  # 자주 묻는 질문 집계에 쓰는 최근 질문 수
TOP_QUESTIONS = 10

_TRAILING_PUNCT_RE = re.compile(r"[\s?!.~…]+$")
_SPACES_RE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """Group trivially different spellings: whitespace and trailing ?!.~ ."""
    return _TRAILING_PUNCT_RE.sub("", _SPACES_RE.sub(" ", text.strip())).lower()


def _question_before(user_messages: List[Dict[str, Any]], answer_created_at: str) -> Optional[str]:
    """Latest user message in the same chat sent at or before the answer."""
    earlier = [m for m in user_messages if (m.get("created_at") or "") <= answer_created_at]
    return earlier[-1]["content"] if earlier else None


def build_dashboard(
    *,
    feedback_rows: Iterable[Dict[str, Any]],
    user_messages_by_chat: Dict[str, List[Dict[str, Any]]],
    recent_questions: Iterable[str],
    total_feedback: int,
    up_count: int,
    total_questions: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Shape DB rows into the admin page's existing (former mock) structure.

    feedback_rows: message_feedback rows (newest first) with the rated
    assistant message embedded as `chat_messages` {content, chat_id, created_at}.
    user_messages_by_chat: chat_id -> user messages sorted by created_at.
    """
    thumbs: List[Dict[str, Any]] = []
    comments: List[Dict[str, Any]] = []
    for row in feedback_rows:
        answer_msg = row.get("chat_messages") or {}
        question = _question_before(
            user_messages_by_chat.get(answer_msg.get("chat_id") or "", []),
            answer_msg.get("created_at") or "",
        )
        thumbs.append({
            "id": row["id"],
            "kind": row["rating"],
            "question": question or "(질문을 찾을 수 없음)",
            "answer": answer_msg.get("content") or "",
            "createdAt": row["created_at"],
            "reasons": row.get("reasons") or [],
        })
        if row.get("comment"):
            comments.append({
                "id": row["id"],
                "comment": row["comment"],
                "relatedQuestion": question,
                "createdAt": row["created_at"],
            })

    # 자주 묻는 질문: 정규화 키로 묶고, 표시는 가장 최근 원문으로.
    counts: Counter[str] = Counter()
    display: Dict[str, str] = {}
    for text in recent_questions:
        if not text or not text.strip():
            continue
        key = normalize_question(text)
        counts[key] += 1
        display.setdefault(key, text.strip())
    top = [
        {"id": f"tq-{index}", "question": display[key], "count": count}
        for index, (key, count) in enumerate(counts.most_common(TOP_QUESTIONS), start=1)
    ]

    return {
        "summary": {
            "totalFeedback": total_feedback,
            "upCount": up_count,
            "downCount": max(0, total_feedback - up_count),
            "satisfactionRate": (up_count / total_feedback) if total_feedback else None,
            "totalQuestions": total_questions,
            "questionSampleSize": sum(counts.values()),
            "lastUpdatedAt": (now or datetime.now(timezone.utc)).isoformat(),
        },
        "feedback": {"thumbs": thumbs, "comments": comments},
        "topQuestions": top,
    }


def _count(query: Any) -> int:
    response = query.execute()
    return int(response.count or 0)


@router.get("/feedback/training-candidates")
def training_candidates(
    response: Response,
    after_id: Optional[UUID] = None,
    limit: int = Query(default=20, ge=1, le=50),
    state: AppState = Depends(get_state),
    _admin_id: str = Depends(require_admin),
) -> Dict[str, Any]:
    """Read current downvotes with UUID keyset pagination, beyond the dashboard's 500 rows."""
    response.headers["Cache-Control"] = "no-store"
    client = state.supabase
    try:
        query = (
            client.table("message_feedback")
            .select("id, rating, reasons, comment, updated_at, "
                    "chat_messages(role, content, sources, chat_id, created_at)")
            .eq("rating", "down")
            .order("id")
            .limit(limit + 1)
        )
        if after_id:
            query = query.gt("id", str(after_id))
        rows = query.execute().data or []
        items = []
        for row in rows[:limit]:
            answer = row.get("chat_messages") or {}
            questions = []
            if answer.get("chat_id") and answer.get("created_at"):
                questions = (
                    client.table("chat_messages")
                    .select("content, created_at")
                    .eq("chat_id", answer["chat_id"])
                    .eq("role", "user")
                    .lte("created_at", answer["created_at"])
                    .order("created_at", desc=True)
                    .limit(2)
                    .execute().data or []
                )
            items.append(build_training_candidate(row, questions))
        return {"items": items, "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None}
    except Exception:
        logger.exception("feedback training candidate query failed")
        raise HTTPException(status_code=502, detail="학습 후보를 불러오지 못했습니다.")


@router.get("/me")
def admin_me(
    request: Request,
    state: AppState = Depends(get_state),
    _user_id: str = Depends(require_user),
) -> Dict[str, bool]:
    """Whether the logged-in caller is an admin, so the UI can hide the
    Admin entry from everyone else (the dashboard itself is still gated by
    require_admin)."""
    return {"isAdmin": is_admin_request(request, state)}


@router.get("/dashboard")
def admin_dashboard(
    state: AppState = Depends(get_state),
    _admin_id: str = Depends(require_admin),
) -> Dict[str, Any]:
    client = state.supabase
    try:
        feedback_rows = (
            client.table("message_feedback")
            .select("id, rating, reasons, comment, created_at, chat_messages(content, chat_id, created_at)")
            .order("created_at", desc=True)
            .limit(FEEDBACK_LIMIT)
            .execute()
            .data
            or []
        )
        total_feedback = _count(client.table("message_feedback").select("id", count="exact").limit(1))
        up_count = _count(
            client.table("message_feedback").select("id", count="exact").eq("rating", "up").limit(1)
        )
        total_questions = _count(
            client.table("chat_messages").select("id", count="exact").eq("role", "user").limit(1)
        )

        chat_ids = sorted({
            (row.get("chat_messages") or {}).get("chat_id")
            for row in feedback_rows
            if (row.get("chat_messages") or {}).get("chat_id")
        })
        user_messages_by_chat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if chat_ids:
            rows = (
                client.table("chat_messages")
                .select("chat_id, content, created_at")
                .eq("role", "user")
                .in_("chat_id", chat_ids)
                .order("created_at")
                .limit(5000)
                .execute()
                .data
                or []
            )
            for message in rows:
                user_messages_by_chat[message["chat_id"]].append(message)

        recent_questions = [
            row["content"]
            for row in (
                client.table("chat_messages")
                .select("content")
                .eq("role", "user")
                .order("created_at", desc=True)
                .limit(QUESTION_SAMPLE_LIMIT)
                .execute()
                .data
                or []
            )
        ]
    except Exception:
        logger.exception("admin dashboard query failed")
        raise HTTPException(status_code=502, detail="대시보드 데이터를 불러오지 못했습니다.")

    return build_dashboard(
        feedback_rows=feedback_rows,
        user_messages_by_chat=user_messages_by_chat,
        recent_questions=recent_questions,
        total_feedback=total_feedback,
        up_count=up_count,
        total_questions=total_questions,
    )
