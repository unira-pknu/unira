"""Adapter for legacy ``view.do?no=`` boards using ``view=view&idx=``."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .query_view_do import QueryViewDoAdapter, _query_url


def _detail_url(board_url: str, item_id: str) -> str:
    return _query_url(board_url, idx=item_id, view="view")


class QueryViewLegacyAdapter(QueryViewDoAdapter):
    name = "query_view_legacy"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if not detail:
            return super().select_body_root(soup, page_url=page_url, base_url=base_url)
        body = soup.select_one(".board-view-wrap tbody")
        if body is None:
            return None
        clean = BeautifulSoup(str(body), "lxml")
        for row in list(clean.select("tr")):
            if row.parent is not None and row.select_one("a.file-add"):
                row.decompose()
        return clean

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        result = super().analyze_section(name=name, page_url=page_url, html=html, final_url=final_url)
        if result["status"] == "unsupported":
            return result
        soup = BeautifulSoup(html, "lxml")
        board = bool(soup.select_one("table.tbl_01, #photo_list2")) or any(
            parse_qs(urlsplit(str(anchor.get("href") or "")).query).get("view", [""])[0].lower() == "view"
            and str(parse_qs(urlsplit(str(anchor.get("href") or "")).query).get("idx", [""])[0]).isdigit()
            for anchor in soup.select("a[href]")
        )
        if board:
            result.update(kind="board", document_type="notice", confidence=0.95)
        return result

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        links = soup.select("table.tbl_01 tbody tr a[href*=idx], #photo_list2 li a[href*=idx]")
        for link in links:
            href = urljoin(board_url, str(link.get("href") or ""))
            query = parse_qs(urlsplit(href).query)
            item_id = str(query.get("idx", [""])[0])
            if not item_id.isdigit() or item_id in seen:
                continue
            seen.add(item_id)
            row = link.find_parent("tr")
            date_el = row.select_one(".date") if row else link.find_next("span", class_="date")
            notice = bool(row and row.select_one('img[alt*="공지"]'))
            title = link.select_one(".btxt") or link
            items.append({
                "source_id": item_id,
                "post_url": _detail_url(board_url, item_id),
                "num": "NOTICE" if notice else item_id,
                "post_no": None if notice else int(item_id),
                "is_notice": notice,
                "date": date_el.get_text(" ", strip=True) if date_el else "",
                "title": title.get_text(" ", strip=True),
            })
        return items

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if not content:
            return []
        attachments: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in content.select("a.file-add"):
            match = re.search(r"downFile\s*\(\s*['\"]?(\d+)", str(anchor.get("onclick") or ""), re.I)
            if match:
                form = anchor.find_parent("form") or content.select_one('form[name="board_form"]')
                fields: list[tuple[str, str]] = []
                if form:
                    for field in form.select("input[name]"):
                        name = str(field.get("name") or "")
                        value = match.group(1) if name == "fidx" else str(field.get("value") or "")
                        if name:
                            fields.append((name, value))
                if not any(name == "fidx" for name, _ in fields):
                    fields.append(("fidx", match.group(1)))
                url = urljoin(page_url, "/base/portal/bbs/downFile.do") + "?" + urlencode(fields)
            else:
                href = str(anchor.get("href") or "").strip()
                if not href or href.lower().startswith("javascript:"):
                    continue
                url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)
            attachments.append({
                "name": anchor.get_text(" ", strip=True) or f"attachment-{len(attachments) + 1}",
                "url": url,
            })
        return attachments

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        wrapper = soup.select_one(".board-view-wrap")
        title = wrapper.select_one(".btxt") if wrapper else None
        query_idx = str(parse_qs(urlsplit(post_url).query).get("idx", [""])[0])
        source_id = str(item.get("source_id") or query_idx)
        if not wrapper or not title or not source_id.isdigit():
            return None

        author_el = wrapper.select_one("thead .name")
        published = ""
        for element in wrapper.select("thead span"):
            match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", element.get_text(" ", strip=True))
            if match:
                published = match.group().replace(".", "-").replace("/", "-")
                break

        body_parts: list[str] = []
        for row in wrapper.select("tbody tr"):
            if row.select_one("a.file-add"):
                continue
            clone = BeautifulSoup(str(row), "lxml")
            for element in clone.select("script, style, .board_btn, .np-page"):
                element.decompose()
            text = re.sub(r"\s+", " ", clone.get_text(" ", strip=True)).strip()
            if text:
                body_parts.append(text)

        return {
            "title": title.get_text(" ", strip=True),
            "author": author_el.get_text(" ", strip=True) if author_el else None,
            "date": published or item.get("date", ""),
            "url": post_url,
            "is_notice": item.get("is_notice", False),
            "body": " ".join(body_parts),
            "attachments": self.parse_attachments(
                soup, page_url=post_url, base_url=base_url, site_prefix=site_prefix,
            ),
            "source_id": source_id,
        }
