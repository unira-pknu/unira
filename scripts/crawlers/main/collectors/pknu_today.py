"""Collect one list page of the three PKNU Today community boards."""

from __future__ import annotations
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path
from scripts.crawlers.main.common.board import ListItem, parse_detail_page
from scripts.crawlers.main.paths import main_root
from scripts.crawlers.main.routes import ROUTES
from urllib.parse import parse_qs, urljoin, urlparse
import argparse
import json
import re
import requests
import time


ROOT = Path(__file__).resolve().parents[4]
OUTPUT = main_root(ROOT)
BOARD_LABELS = {51: "부경투데이", 52: "교수동정", 53: "부경나우"}
REQUEST_TIMEOUT = 25


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _post_number(href: str) -> str | None:
    values = parse_qs(urlparse(href).query).get("no", [])
    return values[0] if values and values[0].isdigit() else None


def parse_first_page(html: str, page_id: int) -> list[dict[str, str]]:
    """Read only links in the selected board's list, never navigation links."""
    if page_id not in BOARD_LABELS:
        raise ValueError(f"unsupported PKNU Today page: {page_id}")
    soup = BeautifulSoup(html, "lxml")
    if page_id in (51, 52):
        anchors = soup.select('#subCont ul.bdWz > li > a[href*="action=view"]')
    else:
        anchors = soup.select('#subCont table.brdList tbody td.bdlTitle > a[href*="action=view"]')
    base_url = f"https://www.pknu.ac.kr/main/{page_id}"
    posts: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        number = _post_number(anchor.get("href", ""))
        if not number or number in seen:
            continue
        seen.add(number)
        title_el = anchor.select_one("h5") if page_id in (51, 52) else anchor
        posts.append({
            "post_no": number,
            "title": _clean(title_el.get_text(" ", strip=True)) if title_el else "",
            "url": urljoin(base_url, anchor["href"]),
        })
    return posts


def _media_links(body: BeautifulSoup, page_url: str) -> tuple[list[dict], list[dict]]:
    images: list[dict] = []
    links: list[dict] = []
    seen_images: set[str] = set()
    seen_links: set[str] = set()
    for image in body.select("img"):
        source = image.get("src") or image.get("data-src") or ""
        url = urljoin(page_url, source)
        if source and urlparse(url).scheme in {"http", "https"} and url not in seen_images:
            seen_images.add(url)
            images.append({"url": url, "alt": _clean(image.get("alt", ""))})
    for anchor in body.select("a[href]"):
        href = anchor.get("href", "")
        url = urljoin(page_url, href)
        if href and urlparse(url).scheme in {"http", "https"} and url not in seen_links:
            seen_links.add(url)
            links.append({"url": url, "text": _clean(anchor.get_text(" ", strip=True))})
    return images, links


def collect_first_page(
    session: requests.Session, page_id: int, *, output_root: Path = OUTPUT,
    request_delay: float = 0.3,
) -> dict:
    """Save the first list page's articles and a manifest for review."""
    if page_id not in BOARD_LABELS:
        raise ValueError(f"unsupported PKNU Today page: {page_id}")
    list_url = f"https://www.pknu.ac.kr/main/{page_id}"
    response = session.get(list_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = "utf-8"
    posts = parse_first_page(response.text, page_id)
    if not posts:
        raise ValueError(f"no articles found on {list_url} page 1")

    results: list[dict] = []
    for post in posts:
        if request_delay:
            time.sleep(request_delay)
        try:
            response = session.get(post["url"], timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = "utf-8"
            item = ListItem(
                no=post["post_no"], notice_no=None, is_notice=False,
                list_date="", pknu_cd=set(), categories={BOARD_LABELS[page_id]},
            )
            extracted = parse_detail_page(response.text, item)
            if not extracted or not extracted["title"] or not extracted["content"]:
                raise ValueError("article title or body is missing")
            soup = BeautifulSoup(response.text, "lxml")
            body = soup.select_one("#subCont div.bdvTxt")
            if body is None:
                raise ValueError("article body container is missing")
            images, links = _media_links(body, post["url"])
            document = {
                "source_site": "https://www.pknu.ac.kr",
                "category": "커뮤니티",
                "subcategory": BOARD_LABELS[page_id],
                "page_id": page_id,
                "post_no": post["post_no"],
                "url": post["url"],
                "title": _clean(extracted["title"]),
                "author": _clean(extracted["author"]),
                "date": extracted["date"],
                "content": _clean(extracted["content"]),
                "images": images,
                "links": links,
                "attachments": extracted["attachments"],
                "crawled_at": datetime.now(timezone.utc).isoformat(),
            }
            target = output_root.joinpath(*ROUTES[page_id].category_path) / "json" / "posts" / f'main_{page_id}_{post["post_no"]}.json'
            _write_json(target, document)
            results.append({"post_no": post["post_no"], "status": "completed",
                            "output": target.relative_to(ROOT).as_posix(),
                            "image_count": len(images), "attachment_count": len(extracted["attachments"])})
        except (requests.RequestException, ValueError) as exc:
            results.append({"post_no": post["post_no"], "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})

    saved = sum(result["status"] == "completed" for result in results)
    manifest = {
        "page_id": page_id,
        "category": BOARD_LABELS[page_id],
        "list_url": list_url,
        "page_index": 1,
        "discovered_count": len(posts),
        "saved_count": saved,
        "failed_count": len(posts) - saved,
        "status": "completed" if saved == len(posts) else "needs_review",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": results,
    }
    _write_json(output_root / "_runs" / "pknu_today" / f"main_{page_id}_page1.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-ids", type=int, nargs="+", choices=tuple(BOARD_LABELS),
                        default=list(BOARD_LABELS))
    args = parser.parse_args()
    with requests.Session() as session:
        session.headers.update({"User-Agent": "campus-rag/1.0 (+https://www.pknu.ac.kr)"})
        reports = [collect_first_page(session, page_id) for page_id in dict.fromkeys(args.page_ids)]
    print(json.dumps({"reports": [{key: value for key, value in report.items() if key != "items"}
                                  for report in reports]}, ensure_ascii=False, indent=2))
    return int(any(report["status"] != "completed" for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
