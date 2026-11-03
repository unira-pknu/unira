'Shared CMS board list and detail parsing.'
from __future__ import annotations
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from pathlib import Path
from scripts.main.common.attachments import save_attachments
from scripts.main.common.storage import BASE_URL
from typing import Any
from urllib.parse import urljoin, urlparse
import re


_NAV_PHRASE_RE = re.compile(
    r"(다음\s*게시글이\s*없습니다\.?|이전\s*게시글이\s*없습니다\.?)",
    re.IGNORECASE,
)


@dataclass
class ListItem:
    no: str
    notice_no: int | None
    is_notice: bool
    list_date: str
    pknu_cd: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)


def parse_page_indicator(html: str) -> tuple[int, int]:
    m = re.search(r"<span>(\d+)</span>\s*/\s*(\d+)", html)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1, 1


def parse_list_page(html: str, cd: str, label: str) -> list[ListItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[ListItem] = []
    rows = soup.select("table.brdList tbody tr")
    if not rows:
        rows = soup.select("ul.bdWz > li, ul.gryList > li")
    for tr in rows:
        link = tr.select_one('a[href*="action=view"]')
        if not link:
            continue
        m = re.search(r"[?&]no=(\d+)", link.get("href", ""))
        if not m:
            continue
        post_no = m.group(1)

        num_el = tr.select_one("td.bdlNum")
        num_text = num_el.get_text(strip=True) if num_el else ""
        # 학교 공지 게시판은 일반 글 번호 칸에도 class="noti"를 붙인다(2026-11-02 확인:
        # 24행 모두 'bdlNum noti', 일반 글은 숫자). 그래서 class는 번호가 숫자가 아닐 때(아이콘 등)만 본다.
        is_notice = num_text.upper() == "NOTICE" or bool(
            num_el and "noti" in (num_el.get("class") or []) and not num_text.isdigit()
        )
        notice_no: int | None = None
        if not is_notice:
            try:
                notice_no = int(num_text)
            except ValueError:
                pass

        date_el = tr.select_one("td.bdlDate")
        list_date = date_el.get_text(strip=True) if date_el else ""
        if not list_date:
            card_meta = tr.select_one("dd > span")
            match = re.search(r"\d{4}-\d{2}-\d{2}", card_meta.get_text(" ", strip=True)) if card_meta else None
            list_date = match.group() if match else ""

        item = ListItem(
            no=post_no,
            notice_no=notice_no,
            is_notice=is_notice,
            list_date=list_date,
            pknu_cd={cd},
            categories={label},
        )
        items.append(item)
    return items


def extract_body_text(content_el) -> str:
    if content_el is None:
        return ""
    soup_copy = BeautifulSoup(str(content_el), "lxml")
    for el in soup_copy.select(".bdvNav, .brdBtn, .c_bdvNav, .c_bdvBtn"):
        el.decompose()
    text = soup_copy.get_text(" ", strip=True)
    text = _NAV_PHRASE_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_detail_page(html: str, list_item: ListItem) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("tr.first_noti td.title_b") or soup.select_one("td.title_b")
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    author = ""
    author_el = soup.select_one("td.text_l.noti_name")
    if author_el:
        author = author_el.get_text(strip=True)
    else:
        for tr in soup.select("tr.noti"):
            tds = tr.find_all("td")
            for i, td in enumerate(tds):
                if "작성자" in td.get_text():
                    if i + 1 < len(tds):
                        author = tds[i + 1].get_text(strip=True)
                    break

    date = list_item.list_date
    for tr in soup.select("tr.noti"):
        tds = tr.find_all("td")
        for i, td in enumerate(tds):
            if "작성일" in td.get_text():
                if i + 1 < len(tds):
                    date = tds[i + 1].get_text(strip=True)
                break
        if date:
            break
    if not date:
        m = re.search(r"\d{4}-\d{2}-\d{2}", soup.get_text(" ", strip=True))
        if m:
            date = m.group()

    body_el = soup.select_one("div.bdvTxt")
    content = extract_body_text(body_el)

    attachments: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    root = soup.select_one("div.bdCont") or soup.body
    if root:
        for a in root.select('a[href*="boardDownload.do"]'):
            href = a.get("href", "").strip()
            if not href or href.startswith("javascript"):
                continue
            abs_url = urljoin(BASE_URL, href)
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)
            name = a.get_text(strip=True) or Path(urlparse(abs_url).path).name
            attachments.append({"name": name, "url": abs_url})

    return {
        "title": title,
        "author": author,
        "date": date,
        "content": content,
        "attachments": attachments,
    }
