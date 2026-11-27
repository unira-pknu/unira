"""Adapter for legacy PHP sites using ``bbscode``, ``idx`` and ``kind=view``."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import DepartmentCMSAdapter
from .numeric_cms import (
    extract_body_content, _empty_content_info, _body_reference_links, _embedded_media_links,
)


_SUPPORTED_ROOTS = {"00main", "01about", "05piazza"}
_NAV_NAMES = {"HOME", "LOGIN", "LOGOUT", "SITEMAP", "홈", "로그인", "사이트맵"}


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _bbs_code(html: str, page_url: str) -> str | None:
    query = _query(page_url)
    value = str(query.get("bbscode", [""])[0]).strip()
    if value:
        return value
    soup = BeautifulSoup(html, "lxml")
    field = soup.select_one('[name="bbscode"]')
    if field and str(field.get("value") or "").strip():
        return str(field.get("value")).strip()
    for anchor in soup.select("a[href]"):
        value = str(_query(str(anchor.get("href") or "")).get("bbscode", [""])[0]).strip()
        if value:
            return value
    return None


def _static_video_content(soup, content, page_url: str):
    """Preserve the verified geoinfo player's literal source without running JS."""
    if content is None or urlsplit(page_url).hostname != "geoinfo.pknu.ac.kr":
        return content
    frame = content.select_one("iframe#youtubeFrame")
    if frame is None or str(frame.get("src") or "").strip():
        return content
    video_ids = set()
    for script in soup.select("script:not([src])"):
        code = script.get_text()
        # Require the known element binding and exact template assignment in
        # the same script; arbitrary JS expressions and other players stay empty.
        if not re.search(
            r"(?m)^\s*const\s+youtubeFrame\s*=\s*document\.getElementById\(['\"]youtubeFrame['\"]\)\s*;", code,
        ):
            continue
        if not re.search(r"(?m)^\s*youtubeFrame\.src\s*=\s*`https://www\.youtube\.com/embed/\$\{videoId\}`\s*;", code):
            continue
        video_ids.update(re.findall(
            r"(?m)^\s*const\s+videoId\s*=\s*['\"]([A-Za-z0-9_-]{11})['\"]\s*;", code,
        ))
    if len(video_ids) != 1:
        return content
    copy = BeautifulSoup(str(content), "lxml")
    copy.select_one("iframe#youtubeFrame")["src"] = "https://www.youtube.com/embed/" + video_ids.pop()
    return copy


class LegacyPHPAdapter(DepartmentCMSAdapter):
    name = "legacy_php"
    version = "1.2"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if detail:
            table = soup.select_one("table.write")
            if table is None:
                return None
            cells = [row.select_one("td[colspan]") for row in table.select("tr") if row.select_one("th") is None]
            return next((cell for cell in reversed(cells) if cell is not None), None)
        for selector in (".page_con", "#contents", "#content", ".contents", ".content", "table.tb3", "#container"):
            root = soup.select_one(selector)
            if root is not None:
                return root
        return None

    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]:
        soup, base = BeautifulSoup(html, "lxml"), urlsplit(base_url)
        found: dict[str, dict[str, str]] = {}
        for anchor in soup.select("a[href]"):
            name = anchor.get_text(" ", strip=True).lstrip("- ").strip()
            absolute = urljoin(base_url, str(anchor.get("href") or ""))
            parsed, query = urlsplit(absolute), _query(absolute)
            parts = parsed.path.strip("/").split("/")
            if parsed.netloc.lower() != base.netloc.lower() or not parts or parts[0] not in _SUPPORTED_ROOTS:
                continue
            if not parsed.path.lower().endswith(".php") or query.get("idx") or query.get("kind"):
                continue
            if not name:
                continue
            normalized_path = "/" + "/".join(part for part in parsed.path.split("/") if part)
            if name.upper() in _NAV_NAMES or normalized_path == "/00main/main.php":
                continue
            clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            found.setdefault(clean, {"name": name or parsed.path.rsplit("/", 1)[-1], "url": clean,
                                     "path": parsed.path})
        return list(found.values())

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        requested, effective_url = urlsplit(page_url), final_url or page_url
        effective = urlsplit(effective_url)
        token = re.sub(r"[^a-zA-Z0-9]+", "_", requested.path.strip("/")).strip("_")
        common = {"id": f"php_{token}", "name": name or token, "category": "미분류",
                  "path": requested.path, "final_url": effective_url}
        if effective.hostname and effective.hostname.lower() != (requested.hostname or "").lower():
            return {**common, "kind": "static_page", "document_type": "guide", "bbs_id": None,
                    "confidence": 1.0, "status": "unsupported", "warnings": ["external redirect blocked"]}
        soup = BeautifulSoup(html, "lxml")
        bbs = _bbs_code(html, page_url)
        board = bool(soup.select_one("table.list_normal_D")) or any(
            str(_query(urljoin(page_url, str(a.get("href") or ""))).get("kind", [""])[0]).lower() == "view"
            for a in soup.select("a[href]")
        )
        static = bool(soup.select_one("#contents, #content, .contents, .content, #container, table.tb3, .page_con"))
        warnings = [] if not board or bbs else (["board has no bbscode"] if board else [])
        return {**common, "kind": "board" if board else "static_page",
                "document_type": "notice" if board else "guide", "bbs_id": bbs,
                "confidence": 0.95 if board and bbs else (0.75 if board else (0.65 if static else 0.2)),
                "status": "candidate" if (board and bbs) or static else "unsupported", "warnings": warnings}

    def list_request(self, board_url: str, page: int, bbs_id: str | None = None) -> tuple[str, dict[str, Any]]:
        return board_url, {"p": page, "bbscode": bbs_id or ""}

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in soup.select("table.list_normal_D tr"):
            link = next((a for a in row.select("a[href]") if
                         str(_query(urljoin(board_url, str(a.get("href") or ""))).get("kind", [""])[0]).lower() == "view"), None)
            if not link:
                continue
            url = urljoin(board_url, str(link.get("href") or ""))
            idx = str(_query(url).get("idx", [""])[0])
            if not idx.isdigit() or idx in seen:
                continue
            seen.add(idx)
            cells = row.select("td")
            texts = [cell.get_text(" ", strip=True) for cell in cells]
            date = next((text for text in reversed(texts) if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)), "")
            # A broad substring caused malformed legacy number cells to mark
            # every row as pinned. Only the number cell or an explicit icon
            # may identify a notice.
            first_cell = texts[0].strip() if texts else ""
            notice_icon = row.select_one('img[alt*="공지"], img[title*="공지"]')
            notice = first_cell == "공지" or notice_icon is not None
            result.append({"source_id": idx, "post_url": url, "num": "NOTICE" if notice else idx,
                           "post_no": int(idx), "is_notice": notice, "date": date,
                           "title": link.get_text(" ", strip=True)})
        return result

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        if not content:
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in content.select("a[href]"):
            href = str(anchor.get("href") or "")
            if str(_query(href).get("mode", [""])[0]).lower() != "down":
                continue
            url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)
            result.append({"name": anchor.get_text(" ", strip=True) or f"attachment-{len(result)+1}", "url": url})
        return result

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        table = soup.select_one("table.write")
        idx = str(item.get("source_id") or _query(post_url).get("idx", [""])[0])
        if not table or not idx.isdigit():
            return None
        fields: dict[str, str] = {}
        body_cell = None
        for row in table.select("tr"):
            header, cell = row.select_one("th"), row.select_one("td")
            if not cell:
                continue
            label = header.get_text(" ", strip=True) if header else ""
            if label:
                fields[label] = cell.get_text(" ", strip=True)
            elif str(cell.get("colspan") or ""):
                body_cell = cell
        title = next((v for k, v in fields.items() if "제목" in k), "")
        if not title or body_cell is None:
            return None
        author = next((v for k, v in fields.items() if "작성자" in k), None)
        date_text = next((v for k, v in fields.items() if "작성일" in k), str(item.get("date") or ""))
        match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", date_text)
        date = match.group().replace(".", "-").replace("/", "-") if match else str(item.get("date") or "")
        clean = BeautifulSoup(str(body_cell), "lxml")
        for selector in ("script", "style", ".breadcrumb", ".share", ".sns", "nav"):
            for element in clean.select(selector):
                element.decompose()
        body = re.sub(r"\s+", " ", clean.get_text(" ", strip=True)).strip()
        return {"title": title, "author": author, "date": date, "url": post_url,
                "is_notice": bool(item.get("is_notice")), "body": body, "source_id": idx,
                "attachments": self.parse_attachments(table, page_url=post_url, base_url=base_url,
                                                      site_prefix=site_prefix)}

    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "",
    ) -> dict[str, Any]:
        title = soup.select_one("h1, h2, .nav_title_txt, .page-title")
        content = self.select_body_root(soup, page_url=page_url, base_url=base_url)
        if not content:
            return {"title": fallback_title, "content": "", "attachments": []}
        content = _static_video_content(soup, content, page_url or base_url)
        text = extract_body_content(content, prefer_detail_body=False)
        attachments = self.parse_attachments(content, page_url=page_url, base_url=base_url, site_prefix=site_prefix)
        references = _embedded_media_links(content, page_url or base_url) + _body_reference_links(content, page_url or base_url)
        return {"title": title.get_text(" ", strip=True) if title else fallback_title,
                "content": text, "attachments": attachments,
                **({"reference_links": references} if references else {}),
                **_empty_content_info(content, text, attachments)}
