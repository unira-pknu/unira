"""Find the notice a question names by a rare word in its title.

Questions often name one specific notice by a word that almost only appears
in that notice's title: "정효택 장학금", "CES 2027 견학", "글로벌어학실습",
"지역인재 7급". Neither search channel catches these reliably:

- the embedding model doesn't know the proper noun, and
- the keyword channel ORs only QUERY_TERM_GROUPS synonyms once any group
  matches, so "정효택 장학금 ..." searches for "장학 OR 장학금" across
  thousands of chunks and the 정효택 notice never makes the candidate pool.

Measured 2026-10-19 on eval/google_compare: 0/8 attachment questions and 2/7
recent-notice questions answered, every miss a notice that was in the DB.

This module picks those rare title words and ranks notices by them; the
retrieval module fetches the chosen notices' best chunks into the reranker
candidates (see retrieval._title_anchor_chunks). The reranker still decides
whether they beat everything else, so a wrong pick costs latency, not answers.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Optional, Sequence

from .freshness import document_term

# Tokens whose title document frequency (distinct notice URLs across both
# notice tables) is above this are too common to point at one notice
# ("장학금" 521, "특강" 351, "학기" 2359). 지역인재 measured 69, kept.
MAX_TERM_DF = 80
# Fetch at most this many title matches per term and table; a term that
# fills it is over MAX_TERM_DF anyway.
TERM_FETCH_LIMIT = 200
# Notices whose chunks are pulled in, and best chunks per notice (by notice
# rank). Measured 2026-10-24 on the 9 google_compare misses: the fact chunk
# made the final two of its notice in 3/9 with 3 cosine-picked chunks, 8/9
# with 4 keyword-picked, 9/9 with 6. But (6, 3) cost the 100-case regression
# set 28 -> 25 correct (rule questions: more notice chunks crowd regulations
# out of the reranker pool), so the total stays at the old 6 and the second
# notice, usually a near miss, gets fewer.
ANCHOR_NOTICES = 2
ANCHOR_CHUNKS_PER_NOTICE = (4, 2)
# Total notice URLs, for the idf weight. Order of magnitude is all that matters.
_TOTAL_NOTICES = 10000

_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
_HANGUL_RE = re.compile(r"[가-힣]")
# Spoken verb/adjective endings ("받아", "있어", "하면", "얼마야", "가고"):
# such tokens are never what a notice title is named after, but short ones
# match a few titles by accident and would look rare.
_SPOKEN_ENDING_RE = re.compile(r"(?:야|요|어|아|해|면|고|까|니|지|죠|데|게|래|줘|냐|나|던데|는데)$")
_COLLOQUIAL = frozenset({
    "이번", "우리", "어디", "언제", "얼마", "누가", "무슨", "어떤", "같이", "아직",
    "최대", "이상", "이하", "있는", "하는", "되는", "받은", "관련", "정도", "혹시",
    "학부", "학과", "학교", "부경대", "부경대학교", "국립부경대학교",
})
# Administrative words: rare in titles, so they look like anchors, but they
# describe any notice. Measured: "신청자" (8 notices) outweighed "정효택" (29)
# and pulled a 2025 school-wide notice over this term's department one.
_GENERIC_ADMIN = frozenset({
    "신청", "신청자", "신청서", "신청인", "마감", "마감일", "결과", "제출", "서류", "변경",
    "사항", "기준", "추천", "사본", "대상", "대상자", "방법", "기간", "일정", "금액",
    "안내", "모집", "선발", "지원", "공지", "확인", "내용", "경우", "처리", "방안", "문의",
    "조건", "자격", "절차", "필요", "가능", "여부",
})


def anchor_terms(query_text: Optional[str], stopwords: Iterable[str], particle_suffixes: Sequence[str]) -> List[str]:
    """Candidate title words of `query_text`, in order, deduped.

    Particles are stripped the same way as the lexical fallback query; bare
    numbers ("2026") and single characters are dropped (years and terms are
    handled by freshness.py, not by title lookup).
    """
    return _query_tokens(query_text, set(stopwords) | _COLLOQUIAL | _GENERIC_ADMIN, particle_suffixes)


def chunk_terms(
    query_text: Optional[str], title: str, stopwords: Iterable[str], particle_suffixes: Sequence[str]
) -> List[str]:
    """Query words that tell one chunk of a notice from another.

    Unlike anchor_terms, administrative words stay: "자격", "선발", "금액"
    name nothing in a title but say which part of the notice is wanted.
    Words of the notice title are dropped, since every chunk repeats it in
    its header.
    """
    title_lower = (title or "").lower()
    terms = _query_tokens(query_text, set(stopwords) | _COLLOQUIAL, particle_suffixes)
    return [term for term in terms if term.lower() not in title_lower]


def keyword_hits(content: Optional[str], terms: Sequence[str]) -> int:
    """How many of `terms` the chunk body (header excluded) contains.

    A term of 3+ characters also matches without its last character, so a
    planner's "선발되는" -> "선발되" still finds "선발".
    """
    text = str(content or "")
    marker = text.find(_BODY_MARKER)
    body = (text[marker + len(_BODY_MARKER):] if marker >= 0 else text).lower()
    hits = 0
    for term in terms:
        term = term.lower()
        if term in body or (len(term) >= 3 and term[:-1] in body):
            hits += 1
    return hits


# Chunks are "<header lines>\n본문: <body>" (scripts/vectorization/vectorization.py).
_BODY_MARKER = "\n본문:"


def _query_tokens(query_text: Optional[str], stop: set, particle_suffixes: Sequence[str]) -> List[str]:
    terms: List[str] = []
    for raw in _TOKEN_SPLIT_RE.split(query_text or ""):
        token = raw.strip()
        if not token or token in stop:
            continue
        for suffix in particle_suffixes:
            if len(token) > len(suffix) + 1 and token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        if token.isdigit() or len(token) < 2 or token in stop:
            continue
        if _HANGUL_RE.search(token) and _SPOKEN_ENDING_RE.search(token):
            continue
        terms.append(token)
    return list(dict.fromkeys(terms))


def notice_number(url: str) -> int:
    """Board post number of a notice URL (newer posts are higher), 0 if none."""
    match = re.search(r"no=(\d+)", url or "")
    return int(match.group(1)) if match else 0


def rank_notices(term_rows: Dict[str, List[Dict[str, object]]]) -> List[str]:
    """URLs of the best-matching notices, best first, at most ANCHOR_NOTICES.

    `term_rows` maps each term to the source rows (needing "url" and
    "title", optionally "published_at"/"date") whose title contains it. A
    notice scores the idf of every rare term in its title; ties go to the
    newer post (higher board number), so "정효택 장학금" lands on this term's
    call for applicants rather than one of the 28 earlier ones.
    """
    title_by_url: Dict[str, str] = {}
    term_by_url: Dict[str, int] = {}
    weights: Dict[str, float] = {}
    for term, rows in term_rows.items():
        urls = {str(row.get("url") or "") for row in rows if row.get("url")}
        if not urls or len(urls) > MAX_TERM_DF or len(rows) >= TERM_FETCH_LIMIT:
            continue
        weights[term] = math.log(_TOTAL_NOTICES / len(urls))
        for row in rows:
            if row.get("url"):
                url = str(row["url"])
                title_by_url[url] = str(row.get("title") or "")
                term_by_url[url] = max(term_by_url.get(url, -1), _row_term(row))
    if not weights:
        return []

    scores: Dict[str, float] = {}
    for url, title in title_by_url.items():
        scores[url] = sum(weight for term, weight in weights.items() if term.lower() in title.lower())
    best = max(scores.values())
    # A notice must match the strongest evidence at least half as well as
    # the best one does; otherwise a stray common-ish word drags in noise.
    # Among those, the newest term wins: a question naming a recurring notice
    # ("정효택 장학금", 29 posts since 2021) means this term's post.
    ranked = sorted(
        (url for url, score in scores.items() if score >= best * 0.5),
        key=lambda url: (term_by_url[url], scores[url], notice_number(url)),
        reverse=True,
    )
    return ranked[:ANCHOR_NOTICES]


def _row_term(row: Dict[str, object]) -> int:
    """Semester of one source row, -1 if unknown.

    The posting date matters, not just the title: a yearless title
    ("전국 지역인재 7급 ... 변경사항 안내", posted 2026-10) otherwise lost to
    every "2026년 지역인재 7급 ..." notice, and "2027년 ... 동아리 모집"
    (posted 2026-05) read as a 2027 notice. A notice's attachment rows carry
    no date, so callers take the max over all rows of one URL.
    """
    metadata = {key: row.get(key) for key in ("published_at", "date") if row.get(key)}
    term = document_term({"title": row.get("title") or "", "metadata": metadata})
    return term if term is not None else -1


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def parse_vector(value: object) -> Optional[List[float]]:
    """pgvector comes back from PostgREST as the text '[0.1,0.2,...]'."""
    if isinstance(value, list):
        return [float(v) for v in value]
    if isinstance(value, str) and value.startswith("["):
        try:
            return [float(v) for v in value.strip("[]").split(",")]
        except ValueError:
            return None
    return None
