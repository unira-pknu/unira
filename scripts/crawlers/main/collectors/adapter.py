"""HTML adapter for the university /main/<id> CMS, separate from department CMSs."""

from __future__ import annotations
from bs4 import BeautifulSoup, Tag
from scripts.crawlers.departments.adapters.base import DepartmentCMSAdapter
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
import re


MAIN_HOST = "www.pknu.ac.kr"
MAIN_PATH = re.compile(r"^/main/(\d+)$")
DETAIL_PATHS = {349: 350, 355: 357, 356: 357}


class MainListParserMismatch(ValueError):
    """Visible public detail links could not be parsed as list items."""


def _text(element: Tag | None) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip() if element else ""


def _page_id(url: str) -> int | None:
    match = MAIN_PATH.fullmatch(urlparse(url).path.rstrip("/"))
    return int(match.group(1)) if match else None


def _detail_url(href: str, board_url: str) -> tuple[str, int] | None:
    if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    target = urljoin(board_url, href)
    parsed = urlparse(target)
    board_id = _page_id(board_url)
    expected_id = DETAIL_PATHS.get(board_id, board_id)
    numbers = parse_qs(parsed.query).get("no", [])
    if (parsed.scheme not in {"http", "https"} or parsed.hostname != MAIN_HOST
            or _page_id(target) != expected_id or len(numbers) != 1
            or not numbers[0].isdigit()):
        return None
    return target, int(numbers[0])


def _list_root(soup: BeautifulSoup, page_id: int | None) -> Tag | None:
    if page_id == 349:
        return soup.select_one("#rule_container #tbl_contents")
    if page_id in {355, 356}:
        return soup.select_one(".cont .board-w")
    return soup.select_one("#subCont")


class PknuMainAdapter(DepartmentCMSAdapter):
    name = "pknu_main"
    version = "1.0"

    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]:
        # /main/<id> routes are supplied by the main-site page catalog.
        return []

    def count_list_candidates(self, soup: BeautifulSoup, board_url: str) -> int:
        root = _list_root(soup, _page_id(board_url))
        if root is None:
            return 0
        candidates = set()
        for anchor in root.select("a[href]"):
            resolved = _detail_url(anchor.get("href", ""), board_url)
            if resolved:
                candidates.add(resolved[0])
        return len(candidates)

    def analyze_section(self, *, name: str, page_url: str, html: str,
                        final_url: str | None = None) -> dict[str, Any]:
        soup = BeautifulSoup(html, "lxml")
        page_id = _page_id(page_url)
        candidate_count = self.count_list_candidates(soup, page_url) if page_id is not None else 0
        board_marker = bool(soup.select_one("#subCont .brdList, #rule_container #tbl_contents, .cont .board-w .board-list"))
        is_board = candidate_count > 0 or board_marker
        has_body = soup.select_one("#subCont") is not None
        supported = (urlparse(page_url).hostname == MAIN_HOST and page_id is not None
                     and (final_url is None or final_url == page_url))
        bbs_input = soup.select_one('input[name="bbsId"]')
        return {
            "id": f"main_{page_id}" if page_id is not None else "main_unknown",
            "name": name, "path": urlparse(page_url).path,
            "final_url": final_url or page_url,
            "kind": "board" if is_board else "static_page",
            "document_type": "notice" if is_board else "guide",
            "bbs_id": bbs_input.get("value") if is_board and bbs_input else None,
            "confidence": 0.95 if candidate_count else (0.8 if has_body else 0.3),
            "status": "candidate" if supported and (is_board or has_body) else "unsupported",
            "warnings": [] if supported else ["not a same-URL main-site HTML page"],
            "detail_link_candidates": candidate_count,
        }

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        page_id = _page_id(board_url)
        root = _list_root(soup, page_id)
        if root is None:
            return []
        if page_id == 349:
            rows = root.select("tbody tr")
            selector = "td.title a[href]"
        elif page_id in {355, 356}:
            rows = root.select("li:not(.board-th)")
            selector = ".col-subj a[href]"
        elif page_id == 193:
            rows = root.select(".list .item")
            selector = "a.tit[href]"
        elif root.select_one("ul.bdWz, ul.gryList"):
            rows = root.select("ul.bdWz > li, ul.gryList > li")
            selector = 'a[href*="action=view"]'
        else:
            rows = root.select("table.brdList tbody tr")
            selector = 'a[href*="action=view"]'
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            anchor = row.select_one(selector)
            resolved = _detail_url(anchor.get("href", ""), board_url) if anchor else None
            if resolved is None or resolved[0] in seen:
                continue
            seen.add(resolved[0])
            if page_id == 349:
                number = _text(row.select_one("td.no"))
                date = _text(row.select("td")[-2]) if len(row.select("td")) >= 2 else ""
            elif page_id in {355, 356}:
                number = _text(row.select_one(".col-num"))
                date = _text(row.select_one(".col-date")).removeprefix("작성일").strip()
            elif page_id == 193:
                number = str(resolved[1])
                date = _text(row.select_one(".date"))
            elif row.select_one("ul.bdWz, h5"):
                number = ""
                dates = re.findall(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", _text(row))
                date = dates[-1].replace(".", "-").replace("/", "-") if dates else ""
            else:
                number = _text(row.select_one(".bdlNum"))
                date = _text(row.select_one(".bdlDate"))
            title = _text(row.select_one("h5")) if row.select_one("h5") else _text(anchor)
            items.append({"post_url": resolved[0], "post_no": resolved[1],
                          "num": number, "is_notice": number.upper() == "NOTICE",
                          "date": date, "title": title})
        if not items and self.count_list_candidates(soup, board_url):
            raise MainListParserMismatch(f"detail links present but no list items parsed: {board_url}")
        return items

    def parse_attachments(self, content: Tag | None, *, page_url: str,
                          base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if content is None:
            return []
        attachments = []
        seen = set()
        for anchor in content.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            absolute = urljoin(page_url, href)
            path = urlparse(absolute).path.lower()
            if not ("boarddownload.do" in path or path.endswith((".pdf", ".hwp", ".hwpx", ".docx", ".xlsx", ".zip"))):
                continue
            if absolute not in seen and urlparse(absolute).scheme in {"http", "https"}:
                seen.add(absolute)
                attachments.append({"name": _text(anchor) or path.rsplit("/", 1)[-1], "url": absolute})
        return attachments

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any],
                     *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        board_url = str(item.get("post_url") or post_url)
        requested = _detail_url(board_url, board_url)
        actual = urlparse(post_url)
        if (requested is None or actual.hostname != MAIN_HOST
                or _page_id(post_url) != _page_id(requested[0])
                or parse_qs(actual.query).get("no") != [str(requested[1])]):
            return None
        expected_bbs = parse_qs(urlparse(board_url).query).get("bbsId")
        if expected_bbs and parse_qs(actual.query).get("bbsId") != expected_bbs:
            return None
        page_id = _page_id(post_url)
        title = author = body = date = ""
        content: Tag | None = None
        if page_id == 350:
            content = soup.select_one("#rule_container #rule_view_contents #tbl_views")
            if content is None:
                return None
            for row in content.select("tr"):
                header = _text(row.select_one("th"))
                value = row.select_one("td")
                if header == "제목": title = _text(value)
                elif header == "작성일": date = _text(value)
                elif header == "작성자": author = _text(value)
                elif not header and row.select_one('td[colspan="2"]'):
                    body = _text(row.select_one('td[colspan="2"]'))
        elif page_id == 357:
            content = soup.select_one(".cont .board-w .board-write")
            if content is None:
                return None
            title = _text(content.select_one(".subj dd"))
            body = _text(content.select_one(".bd_textbox"))
        else:
            content = soup.select_one("#subCont .bdCont")
            if content is None:
                return None
            title = _text(content.select_one("tr.first_noti td.title_b"))
            body = _text(content.select_one(".bdvTxt"))
            for row in content.select("tr.noti"):
                cells = row.select("td")
                if cells and _text(cells[0]) == "작성일" and len(cells) > 1:
                    date = _text(cells[1])
                    break
        attachments = self.parse_attachments(content, page_url=post_url, base_url=base_url,
                                             site_prefix=site_prefix)
        if not title or (not body and not attachments):
            return None
        return {"title": title, "author": author, "date": date or item.get("date", ""),
                "url": post_url, "body": body, "attachments": attachments,
                "is_notice": bool(item.get("is_notice")),
                "content_status": "content" if body else "attachment_only"}

    def parse_static(self, soup: BeautifulSoup, *, fallback_title: str) -> dict[str, Any]:
        from scripts.crawlers.main.collectors.static_pages import parse_static_page_html

        if soup.select_one('#subCont .brdList, #subCont .list .item a[href*="action=view"], '
                           '#subCont ul.bdWz a[href*="action=view"], '
                           '#subCont ul.gryList a[href*="action=view"]'):
            return {"title": fallback_title, "content": "", "attachments": [],
                    "status": "needs_review", "reason": "board_list_not_static"}
        parsed = parse_static_page_html(str(soup), 0)
        return {"title": parsed.title if parsed.title != "main-0" else fallback_title,
                "content": parsed.content, "attachments": [], "status": parsed.status,
                "reason": parsed.reason, "warnings": list(parsed.warnings)}
