"""Keep retrieval warm while nobody is asking.

The first question after about an hour of silence took 19s on 2026-10-27
against 7-9s normally: retrieval alone went from 2.2s to 9.8s (Supabase's
caches and connections go cold). Running one search -- no GPT call, so no
OpenAI cost -- at startup and every few minutes keeps that path warm, and
also means the first question after a restart doesn't pay for it.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .deps import AppState

logger = logging.getLogger(__name__)

WARMUP_QUERY = "학사일정"


async def keep_warm(state: AppState, interval_seconds: float) -> None:
    """Run one search now and then every `interval_seconds` until cancelled.
    A failed round is logged and skipped; it never stops the loop."""
    from .routers.ask import _search_one  # router imports deps; import late to avoid a cycle

    while True:
        started = time.perf_counter()
        try:
            rows = await asyncio.to_thread(_search_one, state, WARMUP_QUERY)
            logger.info(
                "warmup: search rows=%d latency_ms=%.0f",
                len(rows),
                (time.perf_counter() - started) * 1000,
            )
        except Exception:
            logger.warning("warmup: search failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
