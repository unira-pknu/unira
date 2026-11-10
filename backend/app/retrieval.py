"""Supabase RPC search wrapper."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from supabase import Client, create_client

from .freshness import (
    CHUNK_TABLES,
    FRESHNESS_LEXICAL_FETCH,
    FRESHNESS_RPCS,
    OPENING_CHUNKS,
    OPENING_SOURCES,
    current_term,
    freshness_adjustment,
    is_current_term_notice,
    is_schedule_query,
    query_term,
    says_this_term,
    seed_slots,
)
from . import department_focus, title_anchor
from .reranking import normalize_scores
from .search_status import SearchResults, SearchUnavailable
from .retrieval_trace import RetrievalTrace

logger = logging.getLogger(__name__)

ACADEMIC_KEYWORDS = (
    "학사",
    "수강",
    "졸업",
    "전공",
    "복수전공",
    "부전공",
    "전과",
    "성적",
    "학점",
    "장학",
    "휴학",
    "복학",
    "등록",
    "신청",
    "기간",
    "자격",
    "조건",
    "이수",
    "교직",
    "현장실습",
)

NOISE_PHRASES = (
    "개인정보 수집",
    "저작물 활용 동의서",
    "접수번호는 공란",
    "글자크기",
    "연구보고",
    "contents",
    "목 차",
)

GENERIC_STUB_TITLES = (
    "학부 소개",
    "학부소개",
    "졸업 후 진로",
    "졸업후진로",
    "교육과정",
    "국립 부경대학교 대학생활 E-하나로",
    "대학생활 E-하나로",
)

_REPEATED_CHAR_RE = re.compile(r"(.)\1{5,}")
_STANDALONE_JAMO_RE = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]{6,}")

QUERY_TERM_GROUPS = {
    "복수전공": ("복수전공", "복수 전공", "다전공", "전공제도"),
    "부전공": ("부전공", "부 전공", "다전공", "전공제도"),
    "마이크로전공": ("마이크로전공", "마이크로 전공", "소단위전공", "소단위 전공"),
    "전과": ("전과", "전부", "전공변경"),
    "전공": ("전공", "전공제도"),
    "졸업": ("졸업", "졸업요건", "학위수여", "졸업작품", "졸업논문", "졸업사정"),
    "학점": ("학점", "소요학점", "이수학점"),
    "수강신청": ("수강신청", "수강 신청"),
    "휴학": ("휴학",),
    "복학": ("복학",),
    "장학": ("장학", "장학금"),
    # "등록" (bare) used to be a variant here too, but it matches ~2.8k-9.7k
    # rows in the large chunk tables on its own (measured directly against
    # content_tsv) -- ranking + sorting that many rows blew the lexical RPC's
    # statement timeout whenever a query also triggered another group (e.g.
    # "계절수업" -> schedule_005). Keep only the compound term.
    "등록금": ("등록금",),
    "성적": ("성적", "평점", "평균평점"),
    "공결": ("공결", "출석인정", "출석 인정", "결석 인정"),
    # Students still say "학점포기", but the school's system is called
    # "성적자율삭제" (main/244) and no document uses the old word, so the
    # 2026-09-29 eval (rules_016) never retrieved it. Both are rare compound
    # terms, so the OR-query stays small.
    "학점포기": ("학점포기", "학점 포기", "성적자율삭제"),
    "교직": ("교직", "교직과정"),
    "현장실습": ("현장실습", "현장실습학기제"),
    # Bare "생활관"/"기숙사" measured at 300-800+ rows each in the large chunk
    # tables; combined with the compound term in one OR-query that pushed
    # lexical search over its statement timeout. The compound term alone
    # already matches the eval's expected documents, so keep only that.
    "생활관": ("학생생활관",),
    "계절수업": ("계절수업",),
    "외국인유학생": ("외국인유학생", "외국인 유학생", "외국인 신입생", "외국인 학위과정"),
    # Bare "증명서" alone matches 2,300-2,700 rows in rag/notice_chunks --
    # comparable to the "등록"/"졸업"/"전공" danger zone above -- so only
    # narrower phrase/compound variants go in the OR-query. "증명서 발급" as a
    # quoted adjacent-phrase is far rarer (16-142 rows per table) and safe.
    # Covers both how a question might phrase the kiosk ("자동발급기") and
    # how the source page actually phrases it ("무인발급기(기)"), which don't
    # share a literal substring so plain full-text search can't bridge them.
    "증명서발급": ("무인발급기", "무인발급기기", "자동발급기", "제증명", "증명서 발급"),
    # 실제 사용자 질문 사례("컴공학과사무실", 띄어쓰기 없는 구어체 줄임말)가
    # 이 그룹 없이는 검색 후보에 전혀 안 걸리는 것을 확인함 -- 임베딩도
    # "컴공"과 "컴퓨터·인공지능공학부"를 가깝게 못 보고, lexical fallback도
    # 공백 기준 토큰화라 붙여쓴 줄임말은 원문과 겹치는 부분이 없음.
    # "컴퓨터공학과"/"컴퓨터공학부"/"인공지능공학부"는 단독으로 1,400~1,900행씩
    # 매치돼 위 "증명서"급 위험 구간에 가까워 실제 검색어(OR 대상)에서는 빼고
    # LEXICAL_EXCLUDED_VARIANTS로 트리거 전용 처리. "컴퓨터·인공지능공학부"는
    # 개설학과 원문 명칭이라 그대로 둬도 인공지능공학부와 매치 집합이 거의
    # 겹쳐 추가 위험이 크지 않음(측정: 최대 1,405행, 안전 구간).
    "컴공학과": (
        "컴공",
        "컴퓨터공학과",
        "컴퓨터공학부",
        "인공지능공학부",
        "컴퓨터·인공지능공학부",
        "컴퓨터인공지능공학부",
    ),
}

STRICT_QUERY_TERMS = {
    "복수전공",
    "부전공",
    "마이크로전공",
    "전과",
    "학점",
    "수강신청",
    "휴학",
    "복학",
    "장학",
    "등록금",
    "성적",
    "교직",
    "현장실습",
}

DATASET_PRIORITIES = {
    "match_rule_documents": 1.00,
    "match_pknu_notice_documents": 0.70,
    "match_pknu_main_documents": 0.70,
    "match_rag_documents": 0.40,
    "match_pknu_syllabus_documents": 0.40,
}

# Query-time dataset routing. 특정 키워드가 질문에 있으면 관련 dataset의
# priority에 boost를 얹어서 상위로 끌어올림. 여러 규칙이 매칭되면 합산.
# 시연 필수 3개(Q1 졸업학점, Q2 졸업유예, Q3 캡스톤)에 회귀 없이 Q4(복수전공)
# 개선을 노림.
DATASET_BOOST_RULES: tuple[tuple[tuple[str, ...], Dict[str, float]], ...] = (
    (
        (
            "휴학", "복학", "결석", "출석", "재수강", "재이수", "전과",
            "졸업요건", "복수전공", "부전공", "학사경고", "학점",
            "학사학위취득유예", "졸업유예",
        ),
        {"match_rule_documents": 0.15, "match_pknu_main_documents": 0.10},
    ),
    (
        (
            "셔틀", "생활관", "기숙사", "도서관", "식당",
            "보건진료소", "연락처", "사무실", "전화",
        ),
        # "국립부경대학교 학칙" 같은 거대 허브 문서는 rule 데이터셋 전체에
        # 균일하게 걸리는 DATASET_PRIORITIES=1.00 덕에, 연락처류 질문과는
        # 무관한 조항이라도 다른 데이터셋의 훨씬 더 구체적인 문서를 상위
        # 랭킹에서 밀어내는 사례가 실측으로 확인됨(예: "컴퓨터·인공지능공학부
        # 사무실 연락처" 질문에서 학칙 조항이 1~2위, 실제 연락처 문서가 6위).
        # 위쪽 boost와 대칭으로 규정 데이터셋에는 페널티를 줘서 상쇄한다.
        {"match_pknu_main_documents": 0.15, "match_rule_documents": -0.35},
    ),
    (
        ("캡스톤", "학부 사무실", "학과 사무실"),
        {"match_rag_documents": 0.10, "match_rule_documents": -0.35},
    ),
)

# Query-time first-stage candidate pool widening. Reuses the same "department
# self-intro/contact" intent signal as DATASET_BOOST_RULES rule 3 above, but
# fixes a different layer of the bug: that rule only nudges *ranking* among
# candidates that already made the pool. It does nothing when the CE
# contact-directory chunk never enters the pool at all -- measured:
# first_stage_k=30 -> absent from top 30, first_stage_k=60 -> rank ~44,
# first_stage_k=80 -> rank #1 (combined with CONTACT_DIRECTORY_BONUS above).
# Compact (no-space) colloquial forms are included because real user queries
# look like "컴공학과사무실 전화번호" with no internal space, unlike the spaced
# "학과 사무실" the existing rule 3 was tuned against.
RAG_SELF_INTRO_TRIGGER_KEYWORDS = (
    "캡스톤",
    "학부 사무실", "학과 사무실",
    "학부사무실", "학과사무실", "과사무실",
)

# Empirically-verified-sufficient candidate pool size for match_rag_documents
# when the self-intro/contact trigger fires. A hardcoded module constant
# (same convention as CONTACT_DIRECTORY_BONUS below), not a Settings field --
# this number is coupled tightly to one specific rpc name + one specific
# trigger, not a general operational knob.
RAG_SELF_INTRO_FIRST_STAGE_K = 80


def _rag_self_intro_first_stage_overrides(
    query_text: Optional[str],
) -> Optional[Dict[str, int]]:
    """Per-RPC first_stage_k override when `query_text` looks like a
    department self-referential contact/office query, else None.

    Scoped narrowly on purpose: every trigger phrase requires "사무실"
    combined with a department-shaped prefix (학과/학부/과) or "캡스톤", so it
    does not fire on generic office/contact queries that have nothing to do
    with the rag dataset (e.g. "도서관 전화번호", "생활관 연락처" contain no
    학과/학부/과 + 사무실 combination and are correctly left alone -- those are
    handled by DATASET_BOOST_RULES rule 2 instead).
    """
    text = _normalize_text(query_text or "")
    if not text:
        return None
    if any(kw in text for kw in RAG_SELF_INTRO_TRIGGER_KEYWORDS):
        return {"match_rag_documents": RAG_SELF_INTRO_FIRST_STAGE_K}
    return None


SOURCE_KIND_PRIORITIES = {
    "post": 1.00,
    "page": 1.00,
    "html": 1.00,
    "attachment": 0.65,
    "archive_member": 0.55,
}

# Reciprocal Rank Fusion for hybrid (vector + lexical/BM25-style) retrieval.
# The embedding model discriminates formal Korean administrative text poorly:
# the one chunk that actually answers "복수전공 신청 조건" ranks ~160th by
# cosine similarity, well outside any vector top-K we could afford to fetch.
# A Postgres full-text ("simple" config) search on the same chunks catches
# exact-term matches the embedding misses. Lexical-only hits have no cosine
# similarity, so we approximate one from their BM25-ish rank via RRF and feed
# them into the existing candidate pool/scoring pipeline unchanged.
_RRF_K = 60
_LEXICAL_SIMILARITY_CEILING = 0.75


def _lexical_rrf_norm(rank_index: int) -> float:
    """0-based rank -> RRF weight, normalized against the best-possible (rank 0) value."""
    return (_RRF_K + 1) / (_RRF_K + rank_index + 1)


def _lexical_synthetic_similarity(rank_index: int, min_similarity: float) -> float:
    """Approximate a cosine-similarity-like score for a lexical-only hit.

    Deliberately capped below a genuine strong vector match (~0.85+) so a
    lexical hit can outrank weakly-matched vector noise without silently
    overriding rows the embedding model was actually confident about.
    """
    norm = _lexical_rrf_norm(rank_index)
    ceiling = max(min_similarity, _LEXICAL_SIMILARITY_CEILING)
    return min_similarity + norm * (ceiling - min_similarity)


# Bare words that are members of a QUERY_TERM_GROUPS entry but, measured
# directly against content_tsv, match thousands of rows in the largest chunk
# tables (rag_chunks/pknu_notice_chunks) on their own -- e.g. bare "졸업"
# alone needed 4.7-5.4s just to rank, right at (and sometimes past) the
# lexical RPC's statement timeout. They still need to stay in
# QUERY_TERM_GROUPS itself: other logic (_query_mismatch_flags /
# _dataset_mismatch_flags, e.g. the 졸업+학점 combo check) relies on the bare
# form to detect that a question is "about" that concept at all. Only the
# lexical OR-query builder below excludes them, since it's the one place a
# single overly-common token turns into a near-full-table scan.
LEXICAL_EXCLUDED_VARIANTS = {"졸업", "전공", "컴퓨터공학과", "컴퓨터공학부", "인공지능공학부"}

# Filler words that carry no retrieval signal but frequently show up in
# natural Korean questions. websearch_to_tsquery ANDs every remaining word
# together, so leaving these in silently zeroes out the match (the source
# chunk contains "학사관리과"/"증명서" but never literally "관련" next to it).
_LEXICAL_STOPWORDS = {
    "관련", "관련해서", "관련된", "관하여", "대해", "대해서", "대한",
    "알려줘", "알려주세요", "알려줄래요", "알려주실래요",
    "궁금해요", "궁금합니다", "궁금한데요", "궁금해",
    "어디에", "어디서", "어디", "어떻게", "무엇인가요", "뭐예요", "뭔가요",
    "있나요", "있어요", "있습니까", "됩니까", "되나요", "인가요", "인가",
    "하나요", "합니까", "하는지", "하는가요",
    "부탁드립니다", "부탁해요", "부탁드려요", "싶어요", "싶습니다",
}

# Korean particles attach directly to the noun with no space (문의처+를,
# 도서관+에), so a literal AND match needs them stripped first. Longest
# suffix first so e.g. "으로부터" doesn't get half-stripped to "로부터".
_LEXICAL_PARTICLE_SUFFIXES = tuple(
    sorted(
        {
            "으로부터", "에서부터", "에게서",
            "이라서", "라서", "이라도", "라도", "이라는", "라는", "이라고", "라고",
            "에서", "으로", "부터", "까지", "이나", "한테", "에게", "이랑", "랑",
            "와는", "과는",
            "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만", "나",
        },
        key=len,
        reverse=True,
    )
)

# Words that frame the question ("학사경고 *기준*", "*받으면* 어떻게 돼") rather
# than name the topic. They rarely appear verbatim next to the answer -- the
# regulation says "기준은"/"기준으로" -- so AND-ing them in zeroed out the
# lexical match for "학사경고 기준" while "학사경고" alone found the guideline.
# Fallback-only: title anchoring still sees them.
_LEXICAL_QUESTION_WORDS = {
    "기준", "방법", "절차", "조건", "결과", "내용", "정보", "경우", "각각",
    "받는", "받으면", "받나요", "하면", "되면", "돼", "돼요", "되는지", "뭐야", "뭐가",
}


def _lexical_fallback_query(query_text: str) -> str:
    """Best-effort AND-query for questions that hit no QUERY_TERM_GROUPS entry.

    This only ever *removes* filler tokens and particle suffixes from the
    existing AND-of-raw-sentence fallback -- it never switches to OR -- so it
    can't reintroduce the near-full-table-scan timeout risk that
    LEXICAL_EXCLUDED_VARIANTS guards against. It can only make an
    already-safe AND query match in more cases, by narrowing the token list
    down to the words actually likely to appear next to the answer.
    """
    # Exchange documents often repeat long parent titles and attachment names.
    # One explicitly named institution + academic year is a bounded document
    # identity; AND-ing all remaining inflected words can eliminate every hit.
    institutions = set(re.findall(r"[가-힣A-Za-z]+대학교", query_text))
    academic_year = re.search(r"(?<!\d)\d{4}학년도", query_text)
    if (len(institutions) == 1 and academic_year
            and re.search(r"교류|교환학생", query_text)):
        return next(iter(institutions)) + " " + academic_year.group()
    tokens: list[str] = []
    for raw_token in _normalize_text(query_text).split(" "):
        token = raw_token.strip("?!.,~()[]\"'")
        if not token or token in _LEXICAL_STOPWORDS:
            continue
        if not re.search(r"(?<!\d)\d{4}학년도$", token):
            for suffix in _LEXICAL_PARTICLE_SUFFIXES:
                if len(token) > len(suffix) + 1 and token.endswith(suffix):
                    token = token[: -len(suffix)]
                    break
        if len(token) >= 2:
            tokens.append(token)
    # Drop question-framing words only if something specific is left; a lone
    # "졸업" would turn the AND query into the near-full-table scan above.
    topic = [token for token in tokens if token not in _LEXICAL_QUESTION_WORDS]
    if any(token not in LEXICAL_EXCLUDED_VARIANTS for token in topic):
        tokens = topic
    return " ".join(dict.fromkeys(tokens))


def _lexical_query_text(
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
) -> str:
    """Build a websearch_to_tsquery-friendly OR-query from known term synonyms.

    Plain full-text search only matches literal tokens, but the same concept
    often shows up under a different word across datasets (question says
    "복수전공", the actual regulation says "다전공"). QUERY_TERM_GROUPS already
    encodes these synonym sets for the mismatch-penalty logic above, so reuse
    it here to OR all known variants together instead of AND-ing the raw
    question text, which would frequently match nothing at all.

    _query_term_groups() matches by substring, so a query like "복수전공" also
    matches the generic "전공" group ("전공" is a substring of "복수전공"). That
    is harmless for the penalty logic but disastrous here: OR-ing in a very
    common bare word like "전공" turns this into a near-full-table scan on the
    largest chunk tables and blows the statement timeout. Drop any matched
    term that is itself a substring of another, more specific matched term.
    """
    specific_terms = [
        (term, variants)
        for term, variants in query_terms
        if not any(term != other and term in other for other, _ in query_terms)
    ]
    if not specific_terms:
        return _lexical_fallback_query(query_text or "") or _normalize_text(query_text or "")
    parts: list[str] = []
    for _, variants in specific_terms:
        for variant in variants:
            variant = variant.strip()
            if not variant or variant in LEXICAL_EXCLUDED_VARIANTS:
                continue
            parts.append(f'"{variant}"' if " " in variant else variant)
    if not parts:
        return _lexical_fallback_query(query_text or "") or _normalize_text(query_text or "")
    return " OR ".join(dict.fromkeys(parts))


class Reranker(Protocol):
    def score(self, question: str, rows: List[Dict[str, Any]]) -> List[float]:
        ...


def build_supabase_client(url: str, key: str) -> Client:
    return create_client(url, key)


def _vector_literal(vec: List[float]) -> str:
    """pgvector accepts text literal '[v1,v2,...]'."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _similarity(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("similarity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _priority_score(row: Dict[str, Any]) -> float:
    metadata = row.get("metadata") or {}
    value = row.get("priority_score", metadata.get("priority_score"))
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dataset_priority(row: Dict[str, Any]) -> float:
    override = row.get("_boosted_dataset_priority")
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    metadata = row.get("metadata") or {}
    rpc_name = str(metadata.get("rpc_name") or "")
    return DATASET_PRIORITIES.get(rpc_name, 0.50)


def _dataset_priority_boost(rpc_name: str, query_text: Optional[str]) -> float:
    if not query_text:
        return 0.0
    text = _normalize_text(query_text)
    if not text:
        return 0.0
    boost = 0.0
    for keywords, boosts in DATASET_BOOST_RULES:
        if any(kw in text for kw in keywords):
            boost += boosts.get(rpc_name, 0.0)
    return boost


def _source_kind_priority(row: Dict[str, Any]) -> float:
    metadata = row.get("metadata") or {}
    source_kind = str(metadata.get("source_kind") or "").strip().lower()
    source_ext = str(metadata.get("source_ext") or "").strip().lower()
    if source_ext in {".html", ".htm", ".json"} and not source_kind:
        source_kind = "html"
    return SOURCE_KIND_PRIORITIES.get(source_kind, 0.70)


# A cross-encoder reranker trained on single-topic passage relevance tends to
# score a chunk that mentions one phone number in a clean, focused sentence
# higher than a chunk that is actually the authoritative directory/contact
# page for that number, but lists it among several other facts (other
# extensions, a fax number, an unrelated program's number) -- the dense
# chunk reads as "less about" the query even though it is the better source.
# Observed live: "컴공학과사무실 전화번호 알려줘" cited a notice that happened to
# mention one CE department number in passing, over the department's own
# contact-directory chunk (6 numbers: 3 office lines, a program line, 2 fax
# numbers) -- reranker preferred the single coincidental mention. Detect the
# directory shape directly (several distinct phone numbers in one chunk) and
# nudge it back up *after* reranking, so the fix doesn't get diluted by
# reranker_weight the way a priority_score change would (that only feeds the
# ~20% "initial_score" side of the blend, not the ~80% reranker side).
_PHONE_NUMBER_RE = re.compile(r"\d{2,4}[-–]\d{3,4}[-–]\d{4}")
CONTACT_DIRECTORY_MIN_PHONE_NUMBERS = 2
CONTACT_DIRECTORY_BONUS = 0.06


def _is_contact_directory_row(row: Dict[str, Any]) -> bool:
    content = str(row.get("content") or "")
    numbers = set(_PHONE_NUMBER_RE.findall(content))
    return len(numbers) >= CONTACT_DIRECTORY_MIN_PHONE_NUMBERS


def _query_mismatch_penalty(row: Dict[str, Any]) -> float:
    """Soft penalty for query-term mismatch flags (see _query_mismatch_flags).

    Previously these flags caused a hard drop, which could wipe out 90%+ of
    candidates for common terms (e.g. "복수전공") whenever no chunk repeated
    the exact keyword, even when the chunk was otherwise the best match
    available. A penalty lets better-matching rows win naturally while still
    surfacing something instead of "관련 정보를 찾을 수 없습니다" when nothing
    better exists.
    """
    flags = row.get("_query_mismatch_flags") or []
    penalty = 0.0
    for flag in flags:
        if flag.startswith("missing_strict_query_terms") or flag == "no_query_term_overlap":
            penalty = max(penalty, 0.35)
        elif flag == "missing_graduation_credit_terms":
            penalty = max(penalty, 0.25)
    return penalty


def _final_score(
    row: Dict[str, Any],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> float:
    bounded_weight = max(0.0, min(1.0, priority_weight))
    bounded_dataset_weight = max(0.0, min(1.0, dataset_priority_weight))
    bounded_source_kind_weight = max(0.0, min(1.0, source_kind_weight))
    semantic_weight = max(
        0.0,
        1.0 - bounded_weight - bounded_dataset_weight - bounded_source_kind_weight,
    )
    score = (
        _similarity(row) * semantic_weight
        + _priority_score(row) * bounded_weight
        + _dataset_priority(row) * bounded_dataset_weight
        + _source_kind_priority(row) * bounded_source_kind_weight
    )
    freshness = float(row.get("_freshness_adjustment") or 0.0)
    return max(0.0, score - _query_mismatch_penalty(row) + freshness)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").replace("\u00a0", " ").split()).strip()


def _title_for_row(row: Dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    return _normalize_text(
        metadata.get("doc_title")
        or metadata.get("source_file")
        or metadata.get("title")
        or row.get("title")
        or row.get("source_slug")
        or ""
    )


def _noise_flags(row: Dict[str, Any]) -> list[str]:
    text = _normalize_text(row.get("content") or row.get("text") or "")
    title = _title_for_row(row)
    if not text:
        return ["empty"]

    tokens = re.findall(r"[0-9A-Za-z\uac00-\ud7a3]+", text.lower())
    unique_tokens = set(tokens)
    hangul_count = len(re.findall(r"[\uac00-\ud7a3]", text))
    digit_count = len(re.findall(r"\d", text))
    dot_count = text.count(".") + text.count("·") + text.count("_")
    compact_len = max(1, len(re.sub(r"\s+", "", text)))
    lower_text = text.lower()
    flags: list[str] = []
    keyword_hits = sum(1 for keyword in ACADEMIC_KEYWORDS if keyword in text)

    if len(text) < 100 and len(unique_tokens) <= 6 and keyword_hits < 3:
        flags.append("metadata_stub")
    if dot_count / compact_len >= 0.20:
        flags.append("punctuation_heavy")
    if digit_count / compact_len >= 0.60 and hangul_count < 20:
        flags.append("numeric_heavy")
    if re.fullmatch(r"[\d\s.,:;~\-–—·ㆍ/()]+", text):
        flags.append("no_words")
    if any(phrase.lower() in lower_text for phrase in NOISE_PHRASES) and len(text) < 300:
        flags.append("template_or_form_noise")

    title_compact = re.sub(r"\s+", "", title)
    text_compact = re.sub(r"\s+", "", text)
    generic_titles = {re.sub(r"\s+", "", item) for item in GENERIC_STUB_TITLES}
    if title_compact in generic_titles and len(text) < 200:
        flags.append("generic_title_stub")
    if title_compact and text_compact and title_compact in text_compact:
        remainder = text_compact.replace(title_compact, "")
        if len(text) < 180 and len(remainder) <= 20:
            flags.append("title_only_stub")

    # Keep short chunks only when they contain enough concrete academic signal.
    if len(text) < 80 and keyword_hits < 2:
        flags.append("weak_short_chunk")

    # 반복 문자/자모 스팸 ("ㅇㅇㅇㅇㅇ", "!!!!!", "~~~~~")
    if _REPEATED_CHAR_RE.search(text):
        flags.append("repeated_char_spam")
    if _STANDALONE_JAMO_RE.search(text):
        flags.append("standalone_jamo_spam")

    return flags


def _is_noisy_row(row: Dict[str, Any]) -> bool:
    return bool(_noise_flags(row))


def _query_term_groups(query_text: Optional[str]) -> list[tuple[str, tuple[str, ...]]]:
    normalized = _normalize_text(query_text or "")
    if not normalized:
        return []
    return [
        (term, variants)
        for term, variants in QUERY_TERM_GROUPS.items()
        if any(variant in normalized for variant in variants)
    ]


def _query_mismatch_flags(
    row: Dict[str, Any],
    query_terms: list[tuple[str, tuple[str, ...]]],
) -> list[str]:
    if not query_terms:
        return []

    haystack = _normalize_text(f"{_title_for_row(row)} {row.get('content') or ''}")
    missing_strict = []
    matched = 0
    matched_terms: set[str] = set()
    for term, variants in query_terms:
        if any(variant in haystack for variant in variants):
            matched += 1
            matched_terms.add(term)
        elif term in STRICT_QUERY_TERMS:
            missing_strict.append(term)

    flags: list[str] = []
    if missing_strict:
        flags.append("missing_strict_query_terms:" + ",".join(missing_strict))
    if len(query_terms) >= 2 and matched == 0:
        flags.append("no_query_term_overlap")
    term_names = {term for term, _ in query_terms}
    if {"졸업", "학점"}.issubset(term_names) and not {"졸업", "학점"}.issubset(
        matched_terms
    ):
        flags.append("missing_graduation_credit_terms")
    return flags


def _dataset_mismatch_flags(
    row: Dict[str, Any],
    query_terms: list[tuple[str, tuple[str, ...]]],
) -> list[str]:
    term_names = {term for term, _ in query_terms}
    if not term_names:
        return []

    metadata = row.get("metadata") or {}
    rpc_name = str(metadata.get("rpc_name") or "")
    haystack = _normalize_text(f"{_title_for_row(row)} {row.get('content') or ''}")
    flags: list[str] = []

    if {"졸업", "학점"}.issubset(term_names):
        if not any(
            keyword in haystack
            for keyword in ("졸업요건", "졸업 소요학점", "소요학점", "교육과정 편성")
        ):
            flags.append("not_suitable_for_graduation_credit")

    return flags


def _row_uri(row: Dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    return (
        row.get("uri")
        or row.get("url")
        or metadata.get("doc_url")
        or metadata.get("source_page_url")
        or metadata.get("attachment_url")
        or ""
    )


def _attachment_source_identity(row: Dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    if metadata.get("source_kind") not in {"attachment", "archive_member"}:
        return ""
    if row.get("source_id"):
        return "source:" + str(row["source_id"])
    if metadata.get("source_path"):
        return "file:" + str(metadata["source_path"]).replace("\\", "/")
    return ""


def _explicit_attachment_candidates(rows: List[Dict[str, Any]], question: Optional[str]) -> List[Dict[str, Any]]:
    """A complete, distinctive attachment name identifies its document first."""
    text = re.sub(r"\s+", "", question or "").casefold()
    named = set()
    for row in rows:
        identity = _attachment_source_identity(row)
        metadata = row.get("metadata") or {}
        filename = str(metadata.get("source_file") or metadata.get("attachment_name") or "")
        stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", filename)
        stem = re.sub(r"\s+", "", stem).casefold()
        if identity and len(stem) >= 12 and stem in text:
            named.add(identity)
    return [row for row in rows if _attachment_source_identity(row) in named] if named else rows


def _url_key(row: Dict[str, Any]) -> str:
    """Per-URL cap 용 key. 쿼리스트링/앵커 제거해서 같은 문서 다른 뷰 통합.

    URL만으로 묶으면 안 된다 -- `www.pknu.ac.kr/main/434`처럼 서로 다른
    문서 여러 개가 같은 CMS 랜딩페이지 URL을 공유하는 경우가 실제로 있다
    (dedup 로직에서 이미 겪은 것과 동일한 문제). URL 하나로 묶으면, 그
    URL을 공유하는 무관한 문서가 이미 max_chunks_per_url 자리를 다
    채워버려서 정작 그 URL의 진짜 문서가 밀려나는 일이 생긴다 -- 오늘
    부서 연락처 디렉터리를 잘게 쪼갠 뒤 실제로 겪은 사례. 제목까지 묶어야
    "같은 문서의 여러 청크"만 캡 대상이 된다.
    """
    attachment = _attachment_source_identity(row)
    if attachment:
        return attachment
    uri = re.sub(r"[?#].*$", "", _normalize_text(_row_uri(row)).lower())
    title = _normalize_text(_title_for_row(row)).lower()
    if uri:
        return f"url:{uri}|title:{title}"
    return f"title:{title}"


def _dedupe_key(row: Dict[str, Any]) -> str:
    """정확히 같은 chunk 제거용 (같은 chunk가 여러 RPC에서 반환되는 경우 대비)."""
    attachment = _attachment_source_identity(row)
    if attachment:
        if row.get("chunk_index") is not None:
            return f"{attachment}|chunk:{row['chunk_index']}"
        if row.get("id"):
            return "chunk:" + str(row["id"])
    uri = _row_uri(row)
    title = _title_for_row(row)
    metadata = row.get("metadata") or {}
    content = str(row.get("content") or "")
    if content:
        digest = hashlib.sha1(
            _normalize_text(content)[:500].encode("utf-8", errors="ignore")
        ).hexdigest()
        return f"{uri}|{title}|{digest}"

    for key in ("id", "chunk_id", "document_id"):
        value = row.get(key) or metadata.get(key)
        if value:
            return f"{key}:{value}"
    return f"{uri}|{title}"


def _is_lexical_only(row: Dict[str, Any]) -> bool:
    return bool(row.get("_lexical_only"))


def _add_row(
    selected: List[Dict[str, Any]],
    seen: set[str],
    url_counts: Dict[str, int],
    row: Dict[str, Any],
    *,
    max_chunks_per_url: int,
    lexical_url_counts: Optional[Dict[str, int]] = None,
    max_lexical_chunks_per_url: Optional[int] = None,
) -> bool:
    key = _dedupe_key(row)
    if key in seen:
        return False
    url_key = _url_key(row)
    if url_counts.get(url_key, 0) >= max_chunks_per_url:
        return False
    if (
        lexical_url_counts is not None
        and max_lexical_chunks_per_url is not None
        and _is_lexical_only(row)
        and lexical_url_counts.get(url_key, 0) >= max_lexical_chunks_per_url
    ):
        return False
    seen.add(key)
    url_counts[url_key] = url_counts.get(url_key, 0) + 1
    if lexical_url_counts is not None and _is_lexical_only(row):
        lexical_url_counts[url_key] = lexical_url_counts.get(url_key, 0) + 1
    selected.append(row)
    return True


def _cap_per_url(
    rows: List[Dict[str, Any]],
    *,
    max_chunks_per_url: int,
    max_lexical_chunks_per_url: Optional[int] = None,
    trace: Optional[RetrievalTrace] = None,
) -> List[Dict[str, Any]]:
    """Preserve input order, keep at most `max_chunks_per_url` rows per URL.

    Lexical-only hits (no genuine vector match; similarity is a BM25-rank
    approximation, see _lexical_synthetic_similarity) get an extra, tighter
    per-URL allowance on top of the general cap. Without this, one broad
    document that happens to contain several query keywords (e.g. an
    all-topics "student life guide" ebook chunked into 2000+ pieces) can
    claim most of its URL's chunk slots via lexical coincidence alone,
    crowding out chunks from other, more specifically relevant documents.
    """
    counts: Dict[str, int] = {}
    lexical_counts: Dict[str, int] = {}
    kept: List[Dict[str, Any]] = []
    for row in rows:
        url_key = _url_key(row)
        if counts.get(url_key, 0) >= max_chunks_per_url:
            if trace is not None:
                trace.reject("selection_cap", row, "per_source_cap")
            continue
        if (
            max_lexical_chunks_per_url is not None
            and _is_lexical_only(row)
            and lexical_counts.get(url_key, 0) >= max_lexical_chunks_per_url
        ):
            if trace is not None:
                trace.reject("selection_cap", row, "lexical_per_source_cap")
            continue
        counts[url_key] = counts.get(url_key, 0) + 1
        if _is_lexical_only(row):
            lexical_counts[url_key] = lexical_counts.get(url_key, 0) + 1
        kept.append(row)
    return kept


def _rerank_candidates(
    *,
    question: str,
    candidates: List[Dict[str, Any]],
    top_k: int,
    max_chunks_per_url: int,
    reranker: Optional[Reranker],
    reranker_weight: float,
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
    max_lexical_chunks_per_url: Optional[int] = None,
    trace: Optional[RetrievalTrace] = None,
) -> List[Dict[str, Any]]:
    def finish(ordered):
        if trace is not None:
            trace.snapshot("rerank_ordered", ordered)
        capped = _cap_per_url(ordered, max_chunks_per_url=max_chunks_per_url,
                              max_lexical_chunks_per_url=max_lexical_chunks_per_url, trace=trace)
        if trace is not None:
            trace.snapshot("after_source_caps", capped, top_k=top_k)
        return capped[:top_k]

    if not candidates:
        if trace is not None:
            trace.reranker = {"status": "no_candidates"}
        return finish([])

    if not reranker or not question.strip():
        if trace is not None:
            trace.reranker = {"status": "disabled" if not reranker else "empty_question"}
        ordered = sorted(
            candidates,
            key=lambda row: _final_score(
                row,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            ),
            reverse=True,
        )
        return finish(ordered)

    try:
        raw_scores = reranker.score(question, candidates)
    except Exception as exc:
        logger.exception("retrieval: reranker failed, falling back to initial scores")
        if trace is not None:
            trace.reranker = {"status": "fallback", "error_type": type(exc).__name__}
        ordered = sorted(
            candidates,
            key=lambda row: _final_score(
                row,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            ),
            reverse=True,
        )
        return finish(ordered)

    normalized_scores = normalize_scores(raw_scores)
    if trace is not None:
        trace.reranker = {"status": "applied" if len(normalized_scores) == len(candidates) else "score_count_mismatch",
                          "input_count": len(candidates), "score_count": len(normalized_scores)}
    bounded_reranker_weight = max(0.0, min(1.0, reranker_weight))
    initial_weight = 1.0 - bounded_reranker_weight
    rescored: List[Dict[str, Any]] = []
    for row, rerank_score in zip(candidates, normalized_scores):
        copied = dict(row)
        initial_score = _final_score(
            copied,
            priority_weight,
            dataset_priority_weight,
            source_kind_weight,
        )
        rerank_final_score = (
            rerank_score * bounded_reranker_weight
            + initial_score * initial_weight
        )
        # Applied *after* the reranker blend, not folded into initial_score,
        # so it isn't diluted by (1 - reranker_weight) -- see
        # _is_contact_directory_row's docstring for why this lives here.
        if _is_contact_directory_row(copied):
            rerank_final_score += CONTACT_DIRECTORY_BONUS
        # Same idea for the freshness/graduate penalties: the reranker can't
        # tell a 2021 notice from this term's, so re-apply them on the
        # reranker side of the blend (see freshness.py).
        rerank_final_score += (
            float(copied.get("_freshness_adjustment") or 0.0) * bounded_reranker_weight
        )
        copied["rerank_score"] = rerank_score
        copied["rerank_final_score"] = rerank_final_score
        rescored.append(copied)

    rescored.sort(key=lambda row: float(row.get("rerank_final_score") or 0.0), reverse=True)
    return finish(rescored)


def _process_candidate_row(
    row: Dict[str, Any],
    rpc_name: str,
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> tuple[Optional[Dict[str, Any]], str]:
    """Shared per-row pipeline, used for both vector and lexical RPC rows.

    Returns (row, outcome). outcome is "noise" or "dataset_mismatch" when the
    row is dropped (row is None in that case), "query_mismatch" when it is
    kept but penalized, or "ok" otherwise.
    """
    copied = dict(row)
    metadata = dict(copied.get("metadata") or {})
    metadata.setdefault("rpc_name", rpc_name)
    copied["metadata"] = metadata

    if _noise_flags(copied):
        return None, "noise"

    query_flags = _query_mismatch_flags(copied, query_terms)
    if query_flags:
        copied["_query_mismatch_flags"] = query_flags

    if _dataset_mismatch_flags(copied, query_terms):
        return None, "dataset_mismatch"

    copied["_freshness_adjustment"] = freshness_adjustment(copied, rpc_name, query_text)
    base_dataset_priority = DATASET_PRIORITIES.get(rpc_name, 0.50)
    boost = _dataset_priority_boost(rpc_name, query_text)
    copied["_boosted_dataset_priority"] = max(0.0, min(1.0, base_dataset_priority + boost))
    copied["priority_score"] = _priority_score(copied)
    copied["dataset_priority"] = _dataset_priority(copied)
    copied["final_score"] = _final_score(
        copied, priority_weight, dataset_priority_weight, source_kind_weight
    )
    return copied, ("query_mismatch" if query_flags else "ok")


RpcResult = Tuple[Any, float, Optional[BaseException]]

# Concurrent RPCs run on one long-lived pool, and each worker thread talks to
# Supabase through its own client. Sharing the app's single client across
# threads broke whole requests at once (measured 2026-10-13: 9-16 of 192 calls
# failed with socket errors at 4-8 workers, all 8 RPCs of a query together);
# one client per worker failed 0 of 288. The pool is kept so those per-thread
# clients (and their open connections) are reused instead of rebuilt per query.
_rpc_pool: Optional[ThreadPoolExecutor] = None
_rpc_pool_size = 0
_rpc_pool_lock = threading.Lock()
_worker_clients = threading.local()


def _get_rpc_pool(size: int) -> ThreadPoolExecutor:
    global _rpc_pool, _rpc_pool_size
    with _rpc_pool_lock:
        if _rpc_pool is None or _rpc_pool_size != size:
            if _rpc_pool is not None:
                _rpc_pool.shutdown(wait=False)
            _rpc_pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="rag-rpc")
            _rpc_pool_size = size
        return _rpc_pool


def _worker_client(shared: Client) -> Client:
    """This worker thread's own client with the same URL/key as `shared`.
    Clients without those attributes (test fakes) are used as-is."""
    url = getattr(shared, "supabase_url", None)
    key = getattr(shared, "supabase_key", None)
    if not isinstance(url, str) or not isinstance(key, str):
        return shared
    clients = getattr(_worker_clients, "by_target", None)
    if clients is None:
        clients = _worker_clients.by_target = {}
    client = clients.get((url, key))
    if client is None:
        client = clients[(url, key)] = build_supabase_client(url, key)
    return client


def _fetch_rpc_responses(
    client: Client,
    rpc_names: List[str],
    base_payload: Dict[str, Any],
    candidate_counts: Dict[str, int],
    lexical_query: Optional[str],
    max_workers: int = 1,
    metadata_filters: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, RpcResult]:
    """Call every vector RPC (and its `<name>_lexical` companion when there is
    a lexical query), at most `max_workers` at a time; with 1 they run in the
    original order (vector, lexical, next vector, ...). Returns name ->
    (response, elapsed_ms, error); a failed call carries its exception instead
    of raising, so successful vector/lexical channels remain independent."""
    calls: Dict[str, Dict[str, Any]] = {}
    for rpc_name in rpc_names:
        calls[rpc_name] = {**base_payload, "match_count": candidate_counts[rpc_name]}
        if metadata_filters and rpc_name in metadata_filters:
            calls[rpc_name]["metadata_filter"] = metadata_filters[rpc_name]
        if lexical_query:
            lexical_count = candidate_counts[rpc_name]
            if rpc_name in FRESHNESS_RPCS:
                lexical_count = max(lexical_count, FRESHNESS_LEXICAL_FETCH)
            calls[f"{rpc_name}_lexical"] = {
                "query_text": lexical_query,
                "match_count": lexical_count,
            }
            # Use the scoped overload only when a filter exists. Older
            # unfiltered clients keep the original two-argument RPC contract.
            if calls[rpc_name].get("metadata_filter"):
                calls[f"{rpc_name}_lexical"]["metadata_filter"] = calls[rpc_name]["metadata_filter"]

    def run(name: str, use_worker_client: bool = False) -> RpcResult:
        started = time.perf_counter()
        try:
            # Client construction can fail too; keep that failure local to
            # this channel rather than raising from future.result().
            rpc_client = _worker_client(client) if use_worker_client else client
            response = rpc_client.rpc(name, calls[name]).execute()
            if not isinstance(response.data, list) or any(not isinstance(row, dict) for row in response.data):
                raise ValueError("invalid search RPC response")
            return response, (time.perf_counter() - started) * 1000, None
        except Exception as exc:  # reported by the caller, same as before
            return None, (time.perf_counter() - started) * 1000, exc

    if max_workers <= 1:
        return {name: run(name) for name in calls}
    pool = _get_rpc_pool(max_workers)
    futures = {
        name: pool.submit(run, name, True) for name in calls
    }
    return {name: future.result() for name, future in futures.items()}


def _opening_chunks_of_current_notices(
    client: Client,
    candidates: List[Dict[str, Any]],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> List[Dict[str, Any]]:
    """Opening chunks of the top current-term notices among `candidates`
    that aren't candidates already (see freshness.OPENING_SOURCES for why).

    Each extra row reuses its notice's candidate row (title, url, priority,
    similarity) with the chunk's own id/content swapped in, then goes
    through the normal per-row pipeline; the reranker decides whether it
    beats the chunk that brought the notice in. A failed fetch only logs.
    """
    parents: Dict[str, Dict[str, Any]] = {}
    ranked = sorted(
        candidates,
        key=lambda row: _final_score(row, priority_weight, dataset_priority_weight, source_kind_weight),
        reverse=True,
    )
    for row in ranked:
        rpc_name = str((row.get("metadata") or {}).get("rpc_name") or "")
        source_id = row.get("source_id")
        if rpc_name not in CHUNK_TABLES or not source_id or source_id in parents:
            continue
        if is_current_term_notice(row, query_text=query_text):
            parents[source_id] = row
            if len(parents) >= OPENING_SOURCES:
                break
    if not parents:
        return []

    present = {_dedupe_key(row) for row in candidates}
    extra: List[Dict[str, Any]] = []
    by_table: Dict[str, List[str]] = {}
    for source_id, parent in parents.items():
        table = CHUNK_TABLES[str(parent["metadata"]["rpc_name"])]
        by_table.setdefault(table, []).append(source_id)
    for table, source_ids in by_table.items():
        try:
            response = (
                client.table(table)
                .select("id, source_id, chunk_id, chunk_index, content, metadata")
                .in_("source_id", source_ids)
                .lt("chunk_index", OPENING_CHUNKS)
                .execute()
            )
        except Exception:
            logger.warning("retrieval: opening chunks fetch failed table=%s", table, exc_info=True)
            continue
        for chunk in response.data or []:
            parent = parents[chunk["source_id"]]
            rpc_name = str(parent["metadata"]["rpc_name"])
            row = {
                **parent,
                "id": chunk["id"],
                "chunk_id": chunk.get("chunk_id"),
                "chunk_index": chunk.get("chunk_index"),
                "content": chunk.get("content"),
                "metadata": {**(parent.get("metadata") or {}), **(chunk.get("metadata") or {})},
            }
            for key in ("_lexical_only", "_query_mismatch_flags", "rerank_score", "rerank_final_score"):
                row.pop(key, None)
            processed, outcome = _process_candidate_row(
                row, rpc_name, query_text, query_terms,
                priority_weight, dataset_priority_weight, source_kind_weight,
            )
            if processed is None or outcome in ("noise", "dataset_mismatch"):
                continue
            key = _dedupe_key(processed)
            if key in present:
                continue
            processed["similarity"] = _similarity(parent)
            processed["final_score"] = _final_score(
                processed, priority_weight, dataset_priority_weight, source_kind_weight
            )
            present.add(key)
            extra.append(processed)
    if extra:
        logger.info(
            "retrieval: added %d opening chunk(s) of %d current-term notice(s)",
            len(extra),
            len(parents),
        )
    return extra


# Source/chunk tables behind each notice RPC, for title-anchor lookups.
TITLE_ANCHOR_TABLES = {
    "match_rag_documents": ("rag_sources", "rag_chunks"),
    "match_pknu_notice_documents": ("pknu_notice_sources", "pknu_notice_chunks"),
}


def _fetch_notice_chunks(
    client: Client,
    source_table: str,
    chunk_table: str,
    source_ids: List[str],
    first_chunks: Optional[int] = None,
) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Source rows and their chunks (with embeddings; only the first
    `first_chunks` of each if given), or None on failure."""
    try:
        sources = (
            client.table(source_table)
            .select("id, source_slug, source_type, title, url, parent_url, metadata, priority_score, priority_details")
            .in_("id", source_ids)
            .execute()
        ).data or []
        query = (
            client.table(chunk_table)
            .select("id, source_id, chunk_id, chunk_index, content, metadata, embedding")
            .in_("source_id", source_ids)
        )
        if first_chunks is not None:
            query = query.lt("chunk_index", first_chunks)
        chunks = query.limit(400).execute().data or []
    except Exception:
        logger.warning("retrieval: notice chunk fetch failed table=%s", chunk_table, exc_info=True)
        return None
    return sources, chunks


def _notice_chunk_rows(
    rpc_name: str,
    sources: List[Dict[str, Any]],
    chunks: List[Dict[str, Any]],
    embedding: List[float],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> List[Dict[str, Any]]:
    """Chunks shaped like the vector RPC's rows (source columns + chunk, real
    cosine similarity), run through the normal per-row pipeline."""
    source_by_id = {source["id"]: source for source in sources}
    rows: List[Dict[str, Any]] = []
    for chunk in chunks:
        source = source_by_id.get(chunk.get("source_id"))
        vector = title_anchor.parse_vector(chunk.get("embedding"))
        if source is None or vector is None:
            continue
        row = {
            "id": chunk["id"],
            "source_id": chunk["source_id"],
            "source_slug": source.get("source_slug"),
            "source_type": source.get("source_type"),
            "title": source.get("title"),
            "url": source.get("url"),
            "parent_url": source.get("parent_url"),
            "chunk_id": chunk.get("chunk_id"),
            "chunk_index": chunk.get("chunk_index"),
            "content": chunk.get("content"),
            "metadata": {
                **(source.get("metadata") or {}),
                **(chunk.get("metadata") or {}),
                "priority_score": source.get("priority_score") or 0.0,
                "priority_details": source.get("priority_details") or {},
            },
            "similarity": title_anchor.cosine(embedding, vector),
        }
        processed, outcome = _process_candidate_row(
            row, rpc_name, query_text, query_terms,
            priority_weight, dataset_priority_weight, source_kind_weight,
        )
        if processed is None or outcome in ("noise", "dataset_mismatch"):
            continue
        rows.append(processed)
    return rows


def _title_anchor_chunks(
    client: Client,
    rpc_names: List[str],
    embedding: List[float],
    candidates: List[Dict[str, Any]],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
    max_workers: int = 1,
    metadata_filters: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Best chunks of the notices a rare query word names in their title.

    See title_anchor.py for why. Rows come out shaped like the vector RPC's
    (source columns + chunk, real cosine similarity) and go through the
    normal per-row pipeline; the reranker decides whether they're used. Any
    failed fetch only logs.
    """
    tables = {rpc: TITLE_ANCHOR_TABLES[rpc] for rpc in rpc_names if rpc in TITLE_ANCHOR_TABLES}
    terms = title_anchor.anchor_terms(query_text, _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES)
    if not tables or not terms:
        return []
    rpc_by_source = {source: rpc for rpc, (source, _chunks) in tables.items()}

    def fetch_titles(term: str, source_table: str, rpc_client: Client) -> List[Dict[str, Any]]:
        try:
            query = (
                rpc_client.table(source_table)
                .select("id, url, title, published_at:metadata->>published_at, date:metadata->>date")
                .eq("status", "active")
                .ilike("title", f"%{term}%")
                .limit(title_anchor.TERM_FETCH_LIMIT)
            )
            filters = _source_lookup_filters((metadata_filters or {}).get(rpc_by_source[source_table], {}))
            if filters:
                query = query.contains("metadata", filters)
            response = query.execute()
        except Exception:
            logger.warning("retrieval: title anchor lookup failed table=%s", source_table, exc_info=True)
            return []
        rpc_name = rpc_by_source[source_table]
        return [{**row, "_rpc_name": rpc_name} for row in response.data or []]

    jobs = [(term, source) for term in terms for source in rpc_by_source]
    if max_workers <= 1:
        results = [fetch_titles(term, source, client) for term, source in jobs]
    else:
        pool = _get_rpc_pool(max_workers)
        futures = [
            pool.submit(lambda t=term, s=source: fetch_titles(t, s, _worker_client(client)))
            for term, source in jobs
        ]
        results = [future.result() for future in futures]
    term_rows: Dict[str, List[Dict[str, Any]]] = {term: [] for term in terms}
    for (term, _source), rows in zip(jobs, results):
        term_rows[term].extend(rows)

    urls = title_anchor.rank_notices(term_rows)
    if not urls:
        return []
    source_ids: Dict[str, List[str]] = {}
    for rows in term_rows.values():
        for row in rows:
            if row.get("url") in urls:
                ids = source_ids.setdefault(row["_rpc_name"], [])
                if row["id"] not in ids:
                    ids.append(row["id"])

    present = {_dedupe_key(row) for row in candidates}
    extra: List[Dict[str, Any]] = []
    scored: Dict[str, List[Dict[str, Any]]] = {}
    for rpc_name, ids in source_ids.items():
        fetched = _fetch_notice_chunks(client, *tables[rpc_name], ids)
        if fetched is None:
            continue
        for row in _notice_chunk_rows(
            rpc_name, *fetched, embedding, query_text, query_terms,
            priority_weight, dataset_priority_weight, source_kind_weight,
        ):
            row["_title_anchor"] = True
            scored.setdefault(str(row.get("url") or ""), []).append(row)
    for url, quota in zip(urls, title_anchor.ANCHOR_CHUNKS_PER_NOTICE):
        rows = scored.get(url) or []
        if not rows:
            continue
        # Every chunk repeats the notice title in its header, so cosine
        # barely tells them apart (blank application forms often win); the
        # query's other words do. Cosine breaks ties.
        words = title_anchor.chunk_terms(
            query_text, str(rows[0].get("title") or ""), _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES
        )
        rows.sort(
            key=lambda row: (title_anchor.keyword_hits(row.get("content"), words), _similarity(row)),
            reverse=True,
        )
        taken = 0
        for row in rows:
            if taken >= quota:
                break
            key = _dedupe_key(row)
            if key in present:
                continue
            present.add(key)
            extra.append(row)
            taken += 1
    logger.info(
        "retrieval: title anchor terms=%s notices=%s added=%d",
        terms,
        [url[-40:] for url in urls],
        len(extra),
    )
    return extra


# Recurring notices are re-posted every term ("2025-1학기 본수강신청 안내",
# "2026학년도 2학기 휴·복학 안내"), and neither search channel can tell the
# terms apart: keyword search ranks hundreds of 수강신청 notices from 2020-2026
# by term frequency alone, so "2025년 1학기 수강신청 기간" never fetched the
# 2025-1 undergraduate notice (one board's title says "본수강신청", one token
# that doesn't even match "수강신청"), and "휴학 신청 기간" got the 군입대
# 휴학바구니 notice instead of 휴·복학 안내 (measured 2026-10-24). When the
# question names a term, or asks for a date (then: this term), titles carrying
# that term and a query word are looked up directly and their opening chunks
# join the reranker candidates. "이번 학기 ..." counts as naming this term.
TERM_TITLE_NOTICES = 8
TERM_TITLE_CHUNKS = 4
TERM_TITLE_FIRST_CHUNKS = 2
# Spellings of a query word that plain substring matching misses (titles
# and the academic calendar write "휴·복학").
_WORD_VARIANTS = {"휴학": ("휴학", "휴·복학", "휴ㆍ복학")}
_TERM_TITLE_SKIP_WORDS = frozenset({"작년", "재작년", "지난", "다음", "학기", "학년도"})


def _source_lookup_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    # Chunk-only fields cannot constrain source-table lookups.
    keys = {"ingestion_dataset", "department_id", "original_source_slug", "year", "term", "level", "staff_name", "course_no", "section"}
    return {key: value for key, value in filters.items() if key in keys}


def _term_title_patterns(term: int) -> str:
    """PostgREST or-filter matching titles that name `term` (term_index)."""
    year, semester = term // 2, term % 2 + 1
    patterns = (f"{year}-{semester}학기", f"{year}학년도*{semester}학기", f"{year}년*{semester}학기")
    return ",".join(f"title.ilike.*{pattern}*" for pattern in patterns)


def _term_title_chunks(
    client: Client,
    rpc_names: List[str],
    embedding: List[float],
    candidates: List[Dict[str, Any]],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
    metadata_filters: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Opening chunks of the asked term's notices whose title has a query word."""
    target = query_term(query_text)
    if target is None:
        # "이번 학기 캡스톤디자인 지원금" names no date but still means this
        # term's notice (2026-10-24: the title anchor picked a 1학기 one).
        if not (is_schedule_query(query_text) or says_this_term(query_text)):
            return []
        target = current_term()
    words = [
        word
        for word in title_anchor.anchor_terms(query_text, _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES)
        if not any(char.isdigit() for char in word) and word not in _TERM_TITLE_SKIP_WORDS
    ]
    if not words:
        return []
    graduate_asked = any(word in (query_text or "") for word in ("대학원", "석사", "박사"))
    patterns = _term_title_patterns(target)
    extra: List[Dict[str, Any]] = []
    present = {_dedupe_key(row) for row in candidates}
    scored: List[Tuple[int, float, Dict[str, Any]]] = []
    for rpc_name, (source_table, chunk_table) in TITLE_ANCHOR_TABLES.items():
        if rpc_name not in rpc_names:
            continue
        titles: Dict[str, Dict[str, Any]] = {}
        for word in words:
            for variant in _WORD_VARIANTS.get(word, (word,)):
                try:
                    query = (
                        client.table(source_table)
                        .select("id, url, title")
                        .eq("status", "active")
                        .ilike("title", f"%{variant}%")
                        .or_(patterns)
                        .limit(50)
                    )
                    filters = _source_lookup_filters((metadata_filters or {}).get(rpc_name, {}))
                    if filters:
                        query = query.contains("metadata", filters)
                    rows = query.execute().data or []
                except Exception:
                    logger.warning("retrieval: term title lookup failed table=%s", source_table, exc_info=True)
                    rows = []
                for row in rows:
                    titles.setdefault(row["id"], row)
        if not graduate_asked:
            titles = {key: row for key, row in titles.items() if "대학원" not in str(row.get("title") or "")}
        if not titles:
            continue

        def title_score(url: str) -> Tuple[int, int]:
            title = next(str(row.get("title") or "") for row in titles.values() if row["url"] == url)
            return (sum(1 for word in words if word in title), title_anchor.notice_number(url))

        urls = sorted({row["url"] for row in titles.values() if row.get("url")}, key=title_score, reverse=True)
        urls = urls[:TERM_TITLE_NOTICES]
        fetched = _fetch_notice_chunks(
            client, source_table, chunk_table,
            [key for key, row in titles.items() if row.get("url") in urls],
            first_chunks=TERM_TITLE_FIRST_CHUNKS,
        )
        if fetched is None:
            continue
        for row in _notice_chunk_rows(
            rpc_name, *fetched, embedding, query_text, query_terms,
            priority_weight, dataset_priority_weight, source_kind_weight,
        ):
            chunk_words = title_anchor.chunk_terms(
                query_text, str(row.get("title") or ""), _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES
            )
            scored.append((title_anchor.keyword_hits(row.get("content"), chunk_words), _similarity(row), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _hits, _similarity_score, row in scored:
        if len(extra) >= TERM_TITLE_CHUNKS:
            break
        key = _dedupe_key(row)
        if key in present:
            continue
        present.add(key)
        row["_term_title"] = True
        extra.append(row)
    logger.info("retrieval: term titles term=%d words=%s added=%d", target, words, len(extra))
    return extra


# The academic calendar (학사일정) answers schedule questions no notice title
# names: "2학기 기말고사 기간" found only a 보건진료소 "기말고사 응원전" notice,
# while "2026학년도 학사일정 안내" lists 제2학기 기말고사 12.14~12.20 (measured
# 2026-10-24). On schedule questions its chunks that contain a query word
# join the reranker candidates. It is posted once a year, so it is cached.
CALENDAR_CHUNKS = 2
_CALENDAR_CACHE_SECONDS = 3600.0
_calendar_cache: Dict[int, Tuple[float, Optional[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]]] = {}
# The calendar is one table whose column header ("학사내용 | 2026학년도(안) |
# 2025학년도") is only in its first chunk; later chunks list two date ranges
# per row with nothing saying which year is which.
_CALENDAR_HEADER_RE = re.compile(r"학사내용\s*\|[^|\n]+\|\s*20\d{2}학년도")


def _calendar_notice(
    client: Client, rpc_names: List[str], year: int
) -> Optional[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """(rpc_name, sources, chunks) of the newest "{year}학년도 학사일정" notice."""
    now = time.monotonic()
    cached = _calendar_cache.get(year)
    if cached and now - cached[0] < _CALENDAR_CACHE_SECONDS:
        return cached[1]
    found = None
    # The school board first: it is the issuing office's own post.
    for rpc_name in ("match_pknu_notice_documents", "match_rag_documents"):
        if rpc_name not in rpc_names:
            continue
        source_table, chunk_table = TITLE_ANCHOR_TABLES[rpc_name]
        try:
            rows = (
                client.table(source_table)
                .select("id, url, title")
                .eq("status", "active")
                .ilike("title", f"%{year}학년도%학사일정%")
                .limit(50)
                .execute()
            ).data or []
        except Exception:
            logger.warning("retrieval: calendar lookup failed table=%s", source_table, exc_info=True)
            return None
        rows = [row for row in rows if row.get("url") and "대학원" not in str(row.get("title") or "")]
        if not rows:
            continue
        url = max({row["url"] for row in rows}, key=title_anchor.notice_number)
        fetched = _fetch_notice_chunks(
            client, source_table, chunk_table, [row["id"] for row in rows if row["url"] == url]
        )
        if fetched is None:
            return None
        found = (rpc_name, *fetched)
        break
    _calendar_cache[year] = (now, found)
    return found


def _academic_calendar_chunks(
    client: Client,
    rpc_names: List[str],
    embedding: List[float],
    candidates: List[Dict[str, Any]],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> List[Dict[str, Any]]:
    """Calendar chunks naming what a schedule question asks about."""
    # Rare, specific words only ("기말고사", "수강취소"); "기간"/"신청" or a
    # term like "2학기" are on every chunk of the calendar.
    words = [
        variant
        for word in title_anchor.anchor_terms(query_text, _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES)
        if not any(char.isdigit() for char in word) and "학사일정" not in word
        for variant in _WORD_VARIANTS.get(word, (word,))
    ]
    if not words:
        return []
    target = query_term(query_text) or current_term()
    year, semester = target // 2, target % 2 + 1
    # A year's calendar runs to the next February, so the dates of a 1st
    # term's 예비·본 수강신청 (January-February) sit at the end of the
    # previous year's table: "2027학년도 1학기 수강신청" is in the 2026학년도
    # 학사일정 (measured 2026-10-26; Google answered it, we said 모름).
    years = (year, year - 1) if semester == 1 else (year,)
    term_labels = (f"{year}학년도 제{semester}학기", f"{year}학년도 {semester}학기")
    scored = []
    for calendar_year in years:
        notice = _calendar_notice(client, rpc_names, calendar_year)
        if notice is None:
            continue
        rows = _notice_chunk_rows(
            *notice, embedding, query_text, query_terms,
            priority_weight, dataset_priority_weight, source_kind_weight,
        )
        header = None
        for row in sorted(rows, key=lambda row: int(row.get("chunk_index") or 0)):
            match = _CALENDAR_HEADER_RE.search(str(row.get("content") or ""))
            if match:
                header = match.group(0)
                break
        for row in rows:
            content = str(row.get("content") or "")
            hits = title_anchor.keyword_hits(content, words)
            # Rows naming the asked term outrank the same event in other terms.
            if hits and any(label in content for label in term_labels):
                hits += 1
            scored.append((hits, _similarity(row), row, header))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    present = {_dedupe_key(row) for row in candidates}
    extra: List[Dict[str, Any]] = []
    for hits, _similarity_score, row, header in scored:
        if hits == 0 or len(extra) >= CALENDAR_CHUNKS:
            break
        key = _dedupe_key(row)
        if key in present:
            continue
        present.add(key)
        content = str(row.get("content") or "")
        if header and header not in content:
            row["content"] = content.replace("\n본문:", f"\n표 열: {header}\n본문:", 1)
        row["_academic_calendar"] = True
        extra.append(row)
    logger.info("retrieval: academic calendar years=%s words=%s added=%d", years, words, len(extra))
    return extra


# Chunks of the asked department pulled from each per-department table.
FOCUS_CHUNKS_PER_SOURCE = 2
FOCUS_MAX_SOURCES = 3


def _focus_department(
    client: Client,
    embedding: List[float],
    candidates: List[Dict[str, Any]],
    query_text: Optional[str],
    query_terms: list[tuple[str, tuple[str, ...]]],
    priority_weight: float,
    dataset_priority_weight: float,
    source_kind_weight: float,
) -> List[Dict[str, Any]]:
    """Swap other departments' table slices for the asked department's.

    See department_focus.py. Only notice tables are refetched (that is where
    the per-department attachments live); a failed fetch keeps the rest.
    """
    variants = department_focus.focus_variants(query_text)
    if not variants:
        return candidates
    kept: List[Dict[str, Any]] = []
    foreign: Dict[Tuple[str, str], None] = {}
    for row in candidates:
        if department_focus.is_other_departments_table(row.get("content"), variants):
            rpc_name = str((row.get("metadata") or {}).get("rpc_name") or "")
            if rpc_name in TITLE_ANCHOR_TABLES and row.get("source_id"):
                foreign[(rpc_name, str(row["source_id"]))] = None
            continue
        trimmed = department_focus.keep_own_rows(row.get("content"), variants)
        if trimmed is not None:
            row = {**row, "content": trimmed, "_department_trimmed": True}
        kept.append(row)
    # Keys of the original rows: trimming changed some contents (and keys).
    present = {_dedupe_key(row) for row in candidates}
    pulled = 0
    for rpc_name, source_id in list(foreign)[:FOCUS_MAX_SOURCES]:
        fetched = _fetch_notice_chunks(client, *TITLE_ANCHOR_TABLES[rpc_name], [source_id])
        if fetched is None:
            continue
        rows = [
            row for row in _notice_chunk_rows(
                rpc_name, *fetched, embedding, query_text, query_terms,
                priority_weight, dataset_priority_weight, source_kind_weight,
            )
            if department_focus.mentions(row.get("content"), variants)
        ]
        rows.sort(key=_similarity, reverse=True)
        for row in rows[:FOCUS_CHUNKS_PER_SOURCE]:
            key = _dedupe_key(row)
            if key in present:
                continue
            present.add(key)
            trimmed = department_focus.keep_own_rows(row.get("content"), variants)
            if trimmed is not None:
                row["content"] = trimmed
            row["_department_focus"] = True
            kept.append(row)
            pulled += 1
    logger.info(
        "retrieval: department focus variants=%s dropped=%d pulled=%d",
        variants[:2], len(candidates) - (len(kept) - pulled), pulled,
    )
    return kept


def search(
    client: Client,
    *,
    rpc_names: List[str],
    embedding: List[float],
    top_k: int,
    first_stage_k: int,
    per_rpc_first_stage_k: Optional[Dict[str, int]] = None,
    rpc_concurrency: int = 1,
    min_similarity: float,
    priority_weight: float = 0.30,
    dataset_priority_weight: float = 0.15,
    source_kind_weight: float = 0.10,
    reranker: Optional[Reranker] = None,
    reranker_weight: float = 0.80,
    max_chunks_per_url: int = 2,
    max_lexical_chunks_per_url: Optional[int] = None,
    query_text: Optional[str] = None,
    metadata_filter: Optional[Dict[str, Any]] = None,
    per_rpc_metadata_filters: Optional[Dict[str, Dict[str, Any]]] = None,
    title_anchor: bool = False,
    scope_check: Optional[Callable[[Dict[str, Any]], bool]] = None,
    capture_trace: bool = False,
) -> SearchResults:
    """Fan out across RPCs, merge rows, then return a diverse top_k set.

    Each RPC must accept the same (query_embedding, match_count, min_similarity,
    metadata_filter) signature and return rows with a `similarity` field. RPCs
    apply min_similarity in DB, so the merged set is already filtered. A failure
    in one channel is logged without dropping its successful companion.
    An empty result with channel failures raises SearchUnavailable instead
    of asserting that no matching evidence exists.
    """
    trace = RetrievalTrace() if capture_trace else None
    if not rpc_names:
        raise SearchUnavailable([{"rpc_name": None, "channel": "configuration", "status": "error",
                                  "result_count": None, "elapsed_ms": 0.0, "error_type": "NoSearchChannels"}],
                                trace=trace.export() if trace is not None else None)
    query_terms = _query_term_groups(query_text)
    lexical_query = _lexical_query_text(query_text, query_terms)
    candidate_count = max(first_stage_k, top_k)
    # When per_rpc_first_stage_k widens one specific RPC's fetch (see
    # RAG_SELF_INTRO_FIRST_STAGE_K), the final candidate-list caps below must
    # widen too, or the extra rows fetched from that RPC get truncated back
    # down to candidate_count before the reranker ever sees them.
    overall_candidate_count = candidate_count
    if per_rpc_first_stage_k:
        overall_candidate_count = max(
            candidate_count,
            *(max(value, top_k) for value in per_rpc_first_stage_k.values()),
        )
    payload = {
        "query_embedding": _vector_literal(embedding),
        "min_similarity": min_similarity,
        "metadata_filter": metadata_filter or {},
    }

    candidate_counts = {
        rpc_name: max((per_rpc_first_stage_k or {}).get(rpc_name, first_stage_k), top_k)
        for rpc_name in rpc_names
    }
    if trace is not None:
        trace.parameters = {"rpc_names": list(rpc_names), "candidate_counts": candidate_counts,
                            "lexical_query": lexical_query, "min_similarity": min_similarity,
                            "top_k": top_k, "max_chunks_per_url": max_chunks_per_url,
                            "max_lexical_chunks_per_url": max_lexical_chunks_per_url}
    # The vector and lexical RPCs are independent round trips; rpc_concurrency
    # controls how many run at once (1 = one after another, the default -- see
    # Settings.rag_rpc_concurrency for why). Ranking below is unchanged.
    fetched = _fetch_rpc_responses(
        client, rpc_names, payload, candidate_counts, lexical_query, max_workers=rpc_concurrency,
        metadata_filters=per_rpc_metadata_filters,
    )

    per_rpc_rows: List[Tuple[str, List[Dict[str, Any]]]] = []
    merged: List[Dict[str, Any]] = []
    channels: List[Dict[str, Any]] = []
    scope_rejected = 0
    for rpc_name in rpc_names:
        rpc_candidate_count = candidate_counts[rpc_name]
        response, elapsed_ms, error = fetched[rpc_name]
        channels.append({"rpc_name": rpc_name, "channel": "vector", "status": "error" if error else "ok",
                         "result_count": len(response.data) if error is None else None,
                         "elapsed_ms": round(elapsed_ms, 2), "error_type": type(error).__name__ if error else None})
        if error is not None:
            logger.warning("retrieval: vector rpc=%s failed; preserving other channels", rpc_name, exc_info=error)
        if trace is not None:
            trace.snapshot("vector_returned", response.data if error is None else [], rpc_name=rpc_name,
                           status="error" if error else "ok")

        rows: List[Dict[str, Any]] = []
        filtered_noise = 0
        penalized_query_mismatch = 0
        filtered_dataset_mismatch = 0
        for row in (response.data if error is None else []):
            if scope_check is not None and not scope_check(row):
                scope_rejected += 1
                if trace is not None:
                    trace.reject("initial_filter", row, "applicability", rpc_name)
                continue
            processed, outcome = _process_candidate_row(
                row,
                rpc_name,
                query_text,
                query_terms,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            )
            if outcome == "noise":
                filtered_noise += 1
                if trace is not None:
                    trace.reject("initial_filter", row, "noise", rpc_name)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "retrieval: filtered_noise rpc=%s title=%r sim=%.4f",
                        rpc_name,
                        _title_for_row(row)[:120],
                        _similarity(row),
                    )
                continue
            if outcome == "dataset_mismatch":
                filtered_dataset_mismatch += 1
                if trace is not None:
                    trace.reject("initial_filter", row, "dataset_mismatch", rpc_name)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "retrieval: filtered_dataset_mismatch rpc=%s title=%r sim=%.4f",
                        rpc_name,
                        _title_for_row(row)[:120],
                        _similarity(row),
                    )
                continue
            if outcome == "query_mismatch":
                penalized_query_mismatch += 1
            rows.append(processed)

        # ── Lexical (BM25-style full-text) channel ───────────────────────
        # Runs against the same table via `<rpc_name>_lexical` and is merged
        # by rank (RRF) rather than raw score, since ts_rank_cd isn't
        # comparable to cosine similarity. Only rows the vector search missed
        # entirely are added here; rows both channels found already made the
        # vector top-K on their own merit and keep their real similarity.
        lexical_added = 0
        lexical_boosted = 0
        if lexical_query:
            rows_by_key = {_dedupe_key(row): row for row in rows}
            vector_keys = set(rows_by_key)
            lexical_rpc_name = f"{rpc_name}_lexical"
            lexical_response, _lexical_ms, lexical_error = fetched[lexical_rpc_name]
            channels.append({"rpc_name": lexical_rpc_name, "channel": "lexical",
                             "status": "error" if lexical_error else "ok",
                             "result_count": len(lexical_response.data) if lexical_error is None else None,
                             "elapsed_ms": round(_lexical_ms, 2),
                             "error_type": type(lexical_error).__name__ if lexical_error else None})
            if lexical_error is not None:
                logger.warning(
                    "retrieval: lexical rpc=%s failed, skipping", lexical_rpc_name, exc_info=lexical_error
                )
                lexical_response = None
            if trace is not None:
                trace.snapshot("lexical_returned", lexical_response.data if lexical_response else [],
                               rpc_name=rpc_name, status="error" if lexical_error else "ok")

            rank_index = 0
            for row in (lexical_response.data if lexical_response else None) or []:
                if scope_check is not None and not scope_check(row):
                    scope_rejected += 1
                    if trace is not None:
                        trace.reject("initial_filter", row, "applicability", rpc_name)
                    continue
                processed, outcome = _process_candidate_row(
                    row,
                    rpc_name,
                    query_text,
                    query_terms,
                    priority_weight,
                    dataset_priority_weight,
                    source_kind_weight,
                )
                if outcome in ("noise", "dataset_mismatch") or processed is None:
                    if trace is not None:
                        trace.reject("initial_filter", row, outcome, rpc_name)
                    continue
                dedupe_key = _dedupe_key(processed)
                if dedupe_key in vector_keys:
                    # Vector search already found this chunk, but possibly at
                    # a mediocre similarity that buries a strong exact-term
                    # match (e.g. a short, keyword-dense chunk the embedding
                    # model doesn't score highly on its own). A high BM25
                    # rank here means the chunk deserves at least the score a
                    # lexical-only hit at that rank would get -- take
                    # whichever of the two signals is stronger instead of
                    # always deferring to the vector channel.
                    existing = rows_by_key[dedupe_key]
                    existing["_lexical_rank_index"] = rank_index
                    lexical_equivalent = _lexical_synthetic_similarity(
                        rank_index, min_similarity
                    )
                    if lexical_equivalent > _similarity(existing):
                        existing["similarity"] = lexical_equivalent
                        existing["priority_score"] = _priority_score(existing)
                        existing["dataset_priority"] = _dataset_priority(existing)
                        existing["final_score"] = _final_score(
                            existing, priority_weight, dataset_priority_weight, source_kind_weight
                        )
                        lexical_boosted += 1
                    rank_index += 1
                    continue
                processed["similarity"] = _lexical_synthetic_similarity(
                    rank_index, min_similarity
                )
                processed["_lexical_only"] = True
                processed["_lexical_rank_index"] = rank_index
                processed["priority_score"] = _priority_score(processed)
                processed["dataset_priority"] = _dataset_priority(processed)
                processed["final_score"] = _final_score(
                    processed, priority_weight, dataset_priority_weight, source_kind_weight
                )
                rows.append(processed)
                rows_by_key[dedupe_key] = processed
                vector_keys.add(dedupe_key)
                lexical_added += 1
                rank_index += 1

        rows.sort(
            key=lambda row: _final_score(
                row,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            ),
            reverse=True,
        )
        logger.info(
            "retrieval: rpc=%s match_count=%d rows=%d filtered_noise=%d penalized_query_mismatch=%d filtered_dataset_mismatch=%d lexical_added=%d lexical_boosted=%d latency_ms=%.1f top_similarity=%.4f top_priority=%.4f top_dataset_priority=%.2f top_source_kind_priority=%.2f top_final=%.4f priority_weight=%.2f dataset_priority_weight=%.2f source_kind_weight=%.2f",
            rpc_name,
            rpc_candidate_count,
            len(rows),
            filtered_noise,
            penalized_query_mismatch,
            filtered_dataset_mismatch,
            lexical_added,
            lexical_boosted,
            elapsed_ms,
            _similarity(rows[0]) if rows else 0.0,
            _priority_score(rows[0]) if rows else 0.0,
            _dataset_priority(rows[0]) if rows else 0.0,
            _source_kind_priority(rows[0]) if rows else 0.0,
            _final_score(
                rows[0],
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            )
            if rows
            else 0.0,
            priority_weight,
            dataset_priority_weight,
            source_kind_weight,
        )
        per_rpc_rows.append((rpc_name, rows))
        merged.extend(rows)
        if trace is not None:
            trace.snapshot("channel_merged", rows, rpc_name=rpc_name)

    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    url_counts: Dict[str, int] = {}
    lexical_url_counts: Dict[str, int] = {}

    # Keep one high-scoring row from each successful RPC before global ranking
    # (more for the notice tables on schedule questions -- see freshness.py).
    # This prevents one noisy collection from crowding out regulations/notices.
    for rpc_name, rows in per_rpc_rows:
        if len(candidates) >= overall_candidate_count:
            break
        wanted = seed_slots(rpc_name, query_text)
        taken = 0
        for row in rows:
            if taken >= wanted or len(candidates) >= overall_candidate_count:
                break
            if _add_row(
                candidates,
                seen,
                url_counts,
                row,
                max_chunks_per_url=max_chunks_per_url,
                lexical_url_counts=lexical_url_counts,
                max_lexical_chunks_per_url=max_lexical_chunks_per_url,
            ):
                taken += 1

    # Keep a bounded share of exact keyword hits until the reranker sees them.
    # Their synthetic similarity is capped at .75 and cannot otherwise compete
    # with many near-identical vector hits above .85, even at lexical rank 1.
    if reranker is not None:
        lexical_budget = min(max(1, overall_candidate_count // 3), overall_candidate_count - len(candidates))
        lexical_pool = sorted(
            ((row["_lexical_rank_index"], rpc_index, row)
             for rpc_index, (_, rows) in enumerate(per_rpc_rows)
             for row in rows if "_lexical_rank_index" in row),
            key=lambda item: item[:2],
        )
        for _, _, row in lexical_pool:
            if lexical_budget <= 0:
                break
            if _add_row(candidates, seen, url_counts, row, max_chunks_per_url=max_chunks_per_url,
                        lexical_url_counts=lexical_url_counts, max_lexical_chunks_per_url=max_lexical_chunks_per_url):
                lexical_budget -= 1

    merged.sort(
        key=lambda row: _final_score(
            row,
            priority_weight,
            dataset_priority_weight,
            source_kind_weight,
        ),
        reverse=True,
    )
    for row in merged:
        if len(candidates) >= overall_candidate_count:
            break
        _add_row(
            candidates,
            seen,
            url_counts,
            row,
            max_chunks_per_url=max_chunks_per_url,
            lexical_url_counts=lexical_url_counts,
            max_lexical_chunks_per_url=max_lexical_chunks_per_url,
        )

    if trace is not None:
        trace.snapshot("candidate_pool", candidates, candidate_limit=overall_candidate_count)

    if title_anchor:
        candidates.extend(
            _title_anchor_chunks(
                client,
                rpc_names,
                embedding,
                candidates,
                query_text,
                query_terms,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
                max_workers=rpc_concurrency,
                metadata_filters={name: (per_rpc_metadata_filters or {}).get(name, metadata_filter or {}) for name in rpc_names},
            )
        )

    candidates.extend(
        _term_title_chunks(
            client,
            rpc_names,
            embedding,
            candidates,
            query_text,
            query_terms,
            priority_weight,
            dataset_priority_weight,
            source_kind_weight,
            metadata_filters={name: (per_rpc_metadata_filters or {}).get(name, metadata_filter or {}) for name in rpc_names},
        )
    )

    if is_schedule_query(query_text):
        candidates.extend(
            _academic_calendar_chunks(
                client,
                rpc_names,
                embedding,
                candidates,
                query_text,
                query_terms,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            )
        )
        candidates.extend(
            _opening_chunks_of_current_notices(
                client,
                candidates,
                query_text,
                query_terms,
                priority_weight,
                dataset_priority_weight,
                source_kind_weight,
            )
        )

    if trace is not None:
        trace.snapshot("expanded_candidates", candidates)
    candidates = _focus_department(
        client,
        embedding,
        candidates,
        query_text,
        query_terms,
        priority_weight,
        dataset_priority_weight,
        source_kind_weight,
    )
    if trace is not None:
        trace.snapshot("department_focus", candidates)

    if scope_check is not None:
        before = len(candidates)
        kept = []
        for row in candidates:
            if scope_check(row):
                kept.append(row)
            elif trace is not None:
                trace.reject("expanded_filter", row, "applicability")
        candidates = kept
        logger.info("retrieval: applicability kept=%d rejected=%d", len(candidates), scope_rejected + before - len(candidates))

    if trace is not None:
        trace.snapshot("applicability_checked", candidates)
    candidates = _explicit_attachment_candidates(candidates, query_text)
    if trace is not None:
        trace.snapshot("rerank_input", candidates)
    selected = _rerank_candidates(
        question=query_text or "",
        candidates=candidates,
        top_k=top_k,
        max_chunks_per_url=max_chunks_per_url,
        reranker=reranker,
        reranker_weight=reranker_weight,
        priority_weight=priority_weight,
        dataset_priority_weight=dataset_priority_weight,
        source_kind_weight=source_kind_weight,
        max_lexical_chunks_per_url=max_lexical_chunks_per_url,
        **({"trace": trace} if trace is not None else {}),
    )
    if trace is not None:
        trace.snapshot("task_selected", selected)

    logger.info(
        "retrieval: overall_candidate_count=%d merged=%d candidates=%d trimmed=%d top_sim=%.3f top_priority=%.3f top_dataset_priority=%.2f top_source_kind_priority=%.2f top_final=%.3f top_rerank=%.3f priority_weight=%.2f dataset_priority_weight=%.2f source_kind_weight=%.2f reranker=%s",
        overall_candidate_count,
        len(merged),
        len(candidates),
        len(selected),
        _similarity(selected[0]) if selected else 0.0,
        _priority_score(selected[0]) if selected else 0.0,
        _dataset_priority(selected[0]) if selected else 0.0,
        _source_kind_priority(selected[0]) if selected else 0.0,
        _final_score(
            selected[0],
            priority_weight,
            dataset_priority_weight,
            source_kind_weight,
        )
        if selected
        else 0.0,
        float(selected[0].get("rerank_score") or 0.0) if selected else 0.0,
        priority_weight,
        dataset_priority_weight,
        source_kind_weight,
        bool(reranker),
    )
    if logger.isEnabledFor(logging.DEBUG):
        selected_summaries = []
        for index, row in enumerate(selected, start=1):
            metadata = row.get("metadata") or {}
            title = (
                metadata.get("doc_title")
                or metadata.get("source_file")
                or metadata.get("title")
                or row.get("title")
                or row.get("source_slug")
                or ""
            )
            selected_summaries.append(
                {
                    "index": index,
                    "rpc": metadata.get("rpc_name"),
                    "title": str(title)[:120],
                    "similarity": round(_similarity(row), 4),
                    "priority_score": round(_priority_score(row), 4),
                    "dataset_priority": round(_dataset_priority(row), 4),
                    "source_kind_priority": round(_source_kind_priority(row), 4),
                    "final_score": round(
                        _final_score(
                            row,
                            priority_weight,
                            dataset_priority_weight,
                            source_kind_weight,
                        ),
                        4,
                    ),
                    "rerank_score": round(float(row.get("rerank_score") or 0.0), 4),
                    "rerank_final_score": round(
                        float(row.get("rerank_final_score") or 0.0), 4
                    ),
                }
            )
        logger.debug("retrieval: selected=%s", selected_summaries)
    result = SearchResults(selected, channels=channels, trace=trace.export() if trace is not None else None)
    if result.degraded:
        logger.warning("retrieval: partial search failed_channels=%d selected=%d",
                       sum(c["status"] == "error" for c in channels), len(result))
        if not result:
            raise SearchUnavailable(channels, trace=result.trace)
    return result
