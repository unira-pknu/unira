"""Build a compact, reviewable crawler-planning JSON from saved main-page inventory.

This command is offline. It does not request pages or change crawler state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from scripts.crawlers.main.discovery.inventory_pages import DEFAULT_OUTPUT, PROJECT_ROOT


TYPE_KO = {
    "homepage": "학교 홈페이지", "static_page": "안내 페이지",
    "table_page": "표가 포함된 페이지", "board": "게시판",
    "link_hub": "연결 목록", "redirect": "다른 주소로 이동",
    "attachment_only": "첨부파일 중심 페이지",
    "embedded_content": "동적·삽입 콘텐츠 페이지",
    "image_only": "이미지 중심 페이지", "file_endpoint": "파일 응답",
    "unknown": "유형 미확인",
}

# A year in a one-off announcement is not evidence that the same URL updates.
RECURRING_TITLE = re.compile(r"^(?:대학통계|학사일정|등록금 안내|행사일정)$", re.I)
ARCHIVE_OR_TEST = re.compile(r"코로나19|COVID-19|테스트|New node", re.I)
MEMBERS_ONLY = re.compile(r"회원만 이용|로그인 후 이용|로그인이 필요", re.I)
CONTRIBUTION_TITLE = re.compile(
    r"자취하숙|자유게시판|열린장터|동아리|스터디|아르바이트|분실물|카풀|자원봉사|민원|신고|신문고|고객센터|소통채널|공유방|소통공간|단디센터|청렴퀴즈|교육수요자만족도|갤러리|FAQ", re.I)


def short(value: object, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def display_title(row: dict) -> str:
    if row["page_type"] == "homepage":
        return "국립부경대학교"
    title = short(row.get("title"), 100)
    labels = [short(label, 100) for label in row.get("menu_labels", []) if len(str(label).strip()) <= 100]
    if (not title or title in {"국립부경대학교", "이동 링크", "New node", "The document has been moved."}) and labels:
        return labels[0]
    return title or (labels[0] if labels else "제목 확인 불가")


def detail_examples(row: dict) -> list[str]:
    result = []
    for link in row.get("content_links", []):
        url = link["url"]
        query = parse_qs(urlsplit(url).query)
        if "no" in query and url not in result:
            result.append(url)
        if len(result) == 3:
            break
    return result


def description(row: dict, title: str, details: list[str]) -> str:
    status, kind = row["status"], row["page_type"]
    excerpt = short(row.get("content_summary"), 135)
    if status == "empty_response":
        return "HTTP 200이지만 응답 본문이 비어 있어 페이지 존재와 내용을 확인하지 못했습니다."
    if status == "access_blocked":
        return "접근 제한 안내가 반환되었습니다. 실제 권한 제한인지 폐기된 경로인지는 미확인입니다."
    if status not in {"exists", "link_only"}:
        return f"응답 확인 실패: {short(row.get('error') or row.get('evidence'), 100)}"
    if kind == "redirect":
        return f"{title}: {short(row.get('destination_url'), 140)}로 이동합니다. 목적지 내용은 미확인입니다."
    if kind == "file_endpoint":
        return "HTML이 아닌 파일 응답입니다. 파일 내용과 용도는 확인하지 않았습니다."
    if kind == "image_only":
        return f"{title}: 본문이 이미지 중심이며 이미지의 텍스트는 추출하지 않았습니다."
    if kind == "board":
        if details:
            first = next((short(link["text"], 75) for link in row["content_links"] if link["url"] == details[0]), "")
            return f"{title} 게시판입니다. 확인된 글 예시: {first or details[0]}"
        return f"{title} 게시판 구조는 확인됐지만 공개 상세 링크는 확인되지 않았습니다."
    if excerpt:
        return f"{title}: {excerpt}" if not excerpt.startswith(title) else excerpt
    if kind == "embedded_content":
        return f"{title}: 동적 영역이 있으나 현재 HTML에서 본문 내용을 확인하지 못했습니다."
    return f"{title}: {TYPE_KO.get(kind, kind)}로 확인됐으나 텍스트 요약은 없습니다."


def monitoring_decision(row: dict, title: str, details: list[str]) -> dict:
    status, kind = row["status"], row["page_type"]
    if status == "not_found":
        choice, reason = "no", "현재 주소에서 페이지가 없다는 응답이 확인됐습니다."
    elif status not in {"exists", "link_only"}:
        choice, reason = "review", "내용과 접근 가능성을 확인하기 전에는 주기적 수집 여부를 결정할 수 없습니다."
    elif kind == "redirect":
        if not row.get("destination_url") or "/main/null" in row["destination_url"]:
            choice, reason = "review", "목적지 URL이 유효한지 확인해야 합니다."
        else:
            choice, reason = "no", "이 경로는 이동 링크이므로 목적지의 수집 필요성을 별도로 판단해야 합니다."
    elif kind == "file_endpoint":
        choice, reason = "review", "파일의 내용과 갱신 여부를 확인하지 않았습니다."
    elif kind == "board":
        if MEMBERS_ONLY.search(row.get("content_summary", "")) or not details:
            choice, reason = "review", "게시판 구조는 있으나 공개 상세 글의 수집 가능성이 확인되지 않았습니다."
        elif ARCHIVE_OR_TEST.search(title):
            choice, reason = "review", "과거 자료 또는 시험용 게시판일 수 있어 운영 여부 확인이 필요합니다."
        elif CONTRIBUTION_TITLE.search(title):
            choice, reason = "review", "이용자 게시물·신고·이미지 중심 게시판일 수 있어 RAG 적재 범위와 개인정보 처리 여부를 먼저 확인해야 합니다."
        elif row.get("duplicate_of") is not None:
            choice, reason = "review", "다른 경로와 현재 목록 내용이 같아 대표 수집 경로를 먼저 정해야 합니다."
        else:
            choice, reason = "yes", "공개 게시글 상세 링크가 있어 새 글의 정기 확인이 유용할 가능성이 높습니다."
    elif kind == "homepage":
        choice, reason = "review", "메인 화면의 변경 정보가 다른 게시판과 중복되는지 확인해야 합니다."
    elif kind == "embedded_content" or "DYNAMIC_OR_EMBEDDED_CONTENT_NOT_FETCHED" in row.get("warnings", []):
        choice, reason = "review", "동적·삽입 데이터의 실제 출처와 갱신 빈도를 확인하지 않았습니다."
    elif RECURRING_TITLE.search(title):
        choice, reason = "yes", "같은 URL의 정기 갱신이 예상되는 안내 제목입니다. 실제 갱신 주기는 추후 확인해야 합니다."
    elif kind == "attachment_only" and "교육과정" in title:
        choice, reason = "review", "첨부 교육과정의 교체 주기와 최신성 확인이 필요합니다."
    else:
        choice, reason = "no", "이 한 차례 응답에서는 정기 갱신 근거가 확인되지 않았습니다. 필요하면 변경 감시 대상으로 지정할 수 있습니다."
    return {"decision": choice, "required": {"yes": True, "no": False, "review": None}[choice],
            "reason": reason, "basis": "현재 한 차례 응답에 근거한 자동 권고; 담당자 확정 전"}


def content_selector(row: dict) -> str | None:
    if row["status"] != "exists":
        return None
    if row["page_type"] == "homepage":
        return "#wrap"
    if row["page_id"] in (133, 134):
        return ".campusMap_conts"
    if row["page_id"] == 349:
        return "#rule_container"
    if row["page_id"] in (355, 356):
        return ".cont:has(.board-w)"
    return "#subCont"


def board_structure(row: dict, folder: Path) -> tuple[str | None, str | None]:
    if row["page_type"] != "board":
        return None, None
    raw_path = row.get("raw_path")
    raw = (folder / raw_path).read_text(encoding="utf-8") if raw_path and (folder / raw_path).is_file() else ""
    selector = next((selector for selector, pattern in (
        ("#tbl_contents", r'\bid=["\']tbl_contents["\']'),
        (".board-w", r'\bclass=["\'][^"\']*\bboard-w\b'),
        (".brdList", r'\bclass=["\'][^"\']*\bbrdList\b'),
        (".board_list", r'\bclass=["\'][^"\']*\bboard_list\b'),
        (".board-list", r'\bclass=["\'][^"\']*\bboard-list\b'),
    ) if re.search(pattern, raw, re.I)), None)
    hidden = next((tag for tag in re.findall(r'<input\b[^>]*>', raw, re.I)
                   if re.search(r'\bname\s*=\s*["\']?bbsId\b', tag, re.I)), "")
    found = re.search(r'\bvalue\s*=\s*["\']?([\w-]+)', hidden, re.I)
    return selector, found.group(1) if found else None


def strategy(row: dict) -> str:
    if row["status"] not in {"exists", "link_only"}:
        return "investigate_response"
    return {
        "board": "list_then_detail", "table_page": "html_table",
        "static_page": "single_html_page", "link_hub": "follow_curated_links",
        "attachment_only": "verify_then_extract_attachment",
        "image_only": "body_image_ocr",
        "embedded_content": "inspect_dynamic_source",
        "redirect": "assess_destination_separately",
        "file_endpoint": "verify_binary_endpoint",
        "homepage": "inspect_homepage_sections",
    }.get(row["page_type"], "inspect_page")


def catalog_page(row: dict, folder: Path = DEFAULT_OUTPUT) -> dict:
    title = display_title(row)
    details = detail_examples(row) if row["page_type"] == "board" else []
    board_list_selector, bbs_id = board_structure(row, folder)
    raw = row.get("raw_path")
    raw_source = (folder / raw).resolve() if raw else None
    try:
        raw_reference = raw_source.relative_to(PROJECT_ROOT).as_posix() if raw_source else None
    except ValueError:
        raw_reference = str(raw_source)
    return {
        "page_id": row["page_id"], "url": row["requested_url"],
        "title": title, "menu_labels": [short(label, 80) for label in row.get("menu_labels", []) if len(str(label).strip()) <= 80][:4],
        "description": description(row, title, details),
        "status": row["status"], "page_type": row["page_type"],
        "http_status": row.get("http_status"), "checked_at": row.get("checked_at"),
        "ongoing_crawl": monitoring_decision(row, title, details),
        "crawler_hints": {
            "strategy_candidate": strategy(row),
            "content_selector": content_selector(row),
            "list_selector_candidate": board_list_selector,
            "bbs_id_observed": bbs_id,
            "detail_url_examples": details,
            "detail_pages_verified": False,
            "pagination_verified": False,
            "destination_url": row.get("destination_url"),
            "iframe_urls": row.get("iframe_urls", [])[:3],
            "content_link_examples": [{"label": short(link["text"], 80), "url": link["url"]} for link in row.get("content_links", [])[:3]],
            "attachment_candidates": [{key: short(value, 100) if key == "text" else value for key, value in item.items()}
                                      for item in row.get("attachment_candidates", [])[:3]],
            "observed_table_count": row.get("table_count", 0),
            "observed_image_count": row.get("image_count", 0),
            "observed_link_count": len(row.get("content_links", [])),
            "raw_response_path": raw_reference,
            "duplicate_content_of_page_id": row.get("duplicate_of"),
            "warnings": row.get("warnings", []),
            "evidence": row.get("evidence", []),
        },
    }


def build_catalog(rows: list[dict], scan: dict, folder: Path = DEFAULT_OUTPUT) -> dict:
    pages = [catalog_page(row, folder) for row in sorted(rows, key=lambda item: item["page_id"])]
    if len({page["page_id"] for page in pages}) != len(pages):
        raise ValueError("Duplicate page IDs in the input inventory")
    if scan["complete"] and len(pages) != scan["requested_pages"]:
        raise ValueError("The input inventory is incomplete")
    return {
        "schema_version": 1,
        "site": "국립부경대학교",
        "scope": f"https://www.pknu.ac.kr/main/{scan['start_id']}–{scan['max_id']} 숫자 경로만 조사; 학교 전체 웹사이트 범위 아님",
        "generated_at": scan["generated_at"],
        "checked_at_range": {
            "first": min((row["checked_at"] for row in rows if row.get("checked_at")), default=None),
            "last": max((row["checked_at"] for row in rows if row.get("checked_at")), default=None),
        },
        "notes": ["설명과 수집 권고는 저장된 목록 페이지의 첫 응답만 근거로 합니다.",
                  "상세 글, 페이지네이션, 외부 이동 목적지, 첨부파일 내용, 동적 API는 확인하지 않았습니다.",
                  "ongoing_crawl.required의 null은 자동 판단을 보류한 review 상태입니다."],
        "counts": {
            "pages": len(pages),
            "status": dict(Counter(page["status"] for page in pages)),
            "page_type": dict(Counter(page["page_type"] for page in pages)),
            "ongoing_crawl_decision": dict(Counter(page["ongoing_crawl"]["decision"] for page in pages)),
        },
        "pages": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output", type=Path, help="Default: <input-dir>/page_catalog.json")
    args = parser.parse_args()
    folder = args.input_dir.resolve()
    rows = [json.loads(line) for line in (folder / "page_inventory.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    scan = json.loads((folder / "page_inventory.summary.json").read_text(encoding="utf-8"))
    catalog = build_catalog(rows, scan, folder)
    output = (args.output or folder / "page_catalog.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_suffix(output.suffix + ".part")
    part.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    part.replace(output)
    print(json.dumps({"output": str(output), "counts": catalog["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
