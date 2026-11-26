"""Reusable adapter for ``view.do?no=`` menu and ``pgMode=View&idx=`` boards."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scripts.crawlers.departments.adapters.base import DepartmentCMSAdapter
from scripts.crawlers.departments.adapters.numeric_cms import extract_body_content, _empty_content_info


_EXCLUDED_NAMES = {"HOME", "SITEMAP", "LOGIN", "LOGOUT", "홈", "사이트맵", "로그인", "로그아웃"}


def _query_url(url: str, **values: str) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    # A detail's identity is its menu/idx, not the list page it appeared on.
    query.pop("pageIndex", None)
    query.update({key: [value] for key, value in values.items()})
    flat = [(key, value) for key, items in query.items() for value in items]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(flat), ""))


class QueryViewDoAdapter(DepartmentCMSAdapter):
    name = "query_view_do"
    version = "1.1"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if detail:
            return soup.select_one(".editor-data-box")
        for selector in (".sub-content", ".contents", "main", "#container-wrap"):
            root = soup.select_one(selector)
            if root is not None:
                return root
        return None

    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        base = urlsplit(base_url)
        found: dict[str, dict[str, str]] = {}
        for anchor in soup.select("a[href]"):
            name = anchor.get_text(" ", strip=True)
            absolute = urljoin(base_url, str(anchor.get("href") or ""))
            parsed = urlsplit(absolute)
            query = parse_qs(parsed.query)
            menu_no = str(query.get("no", [""])[0])
            mode = str(query.get("pgMode", [""])[0]).lower()
            if parsed.netloc.lower() != base.netloc.lower():
                continue
            if not parsed.path.endswith("/view.do") or not menu_no.isdigit():
                continue
            if mode == "view" or query.get("idx"):
                continue
            if name.strip().upper() in _EXCLUDED_NAMES:
                continue
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"no": menu_no}), ""))
            found.setdefault(menu_no, {"name": name or f"menu-{menu_no}", "url": url, "menu_no": menu_no})
        return list(found.values())

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        requested = urlsplit(page_url)
        effective_url = final_url or page_url
        effective = urlsplit(effective_url)
        menu_no = str(parse_qs(requested.query).get("no", [""])[0])
        common = {
            "id": f"menu_{menu_no}", "name": name or f"menu-{menu_no}", "category": "미분류",
            "path": f"{requested.path}?{urlencode({'no': menu_no})}", "final_url": effective_url,
        }
        if effective.hostname and effective.hostname.lower() != (requested.hostname or "").lower():
            return {**common, "kind": "static_page", "document_type": "guide", "bbs_id": None,
                    "confidence": 1.0, "status": "unsupported", "warnings": [f"external redirect: {effective_url}"]}
        soup = BeautifulSoup(html, "lxml")
        board = bool(soup.select_one(".board-list-wrap, input[name=board_id]")) or any(
            "moveview(" in str(anchor.get("onclick") or "").lower() for anchor in soup.select("a[onclick]")
        )
        static = bool(soup.select_one("main, #container-wrap, .sub-content, .contents"))
        status = "candidate" if board or static else "unsupported"
        return {**common, "kind": "board" if board else "static_page",
                "document_type": "notice" if board else "guide", "bbs_id": None,
                "confidence": 0.9 if board else (0.65 if static else 0.2), "status": status,
                "warnings": []}

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in soup.select(".board-list-wrap tbody tr, li:has(a[onclick*=moveView])"):
            link = row.select_one("a[onclick*=moveView], a[href*=idx]")
            if not link:
                continue
            onclick = str(link.get("onclick") or "")
            match = re.search(r"moveView\(['\"]?(\d+)", onclick)
            href = urljoin(board_url, str(link.get("href") or ""))
            query = parse_qs(urlsplit(href).query)
            item_id = match.group(1) if match else str(query.get("idx", [""])[0])
            if not item_id.isdigit() or item_id in seen:
                continue
            seen.add(item_id)
            cells = row.select("td")
            date = cells[-1].get_text(strip=True) if cells else ""
            if not cells:
                match_date = re.search(r"\d{4}[.-]\d{2}[.-]\d{2}", row.get_text(" ", strip=True))
                date = match_date.group().replace(".", "-") if match_date else ""
            notice = bool(row.select_one('img[alt="공지"]'))
            items.append({"source_id": item_id, "post_url": _query_url(board_url, pgMode="View", idx=item_id),
                          "num": "NOTICE" if notice else (cells[0].get_text(strip=True) if cells else ""),
                          "post_no": None if notice else int(item_id), "is_notice": notice, "date": date,
                          "title": (link.select_one('.btxt') or link).get_text(" ", strip=True)})
        return items

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if not content:
            return []
        attachments: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in content.select("a.file-add[href], a[href*=boardDown]"):
            href = str(anchor.get("href") or "").strip()
            if not href or href.lower().startswith("javascript:"):
                continue
            url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)
            name = anchor.get_text(" ", strip=True) or f"attachment-{len(attachments) + 1}"
            attachments.append({"name": name, "url": url})
        return attachments

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        wrapper = soup.select_one(".board-view-wrap")
        body = soup.select_one(".editor-data-box")
        title = soup.select_one(".board-view-wrap .btxt")
        query_idx = str(parse_qs(urlsplit(post_url).query).get("idx", [""])[0])
        source_id = str(item.get("source_id") or query_idx)
        if not wrapper or not body or not title or not source_id.isdigit():
            return None

        author: str | None = None
        published = ""
        for label in wrapper.select("strong"):
            label_text = label.get_text(" ", strip=True).rstrip(":")
            parent_text = label.parent.get_text(" ", strip=True) if label.parent else ""
            value = parent_text.replace(label.get_text(" ", strip=True), "", 1).lstrip(" :").strip()
            if label_text in {"작성자", "등록자"} and value:
                author = value
            elif label_text in {"작성일자", "작성일", "등록일"}:
                match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", value)
                if match:
                    published = match.group().replace(".", "-").replace("/", "-")

        body_copy = BeautifulSoup(str(body), "lxml")
        for selector in ("nav", ".breadcrumb", ".location", ".share", ".sns", ".btn-wrap", "script", "style"):
            for element in body_copy.select(selector):
                element.decompose()
        body_text = re.sub(r"\s+", " ", body_copy.get_text(" ", strip=True)).strip()
        images = body_copy.select("img[src]")
        return {"title": title.get_text(" ", strip=True), "author": author,
                "date": published or item.get("date", ""),
                "url": post_url, "is_notice": item.get("is_notice", False), "body": body_text,
                "attachments": self.parse_attachments(wrapper, page_url=post_url, base_url=base_url,
                                                      site_prefix=site_prefix),
                "source_id": source_id,
                **({"content_state": "image_only", "content_images": [str(img["src"]) for img in images
                    if not str(img["src"]).startswith("data:")]}
                   if not body_text and images else {})}

    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "",
    ) -> dict[str, Any]:
        title = soup.select_one("h1, .page-title")
        content = self.select_body_root(soup, page_url=page_url, base_url=base_url)
        text = extract_body_content(content, prefer_detail_body=False)
        attachments = self.parse_attachments(content, page_url=page_url, base_url=base_url, site_prefix=site_prefix)
        return {"title": title.get_text(" ", strip=True) if title else fallback_title,
                "content": text, "attachments": attachments,
                **_empty_content_info(content, text, attachments)}
