"""Adapter for ``/html/<section>/<page>.php`` menu and board sites."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import DepartmentCMSAdapter
from .numeric_cms import extract_body_content, _empty_content_info, _body_reference_links


_MENU_PATH = re.compile(r"^/html/([^/]+)/([^/]+\.php)$", re.IGNORECASE)
_NAV_NAMES = {"HOME", "LOGIN", "LOGOUT", "SITEMAP", "사이트맵", "로그인"}


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


class HtmlPHPAdapter(DepartmentCMSAdapter):
    name = "html_php"
    version = "1.1"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if detail:
            return soup.select_one(".board_read .readcontent, .readcontent")
        if urlsplit(page_url or base_url).hostname == "itc.pknu.ac.kr":
            photo = soup.select_one('img[src*="/050000_fix_board_professor/"]')
            if photo is not None:
                root = photo.find_parent(class_="container")
                if root is not None:
                    return root
        for selector in (".page_con", ".sub_contents", "#contents", "#content", "main", ".contents", ".content"):
            root = soup.select_one(selector)
            if root is not None:
                return root
        return None

    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]:
        soup, base = BeautifulSoup(html, "lxml"), urlsplit(base_url)
        found: dict[str, dict[str, str]] = {}
        for anchor in soup.select("a[href]"):
            name = anchor.get_text(" ", strip=True)
            absolute = urljoin(base_url, str(anchor.get("href") or ""))
            parsed, query = urlsplit(absolute), _query(absolute)
            match = _MENU_PATH.fullmatch(parsed.path)
            if parsed.netloc.lower() != base.netloc.lower() or not match or not name:
                continue
            if name.upper() in _NAV_NAMES or query.get("mode") or query.get("idx"):
                continue
            kept = {}
            category = str(query.get("category_idx", [""])[0])
            if category:
                kept["category_idx"] = category
            clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept), ""))
            found.setdefault(clean, {"name": name, "url": clean, "section": match.group(1),
                                     "page": match.group(2), "category_idx": category})
        return list(found.values())

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        parsed, effective_url = urlsplit(page_url), final_url or page_url
        effective, match = urlsplit(effective_url), _MENU_PATH.fullmatch(parsed.path)
        token = re.sub(r"[^a-zA-Z0-9]+", "_", parsed.path.strip("/")).strip("_")
        category_idx = str(_query(page_url).get("category_idx", [""])[0])
        if category_idx:
            token += f"_category_{category_idx}"
        common = {"id": f"html_{token}", "name": name or token, "category": "미분류",
                  "path": parsed.path + (f"?category_idx={category_idx}" if category_idx else ""),
                  "final_url": effective_url, "bbs_id": None}
        if not match or (effective.hostname and effective.hostname.lower() != (parsed.hostname or "").lower()):
            return {**common, "kind": "static_page", "document_type": "guide", "confidence": 1.0,
                    "status": "unsupported", "warnings": ["invalid path or external redirect"]}
        soup = BeautifulSoup(html, "lxml")
        read_links = [a for a in soup.select("a[href]") if
                      str(_query(urljoin(page_url, str(a.get("href") or ""))).get("mode", [""])[0]).lower() == "read"
                      and str(_query(urljoin(page_url, str(a.get("href") or ""))).get("idx", [""])[0]).isdigit()]
        board = bool(read_links or soup.select_one(".board_list, .board_wrap, .board-list"))
        static = self.select_body_root(soup, page_url=page_url) is not None
        return {**common, "kind": "board" if board else "static_page",
                "document_type": "notice" if board else "guide",
                "confidence": 0.95 if board else (0.65 if static else 0.2),
                "status": "candidate" if board or static else "unsupported", "warnings": []}

    def list_request(self, board_url: str, page: int, bbs_id: str | None = None) -> tuple[str, dict[str, Any]]:
        parsed, query = urlsplit(board_url), _query(board_url)
        params = {key: values[-1] for key, values in query.items() if key not in {"idx", "mode", "pagenum"}}
        params["pagenum"] = page
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), params

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in soup.select("a[href]"):
            url = urljoin(board_url, str(link.get("href") or ""))
            query = _query(url)
            if str(query.get("mode", [""])[0]).lower() != "read":
                continue
            idx = str(query.get("idx", [""])[0])
            if not idx.isdigit() or idx in seen:
                continue
            seen.add(idx)
            row = link.find_parent(["tr", "li", "article", "div"])
            text = row.get_text(" ", strip=True) if row else link.get_text(" ", strip=True)
            date_match = re.search(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", text)
            notice = "NOTICE" in text.upper() or "공지" in text[:20]
            result.append({"source_id": idx, "post_url": url, "num": "NOTICE" if notice else idx,
                           "post_no": int(idx), "is_notice": notice,
                           "date": date_match.group().replace(".", "-") if date_match else "",
                           "title": link.get_text(" ", strip=True)})
        return result

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if not content:
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in content.select("a[href]"):
            href = str(anchor.get("href") or "")
            lowered = href.lower()
            name = anchor.get_text(" ", strip=True)
            if not any(key in lowered for key in ("download", "file_down", "filedown", "mode=down", "attach")):
                if not re.search(r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip)$", name, re.I):
                    continue
            url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)
            result.append({"name": name or f"attachment-{len(result)+1}", "url": url})
        return result

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        wrapper = soup.select_one(".container.board_read, .board_wrap")
        body = soup.select_one(".container.board_read .readcontent, .board_read .readcontent, .readcontent")
        idx = str(item.get("source_id") or _query(post_url).get("idx", [""])[0])
        if not wrapper or body is None or not idx.isdigit():
            return None
        title_el = wrapper.select_one(".board_wrap_title, .board_title, .title, h3, h2")
        title = title_el.get_text(" ", strip=True) if title_el else str(item.get("title") or "")
        whole = wrapper.get_text(" ", strip=True)
        date_match = re.search(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", whole)
        clean = BeautifulSoup(str(body), "lxml")
        for selector in ("script", "style", ".share", ".sns", ".board_list", "nav"):
            for element in clean.select(selector):
                element.decompose()
        return {"title": title, "author": None,
                "date": date_match.group().replace(".", "-") if date_match else str(item.get("date") or ""),
                "url": post_url, "is_notice": bool(item.get("is_notice")), "source_id": idx,
                "body": re.sub(r"\s+", " ", clean.get_text(" ", strip=True)).strip(),
                "attachments": self.parse_attachments(wrapper, page_url=post_url, base_url=base_url,
                                                      site_prefix=site_prefix)}

    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "",
    ) -> dict[str, Any]:
        content = self.select_body_root(soup, page_url=page_url, base_url=base_url)
        clean = BeautifulSoup(str(content), "lxml") if content is not None else None
        if clean is not None:
            for tab in clean.select(".tab_ul3"):
                tab.decompose()
        title = clean.select_one(".cont_title, h1, h2, .page-title, .sub_title") if clean else None
        text = extract_body_content(clean, prefer_detail_body=False)
        attachments = self.parse_attachments(clean, page_url=page_url, base_url=base_url, site_prefix=site_prefix)
        references = _body_reference_links(clean, page_url or base_url)
        return {"title": title.get_text(" ", strip=True) if title else fallback_title,
                "content": text, "attachments": attachments,
                **({"reference_links": references} if references else {}),
                **_empty_content_info(clean, text, attachments)}
