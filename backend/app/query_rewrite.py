"""LLM-based search query planning: standalone-question resolution, compound
query decomposition, and abbreviation normalization -- all before retrieval.

Three structural gaps this closes, all upstream of search() rather than
patched dataset-by-dataset inside it:

1. Multi-turn: retrieval only ever sees this turn's question text. A natural
   follow-up like "그럼 거기 대표 전화번호는요?" carries no literal keyword for
   what "거기" refers to, even though answer generation (which does receive
   chat_history) would have understood it fine.
2. Vocabulary/naming mismatch: students ask with colloquial abbreviations
   ("컴공") but official documents only use the formal name ("컴퓨터·인공지능
   공학부"). Hand-maintained synonym tables (QUERY_TERM_GROUPS in
   retrieval.py) catch specific pairs we've already found broken, one at a
   time -- whack-a-mole. An LLM already knows the common abbreviation ->
   formal-name mappings for a Korean university and generalizes to ones we
   haven't seen fail yet.
3. Compound queries: "도서관 전화번호나 증명서 발급기 위치" packs two distinct
   intents into one sentence. A single embedding/lexical search represents
   both at once and typically favors whichever intent dominates the vector,
   silently dropping the other. Splitting into separate standalone
   sub-queries lets each one get its own dedicated search() call (vector +
   lexical + rerank, unchanged) instead of diluting one shared candidate
   pool.

This module only ever changes what text retrieval searches with. The
original question and chat_history are still what reaches
generate_answer/stream_answer unchanged -- so it has no effect on answer
generation or the streaming response shape.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List

from openai import OpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 대학교 챗봇의 검색 전처리기입니다. 사용자의 마지막 질문(과 있다면 "
    "대화 이력)을 검색 엔진에 바로 넘길 수 있는 독립적인 하위 질의(sub-query) "
    "목록으로 바꿉니다.\n\n"
    "규칙:\n"
    "1. 대화 이력이 있고 마지막 질문에 '거기', '그거', '그럼'처럼 이전 턴을 "
    "가리키는 대명사나 생략된 주어가 있으면, 대화 이력에서 언급된 구체적인 "
    "명사로 바꾸세요.\n"
    "2. 마지막 질문에 서로 다른 정보를 요구하는 의도가 두 개 이상 섞여 있으면"
    "(예: 'A와 B 알려줘', 'A나 B 위치') 각 의도를 별도의 하위 질의로 나누세요. "
    "의도가 하나뿐이면 하위 질의도 하나만 만드세요. 최대 3개까지만 나누세요.\n"
    "3. 각 하위 질의에 구어체 줄임말이나 비공식 표현이 있으면, 대학 공식 문서에서 "
    "쓸 법한 정식 명칭이나 관련 핵심어를 자연스럽게 덧붙이세요(예: '컴공'이라는 "
    "말이 있으면 '컴퓨터·인공지능공학부'라는 표현도 함께 넣으세요). 원래 의미를 "
    "바꾸거나 대화에 없는 새로운 사실을 추가하지 마세요.\n"
    "4. 질문 자체가 이미 독립적이고 단일 의도이며 줄임말이 없으면 그대로 하나의 "
    "하위 질의로 돌려주세요.\n"
    "5. 인사·감사·작별·챗봇에 대한 질문(누구야, 뭐 할 수 있어)·학교와 무관한 잡담처럼 "
    "찾아볼 정보가 없는 말이면 queries를 빈 목록으로 두세요. 학교·학과·수업·장학·"
    "시설·일정·규정 등 대학 생활과 조금이라도 관련된 정보를 묻는다면 반드시 하위 질의를 "
    "만드세요. 애매하면 하위 질의를 만드세요.\n\n"
    '다음 JSON 형식으로만 답하세요: {"queries": ["...", "..."]}'
)

DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_SUB_QUERIES = 3


def plan_search_queries(
    *,
    openai_client: OpenAI,
    model: str,
    question: str,
    chat_history: List[Dict[str, str]],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[str]:
    """Return 1+ standalone, search-ready queries for `question`.

    Always calls the LLM (even with no chat_history) since abbreviation
    normalization and compound-query splitting are needed on a first
    message too, not just follow-ups. Falls back to `[question]` unchanged
    whenever the call fails, times out, or returns something unusable --
    a broken planning step must never block the request.

    An empty list means small talk ("안녕", "고마워") with nothing to look
    up; the caller answers it without searching (rule 5 of the prompt).
    """
    history_lines = [
        f"{'사용자' if turn.get('role') == 'user' else '어시스턴트'}: {turn.get('content', '')}"
        for turn in chat_history
        if turn.get("content")
    ]
    user_prompt = (
        ("[대화 이력]\n" + "\n".join(history_lines) + "\n\n" if history_lines else "")
        + f"[마지막 사용자 질문]\n{question}"
    )

    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=200,
            timeout=timeout,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        raw_queries = parsed["queries"]
        if not isinstance(raw_queries, list):
            raise ValueError(f"queries is not a list: {raw_queries!r}")
        queries = [str(q).strip() for q in raw_queries if str(q).strip()]
    except Exception:
        logger.warning("query planning failed, falling back to raw question", exc_info=True)
        return [question]

    if not queries:
        logger.info("query planning: %r -> small talk", question)
        return []

    queries = queries[:MAX_SUB_QUERIES]
    logger.info("query planning: %r -> %r", question, queries)
    return queries
