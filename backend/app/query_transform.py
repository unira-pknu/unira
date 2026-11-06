"""학생 질문(자연어)을 검색 친화 형태로 정규화하는 규칙 기반 변환 모듈.

파이프라인: normalize → strip_fillers → expand_abbreviations → expand_synonyms → normalize.
멱등성 보장 — transform_query(transform_query(x)) == transform_query(x).
FastAPI / DB 의존성 없음 — backend 와 scripts 양쪽에서 import 가능.

이 모듈의 책임 범위
--------------------
- 노이즈 토큰(인사·공손·1인칭 등 filler) 회피
- 학생 통용 약어를 부경대 공식 명칭으로 정규화 (토큰 수 증가 없는 대체)
- 좁고 명확한 동의어 보강 (실측 입증된 항목만)

이 모듈이 풀지 않는 문제
------------------------
- False confidence (토큰을 추가하면 유사도는 올라가지만 정확도와 무관해지는 현상):
  본질적 해결은 retrieval/rerank 단계 (BM25 hybrid, cross-encoder rerank, MMR,
  per-query 동적 threshold 등). 본 모듈은 "광범위 토큰을 추가하지 않는다"는
  소극적 원칙으로만 부분 기여.
- 데이터셋 라우팅 (out-of-scope):
  예) "외국인 한국어 강좌 신청" — 단발 공지가 아니라 국제교류부 상시 안내 영역
      "수업 빠지면 어떻게" — 학칙(rule 데이터셋) 영역
  이런 질문은 query 가공과 무관하게 공지사항 데이터셋에 매칭 가능한 chunk가
  본질적으로 없음. 의도 분류 또는 멀티 데이터셋 검색이 별도로 필요하며,
  본 모듈의 효과 측정에서는 제외해야 함.

한국어 단어 경계는 \\b 가 작동하지 않아 lookaround 정규식으로 처리한다.
조사 결합(예: "복전이", "복전을")은 1차에서는 매칭하지 않는다 — 의도적 보수.
"""

from __future__ import annotations

import re
from typing import Iterable

FILLER_PHRASES: tuple[str, ...] = (
    # 인사·공손 종결
    "안녕하세요", "안녕하십니까", "안녕", "감사합니다", "감사해요",
    # 공손체 / 부탁 어구
    "알 수 있을까요", "알려 주세요", "알려주세요", "알려줄래요", "알려줄 수",
    "자세하게", "자세히", "자세한", "구체적으로",
    # ~하려고 / ~싶다
    "하려고 하는데", "하려는데", "하고 싶은데", "하고 싶어요",
    # ~어떻게 종결
    "어떻게 해야 되나요", "어떻게 해야 하나요", "어떻게 되나요", "어떻게 하나요",
    # 1인칭 / 지칭
    "친구 한 명이", "친구 중에", "친구가",
    "제가", "저는", "저희", "내가", "나는",
    # 분리 어구
    "혹시", "좀",
)

# 약어 → 정식 명칭. 키가 사라지고 정식 명칭으로 대체된다 (멱등 보장 위해
# 값 안에 키가 단어 경계로 다시 매칭될 수 있는 항목은 SYNONYMS로 옮긴다).
# 런타임에 키 길이 내림차순 정렬 후 적용해서 "대연캠"이 "대연"보다 우선 매칭되도록 보장.
ABBREVIATIONS: dict[str, str] = {
    "복전": "복수전공",
    "부전": "부전공",
    "교직": "교직과정",
    "근장": "근로장학생",
    "현장실습": "현장실습학기제",
    "교환학": "교환학생",
    "졸유": "졸업유예",
    "조졸": "조기졸업",
    "국장": "국가장학금",
    "대연캠": "대연캠퍼스",
    "용당캠": "용당캠퍼스",
    "대연": "대연캠퍼스",
    "용당": "용당캠퍼스",
}

# 키가 텍스트에 나타나면 값 리스트 단어를 부족분만 append. 원본 단어는 보존.
# 동의어가 추가될 뿐이라 한 번 적용 후엔 모든 값이 텍스트에 존재 → 멱등.
#
# 분류 (baseline 측정 기반):
#   유지: Q7에서 효과 입증된 교환학생 동의어 군.
#   보류: 좁고 명확하나 미측정 — 데이터 확보 후 재평가.
#   삭제: 광범위 토큰("모집/신청/프로그램/변경/대출/수업/강좌/유학생") 추가로
#         Q2 회귀·Q3 false confidence 일으킨 항목 9개. 9개 항목 제거됨
#         (수업 빠, 비교과, 전과, 학자금, 장학금 신청, 복학, 휴학, 한국어, 외국인).
SYNONYMS: dict[str, tuple[str, ...]] = {
    "공결":      ("출석인정",),
    # 유지 — Q7 실측 입증
    "교환학생":   ("교환수학", "해외수학"),
    "교환":      ("교환학생",),
    # 보류: 미측정, 데이터 확보 후 재평가
    "결석":      ("결강", "출결"),
    "출석":      ("출결",),
    "결강":      ("결석",),
    "출결":      ("결석",),
    "휴강":      ("강의 휴업",),
    "재이수":    ("재수강",),
}

_HANGUL_OR_ALNUM = "가-힣A-Za-z0-9"
_MULTISPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")


def _normalize(text: str) -> str:
    if not text:
        return ""
    text = text.replace(" ", " ").strip()
    text = _MULTISPACE_RE.sub(" ", text)
    text = _TRAILING_PUNCT_RE.sub("", text)
    return text


def _strip_fillers(
    text: str, *, fillers: Iterable[str] = FILLER_PHRASES
) -> tuple[str, list[str]]:
    removed: list[str] = []
    # 긴 어구 먼저 — 짧은 어구의 부분 일치를 막는다.
    for phrase in sorted(fillers, key=len, reverse=True):
        if phrase and phrase in text:
            text = text.replace(phrase, " ")
            removed.append(phrase)
    return _normalize(text), removed


def _expand_abbreviations(
    text: str, *, table: dict[str, str] = ABBREVIATIONS
) -> tuple[str, list[tuple[str, str]]]:
    applied: list[tuple[str, str]] = []
    # 긴 약어 먼저 — "대연캠"이 "대연"보다 우선 매칭.
    for abbr in sorted(table, key=len, reverse=True):
        full = table[abbr]
        pattern = re.compile(
            f"(?<![{_HANGUL_OR_ALNUM}]){re.escape(abbr)}(?![{_HANGUL_OR_ALNUM}])"
        )
        new_text, n = pattern.subn(full, text)
        if n:
            text = new_text
            applied.append((abbr, full))
    return _normalize(text), applied


def _expand_synonyms(
    text: str, *, table: dict[str, tuple[str, ...]] = SYNONYMS
) -> tuple[str, list[tuple[str, tuple[str, ...]]]]:
    applied: list[tuple[str, tuple[str, ...]]] = []
    for key, extras in table.items():
        if key in text:
            missing = tuple(e for e in extras if e not in text)
            if missing:
                text = f"{text} {' '.join(missing)}"
                applied.append((key, missing))
    return _normalize(text), applied


def transform_query(question: str) -> str:
    """학생 질문 → 검색 친화 form. 멱등."""
    text = _normalize(question)
    text, _ = _strip_fillers(text)
    text, _ = _expand_abbreviations(text)
    text, _ = _expand_synonyms(text)
    return _normalize(text)


def transform_query_debug(question: str) -> dict:
    """transform_query와 동일 결과 + 각 단계 흔적을 반환 (튜닝 디버깅용)."""
    original = question
    after_norm = _normalize(question)
    after_strip, removed = _strip_fillers(after_norm)
    after_abbr, abbrs = _expand_abbreviations(after_strip)
    after_syn, syns = _expand_synonyms(after_abbr)
    final = _normalize(after_syn)
    return {
        "original": original,
        "after_normalize": after_norm,
        "after_strip_filler": after_strip,
        "after_expand_abbr": after_abbr,
        "after_expand_syn": after_syn,
        "final": final,
        "removed_fillers": removed,
        "applied_abbrs": abbrs,
        "applied_synonyms": syns,
    }


def _run_self_tests() -> int:
    cases: list[tuple[str, str]] = [
        # 회귀 방지 — 짧은 키워드는 그대로
        ("수강신청 기간", "수강신청 기간"),
        ("장학금", "장학금"),
        # filler 제거 (휴학 SYNONYMS 삭제 후 동의어 추가 없음)
        ("안녕하세요 휴학 절차 알려주세요", "휴학 절차"),
        # 약어 확장 + filler 제거
        ("복전 신청 어떻게 하나요", "복수전공 신청"),
        # 합성어 안의 약어는 매칭하지 않음 (단어 경계)
        ("교직이수 안내", "교직이수 안내"),
        # 띄어쓰기 있으면 매칭
        ("교직 이수 안내", "교직과정 이수 안내"),
        # 동의어 보강 (원본 보존 + extras append)
        ("교환학생 가는 방법", "교환학생 가는 방법 교환수학 해외수학"),
        # 캠퍼스 약어 — 긴 거 먼저 (대연캠 → 대연캠퍼스, 대연캠퍼스캠퍼스 X)
        ("대연캠 위치", "대연캠퍼스 위치"),
        ("용당캠 셔틀", "용당캠퍼스 셔틀"),
        # 짧은 캠퍼스 약어 — 단어 경계 있을 때만
        ("대연 본관", "대연캠퍼스 본관"),
        # 조사 결합은 1차에서 매칭 안 함 (의도적)
        ("대연에서 출발", "대연에서 출발"),
        # 종합: 1인칭 + 동의어 + 종결 어구
        ("저는 결석 처리 어떻게 되나요", "결석 처리 결강 출결"),
        # 복학 — 동의어 추가 없음 (회귀 방지 위해 SYNONYMS 삭제)
        ("복학 신청 기간", "복학 신청 기간"),
    ]
    fail = 0
    for inp, expected in cases:
        got = transform_query(inp)
        ok = got == expected
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {inp!r} -> {got!r}" + ("" if ok else f"  (expected {expected!r})"))
        if not ok:
            fail += 1
    # 멱등성: 한 번 더 통과시켜도 동일해야 함
    for inp, _ in cases:
        once = transform_query(inp)
        twice = transform_query(once)
        if once != twice:
            print(f"  [FAIL idempotency] {inp!r}: {once!r} -> {twice!r}")
            fail += 1
    print(f"\n{len(cases)} cases, {fail} failure(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_run_self_tests())
