"""Per-user rate limiting for /ask.

A small hand-rolled sliding-window limiter, not slowapi: slowapi's
`@limiter.limit()` decorator resolves the endpoint's type hints using the
*wrapper* function's `__globals__` (the slowapi module's, not app.routers.ask's),
which breaks FastAPI's body-vs-query inference whenever the route module uses
`from __future__ import annotations` (postponed evaluation) — as this codebase
does everywhere. A plain dependency avoids that whole bug class.

The backend runs as a single process (school server, or one Hugging Face
Space instance), so in-memory state is fine — no Redis needed. Revisit this
if the backend ever runs as multiple replicas (each would keep its own
counters). Note the counters also reset whenever the process restarts, so the
daily cap is a cost guard against scripted abuse, not an exact quota.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import date

from fastapi import Depends, HTTPException, Request

from .deps import AppState, get_state, is_admin_request, require_user
from .freshness import today_kst

MAX_REQUESTS_PER_WINDOW = 20
WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_daily: dict[str, tuple[date, int]] = {}


def _check(key: str, *, limit: int, window_seconds: float) -> None:
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(
                status_code=429,
                detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            )
        hits.append(now)


def _check_daily(key: str, *, limit: int, today: date) -> None:
    """Count one request for `today` (KST); 429 once `limit` is reached.
    Each key keeps only its current day, so memory stays one entry per user."""
    with _lock:
        day, count = _daily.get(key, (today, 0))
        if day != today:
            count = 0
        if count >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"오늘 질문 가능 횟수({limit}회)를 모두 사용했습니다. 내일 다시 이용해 주세요.",
            )
        _daily[key] = (today, count + 1)


def enforce_ask_rate_limit(
    request: Request,
    user_id: str = Depends(require_user),
    state: AppState = Depends(get_state),
) -> str:
    """FastAPI dependency: verifies login (via require_user) and rate-limits
    the verified user id. Returning user_id lets the route reuse it without
    a second Depends(require_user) call (FastAPI dependency caching would
    dedupe that anyway, but this keeps the route's dependency list simple).
    """
    key = f"user:{user_id}"
    _check(key, limit=MAX_REQUESTS_PER_WINDOW, window_seconds=WINDOW_SECONDS)
    daily_limit = state.settings.ask_daily_limit
    if daily_limit > 0 and not is_admin_request(request, state):
        _check_daily(key, limit=daily_limit, today=today_kst())
    return user_id
