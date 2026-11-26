"""Adapter for query-menu sites using ``mcode``/``menucode`` and ``mode``/``no``."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import DepartmentCMSAdapter
from .numeric_cms import extract_body_content, _empty_content_info


_NAVIGATION_NAMES = {"HOME", "SITEMAP", "LOGIN", "LOGOUT", "사이트맵", "이메일무단수집금지"}


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _menu_code(url: str) -> str:
    query = _query(url)
    return str((query.get("mcode") or query.get("menucode") or [""])[0])


class QueryMcodeAdapter(DepartmentCMSAdapter):
    """URL-pattern adapter; no dataset name or host is hard-coded."""

    name = "query_mcode"
    version = "1.1"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if detail:
            return soup.select_one(".boardReadBody .boardContents")
        for selector in ("#contents.page-cont", ".page-cont", "#contents", "main", "#container-wrap"):
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
            code = str((query.get("mcode") or query.get("menucode") or [""])[0])
            if parsed.netloc.lower() != base.netloc.lower() or not code:
                continue
            if query.get("no") or str(query.get("mode", [""])[0]) == "2":
                continue
            if name.strip().upper() in _NAVIGATION_NAMES:
                continue
            canonical = urlunsplit((parsed.scheme, parsed.netloc, "/", urlencode({"mcode": code}), ""))
            found.setdefault(code, {"name": name or f"menu-{code}", "url": canonical, "mcode": code})
        return list(found.values())

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        code, requested = _menu_code(page_url), urlsplit(page_url)
        effective_url, effective = final_url or page_url, urlsplit(final_url or page_url)
        common = {"id": f"mcode_{code}", "name": name or f"menu-{code}", "category": "미분류",
                  "path": f"/?mcode={code}", "final_url": effective_url, "bbs_id": None}
        if effective.hostname and effective.hostname.lower() != (requested.hostname or "").lower():
            return {**common, "kind": "static_page", "document_type": "guide", "confidence": 1.0,
                    "status": "unsupported", "warnings": [f"external redirect: {effective_url}"]}
        soup = BeautifulSoup(html, "lxml")
        board = bool(soup.select_one("table.boardList, .boardRead, .boardNavigation .pagination"))
        static = bool(soup.select_one("#contents, #container-wrap, .page-cont, main"))
        return {**common, "kind": "board" if board else "static_page",
                "document_type": "notice" if board else "guide",
                "confidence": 0.95 if board else (0.7 if static else 0.2),
                "status": "candidate" if board or static else "unsupported", "warnings": []}

    def list_request(self, board_url: str, page: int, bbs_id: str | None = None) -> tuple[str, dict[str, Any]]:
        parsed, code = urlsplit(board_url), _menu_code(board_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/index.Pknu", "", "")), {
            "menucode": code, "mode": "1", "page": page,
        }

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in soup.select("table.boardList tbody tr, table.boardList tr"):
            link = row.select_one("td.title a[href], a[href*='mode=2'][href*='no=']")
            if not link:
                continue
            href = urljoin(board_url, str(link.get("href") or ""))
            item_id = str(_query(href).get("no", [""])[0])
            if not item_id.isdigit() or item_id in seen:
                continue
            seen.add(item_id)
            number = row.select_one("td.num")
            number_text = number.get_text(" ", strip=True) if number else ""
            notice = bool(number and number.select_one("img")) or "공지" in number_text
            date_el = row.select_one("td.date")
            items.append({"source_id": item_id, "post_url": href, "num": "NOTICE" if notice else number_text,
                          "post_no": int(item_id), "is_notice": notice,
                          "date": date_el.get_text(" ", strip=True) if date_el else "",
                          "title": link.get_text(" ", strip=True)})
        return items

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if not content:
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in content.select(".fileinfo a[href], a[href*='download.asp']"):
            url = urljoin(page_url, str(anchor.get("href") or ""))
            if not url or url in seen:
                continue
            seen.add(url)
            name = anchor.get_text(" ", strip=True)
            image = anchor.select_one("img[alt]")
            result.append({"name": name or (str(image.get("alt")) if image else "") or f"attachment-{len(result)+1}",
                           "url": url})
        return result

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        wrapper = soup.select_one(".boardRead")
        title = soup.select_one(".boardReadHeader h3.title, .boardRead h3.title")
        body = soup.select_one(".boardReadBody .boardContents")
        source_id = str(item.get("source_id") or _query(post_url).get("no", [""])[0])
        if not wrapper or not title or body is None or not source_id.isdigit():
            return None
        author: str | None = None
        published = str(item.get("date") or "")
        for entry in wrapper.select(".datainfo li"):
            label = entry.select_one("strong")
            label_text = label.get_text(" ", strip=True).rstrip(":") if label else ""
            value = entry.get_text(" ", strip=True)
            if label:
                value = value.replace(label.get_text(" ", strip=True), "", 1).lstrip(" :").strip()
            if "작성자" in label_text:
                author = value or None
            elif "작성일" in label_text:
                match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", value)
                if match:
                    published = match.group().replace(".", "-").replace("/", "-")
        clean = BeautifulSoup(str(body), "lxml")
        for selector in ("nav", ".breadcrumb", ".location", ".share", ".sns", ".btn", "script", "style"):
            for element in clean.select(selector):
                element.decompose()
        text = re.sub(r"\s+", " ", clean.get_text(" ", strip=True)).strip()
        return {"title": title.get_text(" ", strip=True), "author": author, "date": published,
                "url": post_url, "is_notice": bool(item.get("is_notice")), "body": text,
                "attachments": self.parse_attachments(wrapper, page_url=post_url, base_url=base_url,
                                                      site_prefix=site_prefix), "source_id": source_id}

    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "",
    ) -> dict[str, Any]:
        title = soup.select_one("#contents h1, #contents h2, .page-title, .subTitle")
        content = self.select_body_root(soup, page_url=page_url, base_url=base_url)
        body = extract_body_content(content, prefer_detail_body=False)
        attachments = self.parse_attachments(content, page_url=page_url, base_url=base_url, site_prefix=site_prefix)
        return {"title": title.get_text(" ", strip=True) if title else fallback_title,
                "content": body, "attachments": attachments,
                **_empty_content_info(content, body, attachments)}
