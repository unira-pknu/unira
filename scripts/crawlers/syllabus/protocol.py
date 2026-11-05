"""The public EzReport form and Crownix HTML5 viewer's export protocol.

No browser automation or JavaScript evaluation is used. Parameter mode 3 is
the same reversible transport transform used by the published viewer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote_plus, unquote_to_bytes, urlsplit

REPORT_BASE = "https://reporting.pknu.ac.kr/ReportingServer"
EMBED_URL = REPORT_BASE + "/embed"
VIEWER_SOURCE = REPORT_BASE + "/html5/js/crownix-viewer.min.js"
REPORTS = {
    "kor": ("uni/cour/open/LtPdocMngtPrn.mrd", "U0253001"),
    "eng": ("uni/cour/open/LtPdocMngtPrn.mrd", "U0253002"),
    "tech": ("uni/cour/open/LtPdocTechMngtPrn.mrd", "U0253008"),
}


class ProtocolError(ValueError):
    """Unexpected report protocol; never treat an HTML/error body as a PDF."""


def compact_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def double_uri_encode(value: str) -> str:
    safe = "-_.!~*'()"
    return quote(quote(value, safe=safe), safe=safe)


def report_form(task: dict, session_id: str = "") -> dict[str, str]:
    path, document_type = REPORTS[task["variant"]]
    values = [task["course_no"], task["section"], task["staff_no"], document_type]
    # These values are copied from the public offering dataset, not arbitrary
    # SQL expressions. Refuse quotes/control characters in the tuple fields.
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)) for value in values):
        raise ProtocolError("학수번호·분반·교원번호에 지원하지 않는 문자가 있습니다.")
    params = {
        "YY": str(task["year"]), "SHTM_CD": task["term_code"],
        "IN_PARAM": "(" + ",".join("'" + value + "'" for value in values) + ")",
        "_CLIENT_USER_INFO": {},
        "_PROG_INFO": {
            "PGM_PATH": "uni::cour/open/LtPdocInq.xfdl", "MENU_ID": "U020913", "MENU_NM": "강의계획서조회",
        },
    }
    payload = {
        "reportPath": double_uri_encode(path),
        "reportParam": double_uri_encode(compact_json(params)),
        "reportOption": double_uri_encode('{"ds":"uni"}'),
        "beforeDraw": "", "afterDraw": "",
    }
    if session_id:
        payload["sessionId"] = session_id
    return {"report": compact_json(payload), "key": ""}


def decode_mode3(value: str) -> str:
    """JS unescape -> nibble swap -> untwist -> form/UTF-8 decode."""
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ProtocolError("보고서 파라미터의 percent escape 형식이 다릅니다.")
    raw = unquote_to_bytes(value)
    plain = []
    for index, byte in enumerate(raw):
        code = ((byte >> 4) & 0x0F) | ((byte << 4) & 0xF0)
        code -= (index + 1) % 95
        if code < 32:
            code += 95
        if not 32 <= code <= 126:
            raise ProtocolError("보고서 파라미터 변환 결과가 예상 범위를 벗어났습니다.")
        plain.append(chr(code))
    try:
        return unquote_plus("".join(plain), encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProtocolError("보고서 파라미터를 UTF-8로 읽을 수 없습니다.") from exc


@dataclass(frozen=True)
class ReportDocument:
    service_url: str
    path: str
    param: str

    def export_form(self, *, protocol: str = "sync") -> dict[str, str]:
        return {
            "opcode": "500", "mrd_path": self.path, "mrd_param": self.param,
            "export_type": "pdf", "protocol": protocol, "crownix-client": "html5viewer",
        }


def parse_embed(html: str, task: dict) -> ReportDocument:
    service = re.search(r"new\s+m2soft\.crownix\.Viewer\(\s*'([^']+)'", html)
    mode = re.search(r"\.setParameterEncrypt\(\s*['\"]?(\d+)['\"]?\s*\)", html)
    document = re.search(r"\.openFile\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*option\s*\)", html)
    if not service or not mode or not document or mode.group(1) != "3":
        raise ProtocolError("예상한 Crownix 보고서 페이지 또는 파라미터 모드 3이 아닙니다.")
    if service.group(1) != REPORT_BASE + "/service":
        raise ProtocolError("보고서 서비스 주소가 예상과 다릅니다.")
    path, param = (decode_mode3(value) for value in document.groups())
    expected = REPORTS[task["variant"]][0]
    if not path.replace("\\", "/").casefold().endswith(expected.casefold()):
        raise ProtocolError("요청한 강의계획서와 다른 보고서 서식입니다.")
    return ReportDocument(service.group(1), path, param)


def export_filename(body: str) -> str:
    parts = body.strip().split("|")
    if len(parts) != 2 or parts[0] != "1" or not parts[1]:
        raise ProtocolError("PDF 내보내기 성공 응답이 아닙니다: " + body[:200])
    filename = parts[1]
    parsed = urlsplit(filename)
    if (
        any(ord(char) < 32 for char in filename) or len(filename) > 512
        or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        or filename.startswith(("/", "\\")) or "\\" in filename
        or ".." in PurePosixPath(filename).parts or not filename.lower().endswith(".pdf")
    ):
        raise ProtocolError("예상하지 못한 PDF 파일 식별자입니다.")
    return filename
