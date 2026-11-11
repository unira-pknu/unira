"""Questions asking for a site's address get the address written in the evidence."""

from app.evidence_context import select_evidence
from app.generation import build_user_message


STAFF = "교육연구팀 주무관 629-6718: 도서관 전산시스템 유지보수, 서버·홈페이지·모바일앱 운영 관리."
INTRO = "우리 도서관은 첨단 웹기반 학술정보시스템(http://libweb.pknu.ac.kr) 을 통하여 통합검색을 제공합니다."


def test_address_question_keeps_the_sentence_with_the_address():
    # 2026-11-11: the staff list ("홈페이지 유지보수") won and the libweb sentence was cut.
    content = "\n".join([STAFF] * 8 + [INTRO] + [STAFF] * 8)
    selected = select_evidence(content, question="중앙도서관 홈페이지 주소 알려줘", target_chars=300)
    assert "libweb.pknu.ac.kr" in selected


def test_source_page_url_is_labelled_as_the_page_not_the_site():
    rows = [{"title": "도서관", "url": "https://www.pknu.ac.kr/main/290", "content": INTRO, "metadata": {}}]
    message = build_user_message("도서관 홈페이지 주소", rows, 1000)
    assert "자료 페이지 URL: https://www.pknu.ac.kr/main/290" in message
    assert "\nURL: " not in message
    assert "본문에 적힌 그 사이트의 주소" in message
