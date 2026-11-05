"""Identity checks for actual Korean, English and engineering report layouts."""

from __future__ import annotations

import re

import fitz

from scripts.crawlers.syllabus.storage import normalize
from scripts.crawlers.syllabus.protocol import ProtocolError


def inspect_pdf(data: bytes, task: dict) -> dict:
    if not data.startswith(b"%PDF-"):
        raise ProtocolError("PDF 시그니처가 없습니다.")
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.is_encrypted or not document.page_count:
            raise ProtocolError("비어 있거나 암호화된 PDF입니다.")
        first_page = document[0].get_text()
        # Check identifiers in the report's first-page header, rather than
        # matching an unrelated course/year mentioned in weekly references.
        header = normalize(first_page[:1500])
        heading = normalize(first_page[:250])
        course = re.escape(normalize(task["course_no"]))
        section = re.escape(task["section"].lstrip("0") or "0")
        if not re.search(rf"(?:학수번호|coursenumber|coursecode)[:：]?{course}(?!\d)", header):
            raise ProtocolError("PDF 헤더의 학수번호가 요청과 다릅니다.")
        labeled_section = rf"(?:분반|classnumber|section(?:number|no\.?)?)[:：]?0*{section}(?!\d)"
        combined_section = rf"(?:학수번호|coursenumber)[:：]?{course}[-–—/]0*{section}(?!\d)"
        if not any(re.search(pattern, header) for pattern in (labeled_section, combined_section)):
            raise ProtocolError("PDF 헤더의 분반이 요청과 다릅니다.")
        if not re.search(rf"(?<!\d){task['year']}(?!\d)", heading):
            raise ProtocolError("PDF 헤더의 학년도가 요청과 다릅니다.")
        term_pattern = {
            "1": r"(?:1학기|1stsemester|firstsemester)",
            "2": r"(?:2학기|2ndsemester|secondsemester)",
            "summer": r"(?:여름|하계|summer)",
            "winter": r"(?:겨울|동계|winter)",
        }[task["term"]]
        if not re.search(term_pattern, heading):
            raise ProtocolError("PDF 헤더의 학기가 요청과 다릅니다.")
        variant_pattern = {
            "kor": "강의계획서", "eng": "coursesyllabus", "tech": "공학인증",
        }[task["variant"]]
        if variant_pattern not in header:
            raise ProtocolError("PDF의 국문·영문·공학인증 구분이 요청과 다릅니다.")
        return {
            "pages": document.page_count,
            "text_characters": sum(len(page.get_text()) for page in document),
            "verified_fields": ["course_no", "section", "year", "term", "variant"],
        }
