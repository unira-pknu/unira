"""Inventory bounded /main/<id> pages without changing crawler data or state.

Responses are checkpointed separately so reports can be rebuilt without requests.
Only HTML is inspected; linked pages, files, and images are not downloaded.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from lxml import etree, html as lxml_html

from scripts.crawlers.departments.urls import resolve_url

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BASE_URL = "https://www.pknu.ac.kr"
USER_AGENT = "campus-rag-page-inventory/1.0"
DEFAULT_OUTPUT = PROJECT_ROOT / "files/pknu_main/_discovery"
ERROR_TEXT = re.compile(r"페이지\s*(?:를|가)?\s*(?:찾을\s*수\s*없|존재하지\s*않)|잘못된\s*(?:접근|요청)|요청하신\s*페이지.*(?:없|오류)|page\s+not\s+found", re.I)
DENY_TEXT = re.compile(r"접근\s*(?:권한이\s*없|이\s*거부|할\s*수\s*없|금지)|비정상적인\s*접근|access\s+denied|request\s+blocked", re.I)
FILE_EXT = re.compile(r"\.(?:pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|txt)(?:$|[?#])", re.I)
REMOVE = "script,style,noscript,header,footer,nav,.subMenu,.subTitle,.edtDay,.paging,.brdSch,.brdAll,.brdBtn,.bdvNav,.c_bdvNav,.share,.sns"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_captures(output: Path) -> dict[int, dict]:
    path = output / "captures.jsonl"
    if not path.is_file():
        return {}
    # Ignore an interrupted final line; every complete response remains usable.
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows[row["page_id"]] = row
    return rows


class Client:
    def __init__(self, delay: float, timeout: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9", "Accept-Language": "ko"})
        self.delay, self.timeout, self.last_request = delay, timeout, 0.0
        self.robots: RobotFileParser | None = None

    def get(self, url: str) -> requests.Response:
        if self.robots is not None and not self.robots.can_fetch(USER_AGENT, url):
            raise PermissionError("ROBOTS_DISALLOWED")
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.session.get(url, timeout=(8, self.timeout), allow_redirects=False, stream=True)
        response.encoding = "utf-8"
        return response

    def prepare(self, output: Path) -> dict:
        robots = self.get(BASE_URL + "/robots.txt")
        if robots.status_code == 200 and "<html" not in robots.text.lower():
            parser = RobotFileParser()
            parser.parse(robots.text.splitlines())
            self.robots = parser
            self.delay = max(self.delay, parser.crawl_delay(USER_AGENT) or 0)
            rate = parser.request_rate(USER_AGENT)
            if rate and rate.requests:
                self.delay = max(self.delay, rate.seconds / rate.requests)
        elif robots.status_code not in {404, 410}:
            raise RuntimeError(f"robots.txt could not be verified (HTTP {robots.status_code})")
        (output / "robots.txt").write_text(robots.text, encoding="utf-8")
        root = self.get(BASE_URL + "/main")
        root.raise_for_status()
        if not root.text.strip():
            raise RuntimeError("The homepage response is empty; no scan was started.")
        soup = BeautifulSoup(root.text, "lxml")
        menus: dict[str, list[str]] = {}
        for anchor in soup.select("a[href]"):
            try:
                target = resolve_url(anchor.get("href"), root.url)
            except ValueError:
                continue
            parsed = urlsplit(target)
            match = re.fullmatch(r"/main/(\d+)/?", parsed.path)
            if parsed.hostname != "www.pknu.ac.kr" or not match:
                continue
            label = anchor.get_text(" ", strip=True) or anchor.get("title") or ""
            labels = menus.setdefault(match[1], [])
            if label and label not in labels:
                labels.append(label)
        if not menus:
            raise RuntimeError("No numeric menu links found; specify and verify the site before scanning.")
        return {"seed_url": root.url, "homepage_menu_max_id": max(map(int, menus)),
                "menu_labels": menus, "robots_status": robots.status_code,
                "robots_checked_at": now(), "delay_seconds": self.delay}

    def capture(self, page_id: int, output: Path) -> dict:
        requested = f"{BASE_URL}/main/{page_id}"
        row = {"page_id": page_id, "requested_url": requested, "final_url": requested,
               "checked_at": now(), "http_status": None, "redirects": [], "retryable": False,
               "attempts": 0, "raw_path": None, "error": None}
        current, response = requested, None
        visited = set()
        try:
            for _ in range(4):
                if current in visited:
                    row["error"] = "REDIRECT_LOOP"
                    break
                visited.add(current)
                for attempt in range(2):
                    row["attempts"] += 1
                    try:
                        response = self.get(current)
                    except requests.exceptions.SSLError:
                        raise
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                        if attempt:
                            raise
                        time.sleep(1)
                        continue
                    if response.status_code not in {429, 500, 502, 503, 504} or attempt:
                        break
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after and (not retry_after.isdigit() or int(retry_after) > 30):
                        break
                    response.close()
                    time.sleep(max(1, int(retry_after or "1")))
                row.update(final_url=current, http_status=response.status_code,
                           content_type=response.headers.get("Content-Type", ""))
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                target = resolve_url(response.headers.get("Location"), current)
                row["redirects"].append({"from": current, "to": target, "http_status": response.status_code})
                parsed = urlsplit(target)
                if (parsed.scheme != "https" or parsed.hostname != "www.pknu.ac.kr"
                        or not re.fullmatch(r"/main(?:/\d+)?/?", parsed.path)):
                    row["destination_url"] = target
                    break
                if self.robots is not None and not self.robots.can_fetch(USER_AGENT, target):
                    row["destination_url"] = target
                    row["error"] = "ROBOTS_DISALLOWED_REDIRECT"
                    break
                response.close()
                current = target
            else:
                row["error"] = "REDIRECT_LIMIT"
            if response is not None:
                if response.status_code in {429, 500, 502, 503, 504}:
                    row["retryable"] = True
                mime = response.headers.get("Content-Type", "").lower()
                if not mime or mime.startswith("text/") or "application/xhtml+xml" in mime:
                    raw = output / "raw" / f"{page_id:04d}.txt"
                    raw.parent.mkdir(exist_ok=True)
                    raw.write_text(response.text, encoding="utf-8")
                    row["raw_path"] = raw.relative_to(output).as_posix()
                    row["response_bytes"] = len(response.content)
                    row["body_downloaded"] = True
                else:
                    row["body_downloaded"] = False
                response.close()
        except PermissionError as exc:
            row["error"] = str(exc)
        except (requests.RequestException, ValueError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["retryable"] = isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) and not isinstance(exc, requests.exceptions.SSLError)
        return row


def immediate_destination(soup: BeautifulSoup, url: str) -> str | None:
    refresh = soup.select_one('meta[http-equiv="refresh" i]')
    match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", str(refresh.get("content", "")), re.I) if refresh else None
    if match:
        try:
            return resolve_url(match[1].strip(), url)
        except ValueError:
            return None
    # Never treat the site's shared session-timeout functions as an immediate redirect.
    if soup.select_one("#subCont") is None:
        for script in soup.select("script:not([src])"):
            code = script.get_text().strip()
            if len(code) > 1000 or re.search(r"\bfunction\b|\.ready\s*\(|addEventListener", code):
                continue
            match = re.search(r"(?:window\.|top\.|parent\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", code)
            if match:
                try:
                    return resolve_url(match[1], url)
                except ValueError:
                    pass
    return None


def inventory_soup(raw: str) -> BeautifulSoup:
    """Parse only inventory regions, avoiding repeated parsing of the huge menu."""
    if not raw.strip():
        return BeautifulSoup('', 'lxml')
    try:
        tree = lxml_html.fromstring(raw)
    except (etree.ParserError, ValueError):
        return BeautifulSoup(raw, 'lxml')
    nodes = tree.xpath('//title | //meta[translate(@http-equiv,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="refresh"] | //*[@id="subNav"] | //*[@id="subCont"]')
    legacy = []
    if not tree.xpath('//*[@id="subCont"]'):
        legacy = tree.xpath('//*[@id="rule_container"] | //*[contains(concat(" ",normalize-space(@class)," ")," campusMap_conts ")] | //div[contains(concat(" ",normalize-space(@class)," ")," cont ")][.//*[contains(concat(" ",normalize-space(@class)," ")," board-w ")]]')
        nodes.extend(legacy)
    parts = [etree.tostring(node, encoding='unicode', method='html', with_tail=False) for node in nodes]
    if not tree.xpath('//*[@id="subCont"]'):
        if tree.xpath('//*[@id="wrap"]'):
            parts.append('<div id="wrap"></div>')
        if not legacy:
            visible = tree.xpath('//body//text()[not(ancestor::script) and not(ancestor::style)]')
            parts.append('<div>' + html.escape(' '.join(visible)) + '</div>')
        for script in tree.xpath('//script[not(@src)]'):
            code = script.text_content()
            if len(code.strip()) <= 1000:
                parts.append(etree.tostring(script, encoding='unicode', method='html', with_tail=False))
    return BeautifulSoup(''.join(parts), 'lxml')


def classify_capture(capture: dict, raw: str, menu_labels: dict) -> dict:
    row = {**capture, "status": "needs_review", "page_type": "unknown", "title": "",
           "menu_labels": menu_labels.get(str(capture["page_id"]), []), "breadcrumb": [],
           "content_summary": "", "text_length": 0, "table_count": 0, "image_count": 0,
           "attachment_candidates": [], "iframe_urls": [], "content_links": [],
           "warnings": [], "evidence": [], "content_sha256": None, "duplicate_of": None}
    status = capture.get("http_status")
    if capture.get("error"):
        row["status"] = "robots_excluded" if "ROBOTS_DISALLOWED" in capture["error"] else "request_failed"
        row["evidence"].append(capture["error"])
        return row
    if status in {404, 410}:
        row.update(status="not_found", evidence=[f"HTTP_{status}"])
        return row
    if status in {401, 403} or "/deny.jsp" in capture.get("destination_url", ""):
        row.update(status="access_blocked", evidence=[f"HTTP_{status}_OR_DENY_DESTINATION"])
        return row
    if status is None or status >= 400:
        row.update(status="http_error", evidence=[f"HTTP_{status}"])
        return row
    mime = capture.get("content_type", "").lower()
    if mime and not mime.startswith("text/") and "application/xhtml+xml" not in mime:
        if re.search(r"application/(?:pdf|octet-stream|zip|msword|vnd\.)|image/|audio/|video/", mime):
            row.update(status="link_only", page_type="file_endpoint", title=row['menu_labels'][0] if row['menu_labels'] else '파일 응답', evidence=["NON_HTML_FILE_RESPONSE"], warnings=["FILE_RESPONSE_NOT_INSPECTED"])
        else:
            row["warnings"].append("NON_HTML_RESPONSE_NOT_INSPECTED")
        return row
    soup = inventory_soup(raw)
    row["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
    if capture.get("destination_url"):
        row.update(status="link_only", page_type="redirect", evidence=["HTTP_REDIRECT_NOT_FETCHED"])
        row['title'] = row['menu_labels'][0] if row['menu_labels'] else '이동 링크'
        row['content_summary'] = '연결 대상: ' + capture['destination_url'] + ' (목적지 내용은 수집하지 않음)'
        if re.search(r'/login(?:[/?#]|$)', capture['destination_url'], re.I):
            row['warnings'].append('LOGIN_DESTINATION_NOT_FETCHED')
        return row
    if not raw.strip():
        row["status"] = "needs_review" if row["menu_labels"] else "empty_response"
        row["warnings"].append("EMPTY_HTTP_200_RESPONSE")
        row["evidence"].append("HTTP_200_WITHOUT_HTML_BODY: page existence is not proven")
        return row
    # These are immediate denial pages, not the shared session-timeout handlers.
    for script in soup.select('script:not([src])'):
        code = script.get_text().strip()
        if len(code) > 1000 or re.search(r'\bfunction\b|\.ready\s*\(|addEventListener', code):
            continue
        alerts = ' '.join(re.findall(r"alert\s*\(\s*['\"]([^'\"]+)", code))
        if DENY_TEXT.search(alerts) and re.search(r'history\.(?:go\s*\(\s*-1|back\s*\()', code):
            row.update(status='access_blocked', content_summary=alerts,
                       evidence=['IMMEDIATE_ACCESS_DENIAL_ALERT'],
                       warnings=['ACCESS_DENIAL_CAUSE_UNVERIFIED'])
            return row
    for dt in soup.select("#subNav dt"):
        label = dt.get_text(" ", strip=True)
        if label and label not in row["breadcrumb"]:
            row["breadcrumb"].append(label)
    container = soup.select_one("#subCont")
    if container is None:
        container = soup.select_one('#rule_container,.campusMap_conts,.cont:has(.board-w)')
        if container is not None:
            row['evidence'].append('KNOWN_LEGACY_CONTENT_CONTAINER')
    head = container.select_one(".subTitle") if container else None
    if head and head.get_text(strip=True):
        row["title"] = head.get_text(" ", strip=True)
    destination = immediate_destination(soup, capture["final_url"])
    if destination:
        row.update(status="link_only", page_type="redirect", destination_url=destination,
                   evidence=["HTML_OR_SCRIPT_REDIRECT_NOT_FETCHED"])
        row['content_summary'] = '연결 대상: ' + destination + ' (목적지 내용은 수집하지 않음)'
        alerts = ' '.join(re.findall(r"alert\s*\(\s*['\"]([^'\"]+)", raw))
        if ERROR_TEXT.search(alerts) or ERROR_TEXT.search(row['title']):
            row.update(status="not_found", evidence=["ERROR_ALERT_WITH_REDIRECT"])
        elif re.search(r"/login|/main/49(?:[/?#]|$)|/deny.jsp", destination) and re.search(r"로그인|접근|권한", soup.get_text(" ", strip=True) + alerts):
            row["status"] = "access_blocked"
        elif urlsplit(destination).hostname == 'www.pknu.ac.kr' and urlsplit(destination).path.rstrip('/') in {'', '/main', '/main/1'}:
            row.update(status="needs_review", warnings=["HOMEPAGE_REDIRECT_UNVERIFIED"])
        return row
    if container is None:
        if re.search(r"Pukyong National University", row["title"], re.I) and soup.select_one("#wrap"):
            row.update(status="exists", page_type="homepage", evidence=["MAIN_HOMEPAGE_TEMPLATE"], warnings=["HOMEPAGE_ALIAS"])
            if capture.get('redirects'):
                row.update(status="needs_review", warnings=["HOMEPAGE_REDIRECT_UNVERIFIED"])
        else:
            body = soup.get_text(" ", strip=True)
            if DENY_TEXT.search(row["title"] + " " + body):
                row.update(status="access_blocked", evidence=["ACCESS_DENIAL_TEXT"])
            elif ERROR_TEXT.search(row["title"] + " " + body):
                row.update(status="not_found", evidence=["ERROR_PAGE_TEXT"])
            else:
                row["warnings"].append("CONTENT_CONTAINER_NOT_FOUND")
                row["content_summary"] = body[:500]
        return row
    body = BeautifulSoup(str(container), "lxml")
    is_board = bool(body.select_one(".brdList,.board_list,.board-list,[name=bbsId],.board-w,#tbl_contents"))
    for element in body.select(REMOVE):
        element.decompose()
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
    if DENY_TEXT.search(row["title"]) or (len(text) < 300 and DENY_TEXT.search(text)):
        row.update(status="access_blocked", evidence=["ACCESS_DENIAL_IN_CONTENT"])
        return row
    if ERROR_TEXT.search(row["title"]) or (not row['title'] and len(text) < 100 and ERROR_TEXT.search(text)):
        row.update(status="not_found", evidence=["ERROR_IN_CONTENT"])
        return row
    row["text_length"], row["content_summary"] = len(text), text[:600]
    row["table_count"] = len(body.select("table"))
    images = []
    for image in body.select("img[src],img[data-src]"):
        src = str(image.get("data-src") or image.get("src") or "")
        if re.search(r"(?:icon|board_file|logo|share|sns|button)", src, re.I):
            continue
        images.append(src)
    row["image_count"] = len(images)
    seen_links = set()
    for anchor in body.select("a[href]"):
        try:
            target = resolve_url(anchor.get("href"), capture["final_url"])
        except ValueError:
            continue
        if target in seen_links:
            continue
        seen_links.add(target)
        item = {"text": anchor.get_text(" ", strip=True), "url": target}
        row["content_links"].append(item)
        if FILE_EXT.search(target) or anchor.has_attr("download") or re.search(r"(?:boardDownload|getMdaId|fileDownload)\.do", target, re.I):
            row["attachment_candidates"].append({**item, "verified_download": False})
    for pdf in body.select(".uploadPdf[data-id]"):
        row["attachment_candidates"].append({"media_id": str(pdf["data-id"]), "kind": "embedded_pdf", "verified_download": False})
    for iframe in body.select("iframe[src]"):
        try:
            row["iframe_urls"].append(resolve_url(iframe["src"], capture["final_url"]))
        except ValueError:
            pass
    is_map = bool(body.select_one('#kMap,.campusMap_conts'))
    dynamic = bool(body.select_one(".getOrgMngList,[data-id].uploadPdf,#calendar,#direction_daeyeon,#direction_yongdang,#loadArea")) or is_map
    if dynamic or row["iframe_urls"]:
        row["warnings"].append("DYNAMIC_OR_EMBEDDED_CONTENT_NOT_FETCHED")
    if is_board:
        page_type = "board"
    elif is_map:
        page_type = "embedded_content"
    elif not text and images:
        page_type = "image_only"
        row["warnings"].append("IMAGE_ONLY_REQUIRES_OCR")
    elif row["attachment_candidates"] and len(text) < 80:
        page_type = "attachment_only"
    elif row["table_count"]:
        page_type = "table_page"
    elif (row["iframe_urls"] or dynamic) and len(text) < 80:
        page_type = "embedded_content"
    elif row["content_links"] and len(text) < 120:
        page_type = "link_hub"
    else:
        page_type = "static_page"
    if text or images or row["attachment_candidates"] or row["iframe_urls"] or is_board or dynamic or row["content_links"]:
        row.update(status="exists", page_type=page_type)
        row['evidence'].append('VERIFIED_CONTENT_CONTAINER')
        signature = json.dumps([text, images, row["content_links"], row["attachment_candidates"], row["iframe_urls"]], ensure_ascii=False, sort_keys=True)
        row["content_sha256"] = hashlib.sha256(signature.encode()).hexdigest()
    else:
        row["warnings"].append("EMPTY_CONTENT_CONTAINER")
    return row


def render_reports(output: Path, metadata: dict, captures: dict[int, dict]) -> dict:
    rows = []
    fingerprints: dict[str, int] = {}
    for page_id, capture in sorted(captures.items()):
        if not metadata["start_id"] <= page_id <= metadata["max_id"]:
            continue
        path = output / capture["raw_path"] if capture.get("raw_path") else None
        raw = path.read_text(encoding="utf-8") if path and path.is_file() else ""
        row = classify_capture(capture, raw, metadata["menu_labels"])
        if row["content_sha256"]:
            if row["content_sha256"] in fingerprints:
                row["duplicate_of"] = fingerprints[row["content_sha256"]]
            else:
                fingerprints[row["content_sha256"]] = page_id
        rows.append(row)
        if len(rows) % 100 == 0:
            print(json.dumps({'stage':'classifying_saved_responses','classified':len(rows)},ensure_ascii=False),flush=True)
    valid = [row for row in rows if row["status"] in {"exists", "link_only"}]
    path = output / "page_inventory.jsonl"
    tmp = output / "page_inventory.jsonl.part"
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)
    summary = {**{k:v for k,v in metadata.items() if k != "menu_labels"},
               "generated_at": now(), "requested_pages": metadata["max_id"]-metadata["start_id"]+1,
               "checked_pages": len(rows), "complete": len(rows) == metadata["max_id"]-metadata["start_id"]+1,
               "status_counts": dict(Counter(row["status"] for row in rows)),
               "page_type_counts": dict(Counter(row["page_type"] for row in valid)),
               "existing_or_link_pages": len(valid), "duplicate_pages": sum(row["duplicate_of"] is not None for row in rows),
               "out_of_range_discovered_ids": sorted({int(m.group(1)) for row in rows for link in row["content_links"]
                   if (m := re.fullmatch(r"/main/(\d+)/?", urlsplit(link["url"]).path)) and int(m.group(1)) > metadata["max_id"]
                   and urlsplit(link["url"]).hostname == "www.pknu.ac.kr"})}
    write_json(output / "page_inventory.summary.json", summary)
    from scripts.crawlers.main.discovery.export_page_catalog import build_catalog
    write_json(output / "page_catalog.json", build_catalog(rows, summary, output))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-id', type=int, help='Default: largest /main/<id> link in the homepage')
    parser.add_argument('--start-id', type=int, default=1)
    parser.add_argument('--delay', type=float, default=0.8, help='Minimum interval between serial requests')
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--render-only', action='store_true', help='Rebuild JSONL/HTML from saved responses without requests')
    parser.add_argument('--retry-failed', action='store_true', help='Retry only saved transient failures; leave successful responses unchanged')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata_path = output / 'scan.json'
    captures = read_captures(output)
    if args.render_only:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        print(json.dumps(render_reports(output, metadata, captures), ensure_ascii=False), flush=True)
        return
    client = Client(max(0.1, args.delay), args.timeout)
    metadata = client.prepare(output)
    max_id = args.max_id or metadata['homepage_menu_max_id']
    if not 1 <= args.start_id <= max_id <= 10000:
        parser.error('Require 1 <= start-id <= max-id <= 10000')
    metadata.update(start_id=args.start_id, max_id=max_id, started_at=now(),
                    scope='Numeric main-page routes only; not exhaustive website coverage')
    write_json(metadata_path, metadata)
    print(json.dumps({'start_id':args.start_id,'max_id':max_id,'menu_max_id':metadata['homepage_menu_max_id'],'output':str(output)}, ensure_ascii=False), flush=True)
    consecutive_blocks = 0
    last_progress = time.monotonic()
    try:
        with (output / 'captures.jsonl').open('a', encoding='utf-8', buffering=1) as handle:
            for page_id in range(args.start_id, max_id+1):
                if page_id in captures and not (args.retry_failed and captures[page_id].get('retryable')):
                    continue
                capture = client.capture(page_id, output)
                handle.write(json.dumps(capture, ensure_ascii=False)+'\n')
                handle.flush()
                captures[page_id] = capture
                if capture['http_status'] in {401,403}:
                    consecutive_blocks += 1
                else:
                    consecutive_blocks = 0
                if page_id % 25 == 0 or time.monotonic()-last_progress >= 30:
                    print(json.dumps({'checked':len(captures),'last_id':page_id,'max_id':max_id,'http_status':capture['http_status']}, ensure_ascii=False), flush=True)
                    last_progress = time.monotonic()
                if capture['http_status'] == 429 or consecutive_blocks >= 3:
                    metadata['stopped_reason'] = 'RATE_LIMIT_OR_REPEATED_ACCESS_BLOCK'
                    break
    finally:
        write_json(metadata_path, metadata)
        print(json.dumps(render_reports(output, metadata, captures), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
