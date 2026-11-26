"""Parser for the numeric-menu PKNU CMS originally used by CE."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from scripts.crawlers.departments.adapters.base import DepartmentCMSAdapter
from scripts.crawlers.departments.probe import numeric_menu_links, select_content_container
from scripts.crawlers.departments.urls import resolve_link


_NAV_TEXTS = {"목록보기", "다음", "이전", "next", "prev"}
_NAV_PHRASE_RE = re.compile(
    r"(다음\s*게시글이?\s*없습니다\.?|이전\s*게시글이?\s*없습니다\.?)", re.IGNORECASE,
)
# These sites use .a_bdCont for guides and .c_bdCont for board details.
# Keep the existing selector first and limit the alternate layout to verified hosts.
_A_BDCONT_FIRST_HOSTS = frozenset({
    "control.pknu.ac.kr",
    "fashion.pknu.ac.kr",
    "materials.pknu.ac.kr",
    "college.pknu.ac.kr",
    "ref.pknu.ac.kr",
    "fisheries.pknu.ac.kr",
    "semiconductor.pknu.ac.kr",
})


def _text(element) -> str:
    return element.get_text(" ", strip=True) if element else ""


def _slug(url: str, extra: str = "") -> str:
    return hashlib.md5(f"{url}|{extra}".encode()).hexdigest()[:12]


def _detail_source_id(href: str, board_url: str) -> str:
    absolute = resolve_link(href, board_url)
    if not absolute:
        return ""
    parsed = urlparse(absolute)
    if parse_qs(parsed.query).get("action", [""])[0].lower() != "view":
        return ""
    source_id = str(parse_qs(parsed.query).get("no", [""])[0]).strip()
    return source_id if re.fullmatch(r"[A-Za-z0-9_-]+", source_id) else ""


def _list_date(container) -> str:
    date_element = container.select_one(".bdlDate, .date, [class*='date']")
    text = _text(date_element) if date_element else _text(container)
    match = re.search(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", text)
    return match.group().replace(".", "-").replace("/", "-") if match else ""


def _fallback_list_items(soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
    """Parse newer numeric CMS table, card, and gallery list variants."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        source_id = _detail_source_id(href, board_url)
        if not source_id or source_id in seen:
            continue
        if link.find_parent(class_=re.compile(r"(?:paging|pagination|board-nav|bdvNav)", re.I)):
            continue
        container = link.find_parent(["tr", "li"])
        if container is None:
            continue
        if container.name == "tr" and not container.select("td"):
            continue
        title = _text(link)
        if not title or title.strip().lower() in _NAV_TEXTS:
            continue

        number = ""
        if container.name == "tr":
            number_element = container.select_one(".bdlNum, .num, td:first-child")
            number = _text(number_element)
        marker = number.strip().upper()
        notice_badge = container.select_one('img[alt*="공지"], img[title*="공지"], em')
        badge_text = _text(notice_badge).strip().upper()
        notice = (
            marker in {"NOTICE", "N", "공지"}
            or badge_text in {"NOTICE", "N", "공지"}
        )
        post_no = int(marker) if marker.isdigit() else None
        seen.add(source_id)
        items.append({
            "source_id": source_id,
            "post_url": resolve_link(href, board_url),
            "num": "NOTICE" if notice else number,
            "post_no": post_no,
            "is_notice": notice,
            "date": _list_date(container),
            "title": title,
        })
    return items


def _sns_list_items(soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
    """Read CMS SNS cards whose public content lives in the list itself."""
    items = []
    seen = set()
    parsed_url = urlparse(board_url)
    canonical_url = parsed_url._replace(
        query=urlencode([(key, value) for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
                         if key != 'pageIndex']), fragment='',
    ).geturl()
    for card in soup.select('.snsList .snsBox'):
        title = _text(card.select_one('.txtbox p'))
        if not title:
            continue
        modal = re.fullmatch(r"\s*snsModal\(\s*(['\"])(.*?)\1\s*\)\s*;?\s*", str(card.get('onclick') or ''))
        target = resolve_link(modal.group(2), board_url) if modal and modal.group(2) else None
        token = 'sns_' + hashlib.sha256(f'{title}|{target or ""}'.encode()).hexdigest()[:20]
        if token in seen:
            continue
        seen.add(token)
        items.append({'source_id': token, 'inline_card_id': token,
                      'post_url': canonical_url + '#' + token,
                      'num': '', 'post_no': None, 'is_notice': False,
                      'date': _text(card.select_one('.txtbox span')), 'title': title,
                      'reference_links': [{'label': title, 'url': target, 'followed': False}] if target else []})
    return items


def extract_bbs_id(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    field = soup.select_one('input[name="bbsId"], input[name="bbs_id"]')
    if field and str(field.get("value") or "").isdigit():
        return str(field.get("value"))
    for pattern in (
        r"(?i)bbsId\s*[=:]\s*['\"]?(\d+)", r"(?i)bbs_id\s*[=:]\s*['\"]?(\d+)",
        r"(?i)[?&]bbsId=(\d+)",
    ):
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    for anchor in soup.select("a[href]"):
        match = re.search(r"(?i)[?&](?:bbsId|bbs_id)=(\d+)", str(anchor.get("href") or ""))
        if match:
            return match.group(1)
    match = re.search(r"(?i)[?&]bbsId=(\d+)", page_url)
    return match.group(1) if match else None


def _has_board_table(soup: BeautifulSoup) -> bool:
    """Find structural board evidence without treating every view link as a board."""
    if soup.select_one(".a_brdList"):
        return True
    for table in soup.select("table"):
        headings = " ".join(_text(cell) for cell in table.select("thead th, tr:first-child th"))
        lowered = headings.lower()
        marker_count = sum(marker in lowered for marker in ("번호", "제목", "작성자", "작성일", "조회"))
        marker_count += sum(
            bool(re.search(rf"\b{marker}\b", lowered))
            for marker in ("no", "title", "author", "date", "views")
        )
        if marker_count >= 2 and table.select_one('a[href*="action=view"]'):
            return True
    return False


def _table_text(table) -> str:
    """Keep row/cell boundaries and carry merged row labels into each row."""
    lines = []
    spans: dict[int, tuple[str, int]] = {}
    caption = table.find("caption", recursive=False)
    if caption:
        lines.append(_text(caption))
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        values = {col: value for col, (value, _) in spans.items()}
        spans = {col: (value, left - 1) for col, (value, left) in spans.items() if left > 1}
        col = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while col in values:
                col += 1
            def span(name: str) -> int:
                try:
                    return min(1000, max(1, int(cell.get(name, 1))))
                except (TypeError, ValueError):
                    return 1
            width, height = span("colspan"), span("rowspan")
            value = _text(cell)
            for offset in range(width):
                text = value
                values[col + offset] = text
                if height > 1:
                    spans[col + offset] = (text, height - 1)
            col += width
        if any(values.values()):
            lines.append(" | ".join(values.get(i, "") for i in range(max(values) + 1)))
    return "\n".join(lines)


def _embedded_media_links(content_el, page_url: str = "") -> list[dict[str, Any]]:
    """Keep public iframe/video source references without requesting them."""
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for media in content_el.select("iframe[src], video, audio") if content_el else []:
        sources = [media] if media.get("src") else media.select("source[src]")
        for source in sources:
            raw = str(source.get("src") or "").strip()
            parsed = urlparse(raw)
            if not raw or (parsed.scheme and parsed.scheme not in {"http", "https"}):
                continue
            if parsed.scheme and not parsed.netloc:
                continue
            if not parsed.scheme and not (parsed.path or parsed.netloc):
                continue
            url = urljoin(page_url, raw) if page_url else raw
            if url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "label": str(media.get("title") or
                          ("문서" if _attachment_candidate(raw) else
                           "영상" if media.name in {"iframe", "video"} else "오디오")),
                          "source": media.name, "followed": False})
    return links


def _label_faculty_tabs(document) -> bool:
    labels: dict[str, set[str]] = {}
    for link in document.select(".tch_tab a"):
        handler = re.fullmatch(r"\s*fn_tab_ch\(\s*this\s*,\s*(\d+)\s*\)\s*;?\s*", link.get("onclick", ""))
        fragment = re.fullmatch(r"#(tab\d+)", link.get("href", ""))
        target = f"tab{handler[1]}" if handler else (fragment[1] if fragment else "")
        if target and (label := _text(link)):
            labels.setdefault(target, set()).add(label)

    changed = False
    for target, headings in labels.items():
        panels = document.find_all(id=target)
        if len(headings) != 1 or len(panels) != 1 or "tab" not in panels[0].get("class", []):
            continue
        panels[0].insert(0, f"\n{next(iter(headings))}:\n")
        changed = True
    return changed


def extract_body_content(content_el, *, prefer_detail_body: bool = True, faculty_tab_labels: bool = False) -> str:
    if content_el is None:
        return ""
    copy = BeautifulSoup(str(content_el), "lxml")
    labeled_tabs = faculty_tab_labels and _label_faculty_tabs(copy)
    for selector in ("script", "style", "noscript", "nav", "#subTitle",
                     ".breadcrumb", ".location", ".e_sbtNav", ".a_sbtNav", ".b_sbtNav", ".d_sbtNav",
                     ".b_subTitle", ".d_subTitle", ".sch_tab", ".tch_tab",
                     ".c_bdvBtn", ".c_bdvNav", ".a_bdPaging", ".board-nav", ".btn-list"):
        for element in copy.select(selector):
            if element.parent is not None:
                element.decompose()
    body = copy.select_one(".bdvEdit") if prefer_detail_body else None
    if not body:
        for anchor in copy.select("a"):
            if anchor.get_text(strip=True).lower() in _NAV_TEXTS:
                anchor.decompose()
    root = body if body is not None else copy
    for media in list(root.select("iframe[src], video, audio")):
        references = _embedded_media_links(BeautifulSoup(str(media), "lxml"))
        if references:
            media.replace_with("\n" + "\n".join(
                f"{link['label']}: {link['url']}" for link in references
            ) + "\n")
    for anchor in root.select('a[href^="mailto:"]'):
        address = str(anchor['href'])[7:].split('?', 1)[0].strip()
        if address and address not in _text(anchor):
            anchor.append(' ' + address)
    # Static guides often consist entirely of a table. Never discard a table
    # merely because it is the first one; the detail body already excludes metadata.
    tables = root.select("table")
    for table in reversed(tables):
        table.replace_with("\n" + _table_text(table) + "\n")
    multiline = bool(tables) or labeled_tabs
    text = root.get_text(" ", strip=not multiline)
    if multiline:
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = _NAV_PHRASE_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _detail_content_root(soup, site_url: str):
    root = soup.select_one(".a_bdCont")
    if root is None and urlparse(site_url).hostname in _A_BDCONT_FIRST_HOSTS:
        root = soup.select_one(".c_bdCont")
    return root


def _content_root(soup, site_url: str = ""):
    # Some templates keep the complete guide outside the standard #sbCont.
    for selector in (".e_sbCont #sub", "#stance"):
        root = soup.select_one(selector)
        if root is not None:
            return root
    if urlparse(site_url).hostname in _A_BDCONT_FIRST_HOSTS:
        root = _detail_content_root(soup, site_url)
        if root is not None:
            return root
    # select_one with a comma-separated selector uses document order, not
    # selector priority. CE has an empty layout .container before #sbCont.
    for selector in ("#sbCont", ".b_bdCont", ".d_sbCont"):
        root = soup.select_one(selector)
        if root is not None:
            return root
    for selector in (".e_sbCont", ".page_con", "#contents", ".contents", "main", ".a_bdCont"):
        root = soup.select_one(selector)
        if root is not None:
            return root
    # Verified custom guides have no dedicated body class. Their #sub child
    # contains the guide itself; an earlier header .container is only layout.
    # Some templates repeat #sub around the sidebar and the actual body.
    if urlparse(site_url).hostname in _A_BDCONT_FIRST_HOSTS:
        for root in soup.select("#sub > .container"):
            if root.select_one("#sub") is None:
                return root
    return select_content_container(soup)


def _heading_has_reference(node) -> bool:
    for anchor in node.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not _text(anchor) or not href or href.startswith("#"):
            continue
        try:
            parsed = urlparse(href)
        except ValueError:
            continue
        if parsed.scheme.lower() in {"", "http", "https"}:
            return True
    return False


def _empty_content_info(root, text, attachments):
    # A heading above a diagram is not an extraction of the diagram's text.
    meaningful = BeautifulSoup(str(root), "lxml") if root is not None else None
    if meaningful is not None:
        for node in meaningful.select("h1, h2, h3, h4, h5, h6"):
            # E-book language links can be the entire guide, inside an h4.
            # Keep their visible labels, but not plain headings or UI links.
            if _heading_has_reference(node):
                node.unwrap()
            else:
                node.decompose()
    if text.strip() and (meaningful is None or extract_body_content(meaningful, prefer_detail_body=False).strip()):
        return {}
    from scripts.crawlers.common.body_images import collect_body_images
    images = [item["src"] for item in collect_body_images(root, root=root)] if root is not None else []
    return {"content_state": "attachment_only" if attachments else "image_only" if images else "empty",
            "content_images": [src for src in images if not src.lower().startswith('data:')]}


def _attachment_candidate(href: str, name: str = "") -> bool:
    from scripts.crawlers.departments.attachments import attachment_candidate
    return attachment_candidate(href, name)


def _body_reference_links(content, page_url: str) -> list[dict[str, Any]]:
    if content is None:
        return []
    root = content.select_one('.bdvEdit') or content
    references = {}
    for anchor in root.select('a[href]'):
        href = str(anchor.get('href') or '').strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        name = _text(anchor)
        url = resolve_link(href, page_url)
        if url and not _attachment_candidate(url, name):
            references.setdefault(url, {'url': url, 'label': name, 'source': 'body_link', 'followed': False})
    return list(references.values())


class NumericCMSAdapter(DepartmentCMSAdapter):
    uses_numeric_post_order = True
    name = "numeric_cms"
    version = "1.6"

    def select_body_root(self, soup, *, page_url="", base_url="", detail=False):
        if detail:
            if urlparse(page_url).hostname in {"www.pknu.ac.kr", "pknujob.pknu.ac.kr"}:
                return soup.select_one("div.bdvTxt")
            wrapper = _detail_content_root(soup, base_url or page_url)
            return (wrapper.select_one(".bdvEdit") or wrapper) if wrapper is not None else None
        return _content_root(soup, base_url or page_url)

    def is_profile_detail_url(self, url):
        query = parse_qs(urlparse(url).query)
        return (query.get("action") == ["view"]
                and any(len(query.get(key, [])) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", query[key][0])
                        for key in ("no", "profile")))

    def profile_target_allowed(self, target, requested_url):
        requested, actual = urlparse(requested_url), urlparse(target)
        if actual.scheme not in {"http", "https"} or actual.username or actual.password:
            return False
        if actual.netloc.lower() not in {requested.netloc.lower(), "icms.pknu.ac.kr"}:
            return False
        if actual.path.rstrip("/") != requested.path.rstrip("/"):
            return False
        if not self.is_profile_detail_url(target):
            return False
        return parse_qs(actual.query) == parse_qs(requested.query)

    def static_detail_links(self, soup, *, page_url):
        if self.is_profile_detail_url(page_url):
            return []
        root = self.select_body_root(soup, page_url=page_url)
        if root is None:
            return []
        page = urlparse(page_url)
        found = {}
        for anchor in root.select("a[href]"):
            target = resolve_link(anchor["href"], page_url)
            if not target or not self.is_profile_detail_url(target):
                continue
            parsed = urlparse(target)
            if (parsed.path.rstrip("/") != page.path.rstrip("/")
                    or parsed.netloc.lower() not in {page.netloc.lower(), "icms.pknu.ac.kr"}):
                continue
            card = anchor.find_parent(class_=re.compile(r"^(?:tch_list|professor-card|profile-card|faculty-card)$"))
            if card is None and not re.search(r"상세|프로필|profile|detail", _text(anchor), re.I):
                continue
            # Retain the configured origin so discovered and registered profiles
            # have the same document identity even when the CMS uses an alias.
            query = parse_qs(parsed.query)
            identity_key = "no" if query.get("no") else "profile"
            identity = (identity_key, query[identity_key][0])
            target = page._replace(query=urlencode({"action": "view", identity_key: identity[1]}), fragment="").geturl()
            name = _text(card.select_one(".a_tch_name, h2, h3")) if card else ""
            found.setdefault(identity, {"url": target, "label": name or _text(anchor),
                                        "source": "faculty_profile", "followed": False})
        return list(found.values())

    def parse_profile(self, soup, *, fallback_title, page_url, base_url, site_prefix):
        root = self.select_body_root(soup, page_url=page_url, base_url=base_url)
        if root is None or root.select_one(".tch_in") is None:
            return None
        name = root.select_one(".a_tch_name")
        if name is None or not _text(name):
            return None
        result = self.parse_static(soup, fallback_title=fallback_title, page_url=page_url,
                                   base_url=base_url, site_prefix=site_prefix, faculty_tab_labels=True)
        result["title"] = _text(name)
        return result

    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]:
        return numeric_menu_links(html, base_url)

    def analyze_section(self, *, name: str, page_url: str, html: str, final_url: str | None = None) -> dict[str, Any]:
        soup = BeautifulSoup(html, "lxml")
        parsed = urlparse(page_url)
        menu_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        effective_url = final_url or page_url
        effective = urlparse(effective_url)
        common = {
            "id": f"menu_{menu_id}", "name": name or f"menu-{menu_id}",
            "category": "미분류", "path": parsed.path.rstrip("/"), "final_url": effective_url,
        }
        if effective.hostname and effective.hostname.lower() != (parsed.hostname or "").lower():
            return {**common, "kind": "static_page", "document_type": "guide", "bbs_id": None,
                    "confidence": 1.0, "status": "unsupported", "warnings": [f"external redirect: {effective_url}"]}
        if menu_id in {"1", "9999"} or name.strip().upper() in {"HOME", "SITEMAP"}:
            return {**common, "kind": "static_page", "document_type": "guide", "bbs_id": None,
                    "confidence": 1.0, "status": "unsupported", "warnings": ["navigation-only menu"]}
        bbs_id = extract_bbs_id(html, page_url)
        board_list = _has_board_table(soup)
        view_links = any("action=view" in str(a.get("href") or "").lower() for a in soup.select("a[href]"))
        is_board = board_list or bbs_id is not None
        has_content = _content_root(soup, page_url) is not None
        needs_adapter = board_list and bbs_id is None
        static_collection = view_links and not is_board
        warnings = (
            ["board structure found but bbs_id is unavailable; adapter required"]
            if needs_adapter else []
        )
        confidence = (
            0.95 if board_list and bbs_id else
            0.85 if needs_adapter else
            0.8 if static_collection else
            0.65 if has_content else 0.2
        )
        status = (
            "requires_adapter" if needs_adapter else
            "candidate" if is_board or has_content or static_collection else
            "unsupported"
        )
        return {**common, "kind": "board" if is_board else "static_page",
                "document_type": "notice" if is_board else "guide", "bbs_id": bbs_id,
                "confidence": confidence, "status": status,
                "warnings": warnings}

    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]:
        items = []
        for row in soup.select(".a_brdList tr"):
            cells, link = row.select("td"), row.select_one("td a[href]")
            if not cells or not link:
                continue
            number = cells[0].get_text(strip=True)
            notice = number.upper() == "NOTICE"
            try:
                post_no = None if notice else int(number)
            except ValueError:
                post_no = None
            post_url = resolve_link(link.get("href", ""), board_url)
            if not post_url:
                continue
            items.append({"post_url": post_url, "num": number,
                          "post_no": post_no, "is_notice": notice,
                          "date": cells[-2].get_text(strip=True) if len(cells) >= 2 else ""})
        return items or _fallback_list_items(soup, board_url) or _sns_list_items(soup, board_url)

    def parse_attachments(self, content, *, page_url: str, base_url: str, site_prefix: str) -> list[dict[str, str]]:
        attachments = []
        if not content:
            return attachments
        base = urlparse(base_url)
        for anchor in content.select("a[href]"):
            href, name = str(anchor.get("href") or ""), anchor.get_text(strip=True)
            if not href or href.startswith("javascript") or name.lower() in _NAV_TEXTS:
                continue
            absolute = resolve_link(href, page_url or base_url) if page_url or base_url else href
            if not absolute:
                continue
            parsed = urlparse(absolute)
            if "action=view" in parsed.query:
                continue
            if parsed.netloc == base.netloc and re.fullmatch(rf"/{re.escape(site_prefix)}/\d+", parsed.path):
                continue
            if _attachment_candidate(href, name):
                attachments.append({"name": name, "url": absolute})
        for element in content.select("iframe[src], embed[src], object[data]"):
            href = str(element.get("src") or element.get("data") or "").strip()
            if not _attachment_candidate(href):
                continue
            absolute = resolve_link(href, page_url or base_url) if page_url or base_url else href
            if absolute:
                name = str(element.get("title") or urlparse(href).path.rsplit("/", 1)[-1])
                attachments.append({"name": name, "url": absolute})
        return list({item["url"]: item for item in attachments}.values())

    def parse_detail(self, soup: BeautifulSoup, post_url: str, item: dict[str, Any], *, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        if item.get('inline_card_id'):
            card = next((card for card in _sns_list_items(soup, post_url)
                         if card['inline_card_id'] == item['inline_card_id']), None)
            if card is None:
                return None
            return {'title': card['title'], 'date': card['date'], 'url': card['post_url'],
                    'body': card['title'], 'is_notice': False, 'attachments': [],
                    'reference_links': card['reference_links']}
        # Department boards can redirect individual posts to the university CMS.
        # Only accept its explicit detail structure on the known university host.
        if urlparse(post_url).hostname in {"www.pknu.ac.kr", "pknujob.pknu.ac.kr"}:
            requested_no = parse_qs(urlparse(str(item.get("post_url") or "")).query).get("no")
            actual_query = parse_qs(urlparse(post_url).query)
            if actual_query.get("action") != ["view"] or (requested_no and actual_query.get("no") != requested_no):
                return None
            title = soup.select_one("td.title_b")
            body = soup.select_one("div.bdvTxt")
            wrapper = soup.select_one("div.bdCont, form#frmPost")
            if title is None or body is None or wrapper is None:
                return None
            author = soup.select_one("td.text_l.noti_name")
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", " ".join(_text(row) for row in wrapper.select("tr.noti")))
            attachments = [{"name": _text(a), "url": urljoin(post_url, a["href"])}
                           for a in wrapper.select('a[href*="boardDownload.do"]')]
            attachments = list({a["url"]: a for a in attachments}.values())
            text = extract_body_content(body)
            return {"title": _text(title), "author": _text(author), "date": dates[0] if dates else item.get("date", ""),
                    "url": post_url, "body": text, "is_notice": item.get("is_notice", False),
                    "attachments": attachments, **_empty_content_info(body, text, attachments)}
        title_el = soup.select_one(".bdvTitle")
        # Some menu/detail links use the shared icms host. Keep the layout
        # policy bound to the configured department rather than the link host.
        content_el = _detail_content_root(soup, base_url or post_url)
        if not title_el and not content_el:
            return None
        title = _text(title_el)
        date = item.get("date", "")
        if content_el and not date:
            match = re.search(r"\d{4}-\d{2}-\d{2}", _text(content_el))
            date = match.group() if match else date
        body = extract_body_content(content_el)
        media_links = _embedded_media_links(content_el, post_url) + _body_reference_links(content_el, post_url)
        attachments = self.parse_attachments(content_el, page_url=post_url, base_url=base_url,
                                             site_prefix=site_prefix)
        root = content_el.select_one(".bdvEdit") if content_el else None
        return {"slug": _slug(post_url, title), "title": title, "date": date, "url": post_url,
                "is_notice": item.get("is_notice", False), "body": body, "attachments": attachments,
                **({"reference_links": media_links} if media_links else {}),
                **_empty_content_info(root or content_el, body, attachments)}

    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "", faculty_tab_labels: bool = False,
    ) -> dict[str, Any]:
        breadcrumb = soup.select(".a_sbtNav dd")
        title = breadcrumb[-1].get_text(strip=True) if breadcrumb else fallback_title
        title = title or _text(soup.select_one("title")) or fallback_title
        content = _content_root(soup, base_url or page_url)
        text = extract_body_content(content, prefer_detail_body=False, faculty_tab_labels=faculty_tab_labels)
        attachments = self.parse_attachments(content, page_url=page_url, base_url=base_url, site_prefix=site_prefix)
        references = _embedded_media_links(content, page_url or base_url) + _body_reference_links(content, page_url or base_url)
        return {"title": title, "content": text, "attachments": attachments,
                **({'reference_links': references} if references else {}),
                **_empty_content_info(content, text, attachments)}
