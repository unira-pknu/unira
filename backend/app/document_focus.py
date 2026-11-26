"""Match named campus menus and preserve identifiable evidence within a document."""

from __future__ import annotations

import re


_CAMPUS_MENUS = (
    (r"시설.{0,14}(?:대관|빌리|빌릴)|(?:학교|대학).{0,8}시설.{0,8}(?:빌리|빌릴)", ("시설대관안내",)),
    (r"게시물\s*부착|포스터.{0,16}(?:붙|부착|검인)", ("게시물부착",)),
    (r"국제\s*학생증|\bISIC\b", ("국제학생증발급",)),
    (r"증명서.{0,18}(?:발급|뽑)|(?:무인|24시간).{0,12}발급기", ("제증명발급 안내",)),
    (r"교내.{0,15}주차|주차.{0,12}(?:정기권|요금)", ("주차요금",)),
    (r"백경이|뿌공이|교목|상징.{0,6}(?:나무|동물)", ("상징물",)),
    (r"역사\s*갤러리", ("부경대 역사갤러리",)),
    (r"도서관.{0,15}(?:소개|운영\s*시설|구성)", ("도서관",)),
    (r"체육진흥원", ("체육진흥원",)),
    (r"장애\s*학생\s*지원\s*센터", ("장애학생지원센터",)),
    (r"상대\s*평가|출석.{0,22}학점|학점.{0,22}출석", ("성적",)),
    (r"학적부|개명.{0,16}(?:이름|증빙|정정)", ("학적부기재사항",)),
    (r"사회\s*봉사|봉사.{0,10}(?:교양|학점)", ("사회봉사과목 이수안내",)),
    (r"재입학", ("재입학",)),
    (r"국가\s*장학금?\s*(?:[IⅠ1]\s*유형|제?1\s*유형)|국가\s*장학금?.{0,8}첫\s*학기", ("국가장학",)),
    (r"교환학생.{0,24}(?:평점|자격|몇\s*학기)", ("교환학생프로그램",)),
    (r"(?:국내|타\s*대학).{0,15}학점\s*교류.{0,16}(?:지원|이수)", ("타대학 이수학점 인정안내",)),
)
_DEPARTMENT_MENUS = (
    (r"연혁|정원|개명|이름.{0,10}바뀌|명칭.{0,8}변경|편제|개편|부임|신설|생겼",
     ("연혁", "학과연혁", "학과소개 및 입학관련 정보", "일반대학원 학과소개", "부경대학교 수산과학대학")),
    (r"사무실|(?:찾아)?오시는\s*길|(?:학과|전공|학부).{0,15}(?:전화|팩스|연락처)",
     ("오시는길", "오시는 길", "찾아오시는길", "찾아오시는 길", "찾아 오시는 길", "학과사무실", "전공사무실")),
    (r"학과\s*소개|전공\s*소개|학과\s*개요|정체성", ("학과 소개", "학과소개", "전공 소개", "전공소개", "학과개요")),
)
_FACILITIES = {
    "라일락": ("라일락", "미래관"),
    "다래락": ("다래락", "나비센터"),
    "들락날락": ("들락날락", "위드센터"),
    "한미락": ("한미락", "한미르관"),
    "라운지오": ("라운지오", "동원장보고관"),
}
_MEAL_HEADING = re.compile(r"(라일락|다래락|들락날락|한미락|라운지오)\s*\|?\s*구분\s*[/|]")
_MEAL_QUESTION = re.compile(r"식단|메뉴|식당|중식|석식|점심|저녁|정식")
_MEAL_WEEK = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*(?:\([월화수목금토일]\))?\s*[~～–-]\s*(\d{1,2})\s*\.\s*(\d{1,2})")
_ENROLLMENT_APP = re.compile(r"수강\s*신청\s*앱")
_PORTAL_APP = re.compile(r"포털\s*앱")
_MOBILE = re.compile(r"앱|모바일|(?:휴대|스마트|핸드)\s*폰|(?<![가-힣])폰")
_FACULTY_FAMILY_RULE_TITLE = re.compile(r"교수.*자녀.*강의\s*수강.*성적\s*평가.*공정성")
_EXPLICIT_RULE_VERSION = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:년|학년도|[.\-/]\s*\d)|개정판|시행일")


def _course_evaluation_navigation(question: str) -> bool:
    """Recognize general navigation while leaving dated notices to ordinary search."""
    return bool(
        re.search(r"(?:강의|수업)\s*평가", question)
        and (_MOBILE.search(question) or re.search(r"포털|메뉴|경로|어디|어떻|방법|하는\s*법", question))
        and not _ENROLLMENT_APP.search(question)
        and not re.search(r"20\d{2}\s*(?:학년도|년|[-/]\s*[12])|공지|공고|기간|마감|기한|언제", question)
    )


def course_evaluation_app_mismatch(question: str, content: str) -> bool:
    """An enrollment-app-only passage cannot answer a portal or mobile navigation request."""
    return bool(
        _ENROLLMENT_APP.search(content)
        and not _PORTAL_APP.search(content)
        and _MOBILE.search(question)
        and _course_evaluation_navigation(question)
    )


_ARTICLE = r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?"
_ARTICLE_HEADING = re.compile(_ARTICLE + r"\s*[(（]")
_ASKED_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*년(?:도)?(?:\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?)?")
_HISTORY_ROW = re.compile(r"(?<!\S)((?:19|20)\d{2})\s*[.년]\s*(\d{1,2})\s*[.월](?:\s*(\d{1,2})\s*[.일])?(?=\s|$)")


def rule_title_pattern(question: str) -> str:
    """Identify the named faculty-relative course rule before generic ranking."""
    if (_EXPLICIT_RULE_VERSION.search(question) or not re.search(r"자녀|부모", question)
            or not re.search(r"교수|부모의?\s*강의|부모님\s*강의", question)
            or not re.search(r"수강|필수\s*(?:교과)?과목|분반", question)):
        return ""
    return "%교수%자녀%강의%수강%성적%평가%공정성%"


def article_passage(content: str, question: str, *, title: str = "") -> str:
    """Keep the requested article and its definitions or conditions together."""
    requested = set(re.findall(_ARTICLE, question))
    if (not requested and _FACULTY_FAMILY_RULE_TITLE.search(title) and rule_title_pattern(question)
            and re.search(r"필수\s*(?:교과)?과목|다른\s*분반|타\s*분반", question)
            and not re.search(r"신고|예외|단일\s*분반|분반(?:이|은|도)?\s*(?:없|하나|한\s*개)", question)):
        requested = {("3", "")}
    if len(requested) != 1:
        return ""
    headings = list(_ARTICLE_HEADING.finditer(content))
    for index, heading in enumerate(headings):
        if tuple(value or "" for value in heading.groups()) in requested:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
            return content[heading.start():end].strip()
    return ""


def history_passage(content: str, question: str, title: str) -> str:
    """Bind names and facts to the dates at the start of chronology rows."""
    if "연혁" not in title and "역사갤러리" not in re.sub(r"\s+", "", title):
        return ""
    requested = _ASKED_DATE.findall(question)
    fields = [{value for value in values if value} for values in zip(*requested)]
    if not fields or any(len(values) > 1 for values in fields):
        return ""
    wanted = [int(next(iter(values))) if values else None for values in fields]
    dates = list(_HISTORY_ROW.finditer(content))
    passages = []
    for index, match in enumerate(dates):
        actual = [int(value) if value else None for value in match.groups()]
        if any(expected is not None and expected != value for expected, value in zip(wanted, actual)):
            continue
        end = dates[index + 1].start() if index + 1 < len(dates) else len(content)
        # The legacy flattening includes year-only headings before some rows.
        passages.append(re.sub(r"\s+(?:19|20)\d{2}\s*$", "", content[match.start():end]).strip())
    return "\n".join(dict.fromkeys(passages))


def tuition_row_key(question: str) -> str:
    """Identify one regular-fee row only when the request supplies every key."""
    if not re.search(r"등록금|입학금|수업료", question) or re.search(r"분할\s*납부|분납|회차|학과별", question):
        return ""
    if set(re.findall(r"[가-힣]{2,}대학교", question)) - {"부경대학교", "국립부경대학교"}:
        return ""
    parts = [
        set(re.findall(r"(?<!\d)(20\d{2})\s*(?:학년도|년|[-/])\s*([12])\s*(?:학기|(?=\s|$))", question)),
        set(re.findall(r"(?<![가-힣])(?:[가-힣]{2,12}대학원|학부)(?![가-힣])", question)),
        set(re.findall(r"(?<![가-힣])[가-힣]+계열(?:\s*\([가-힣·]+\))?", question)),
        set(re.findall(r"신입생|재학생", question)),
    ]
    if any(len(values) != 1 for values in parts):
        return ""
    (year, term), level, category, student = (next(iter(values)) for values in parts)
    category = re.sub(r"\s+", "", category)
    return f"{year}학년도 {term}학기 / {level} / {category} / 학생구분 {student}."


def tuition_passage(content: str, key: str) -> str:
    if not key:
        return ""
    row = re.escape("정규 등록금: " + key)
    amount = r"[\d,]+원"
    match = re.search(rf"{row}\s*입학금 {amount}, 수업료 {amount}, 총액 {amount}\."
                      r"(?:\s*공식 대조 상태: [a-z_]+\.)?", " ".join(content.split()))
    return match[0] if match else ""


def menu_titles(question: str, *, department: bool = False) -> list[str]:
    rules = _DEPARTMENT_MENUS if department else _CAMPUS_MENUS
    titles = [title for pattern, values in rules if re.search(pattern, question, re.IGNORECASE) for title in values]
    if not department and _course_evaluation_navigation(question):
        titles.append("강의평가")
    if not department and re.search(r"택시|도로명|주소|지하철|환승|출구|부산역", question):
        campuses = [campus for campus in ("대연", "용당") if re.search(rf"{campus}\s*캠", question)]
        if len(campuses) == 1:
            titles.append(f"{campuses[0]}캠퍼스")
    return list(dict.fromkeys(titles))


def meal_facility(question: str) -> str | None:
    if not _MEAL_QUESTION.search(question):
        return None
    names = [name for name, aliases in _FACILITIES.items() if any(alias in question for alias in aliases)]
    return names[0] if len(names) == 1 else None


def meal_passage(content: str, facility: str) -> str:
    """Keep a named cafeteria's header and rows together in legacy flattened HTML."""
    for heading in _MEAL_HEADING.finditer(content):
        if heading[1] != facility:
            continue
        next_heading = _MEAL_HEADING.search(content, heading.end())
        passage = content[heading.start():next_heading.start() if next_heading else None]
        if len(passage) <= 4000:
            return passage.strip()
    return ""


def meal_date_mismatch(question: str, title: str, published_at: str) -> bool:
    if "식단표" not in title or not _MEAL_QUESTION.search(question):
        return False
    week = _MEAL_WEEK.search(title)
    dates = {(int(m), int(d)) for m, d in re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", question)}
    if week is None or len(dates) != 1:
        return False
    target = next(iter(dates))
    first_month, first_day, last_month, last_day = map(int, week.groups())
    start, end = (first_month, first_day), (last_month, last_day)
    if start > end:  # A December–January week spans the year boundary.
        return not (target >= start or target <= end)
    if not start <= target <= end:
        return True
    years = set(re.findall(r"(?<!\d)(20\d{2})\s*년", question))
    published = re.match(r"(20\d{2})-(\d{2})-", published_at)
    # An early January menu can be posted in December of the previous year.
    return bool(len(years) == 1 and published and abs(int(published[2]) - target[0]) < 11
                and published[1] not in years)
