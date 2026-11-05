"""Reproduce the public site's normal report/export requests using HTTP only."""

from __future__ import annotations

import requests

from scripts.crawlers.departments.tls import mount_verified_legacy_tls
from scripts.crawlers.syllabus.catalog import ENDPOINT, HEADERS, SITE_URL, build_request, read_sections, tasks_from_rows
from scripts.crawlers.syllabus.protocol import (
    EMBED_URL, REPORT_BASE, ProtocolError, export_filename, parse_embed, report_form,
)


class AccessDenied(ProtocolError):
    """Stop on denied access; never attempt to bypass authentication."""


class ReportClient:
    def __init__(self, *, timeout: int = 120, max_bytes: int = 50 * 1024 * 1024):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": HEADERS["User-Agent"]})
        # Both university hosts currently require this negotiation policy in
        # Python 3.12. Certificate/hostname verification stays enabled.
        for host in ("https://irumi.pknu.ac.kr/", "https://reporting.pknu.ac.kr/"):
            mount_verified_legacy_tls(self.session, host)
        self.stage = "initial"
        self.http_status: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def request(self, stage: str, method: str, url: str, **kwargs) -> requests.Response:
        self.stage, self.http_status = stage, None
        response = self.session.request(
            method, url, timeout=(15, self.timeout), allow_redirects=False, **kwargs,
        )
        self.http_status = response.status_code
        if response.status_code in (401, 403):
            response.close()
            raise AccessDenied(f"{stage}: 접근 거부 HTTP {response.status_code}; 요청을 중단합니다.")
        if 300 <= response.status_code < 400:
            response.close()
            raise AccessDenied(f"{stage}: 리디렉션 응답; 로그인/접근 조건을 자동으로 우회하지 않습니다.")
        try:
            response.raise_for_status()
        except requests.RequestException:
            response.close()
            raise
        return response

    def list_tasks(self, year: int, term: str, course_no: str, variant: str) -> list[dict]:
        if type(year) is not int or year < 2021:
            raise ValueError("2021년 이후의 학년도를 입력하세요.")
        body = build_request(year, term, course_name=course_no)
        with self.request("catalog", "POST", ENDPOINT, headers=HEADERS, data=body.encode("utf-8")) as response:
            response.encoding = "utf-8"
            rows = read_sections(response.text)
        tasks, _ = tasks_from_rows(year, term, rows, [variant])
        # The site's subject search can return partial matches. Keep exact IDs.
        return [task for task in tasks if not course_no or task["course_no"] == course_no]

    def pdf(self, task: dict) -> bytes:
        # Only this client's newly established anonymous session is used.
        session_ids = [cookie.value for cookie in self.session.cookies
                       if cookie.name == "JSESSIONID" and cookie.domain.lstrip(".") == "irumi.pknu.ac.kr"]
        form = report_form(task, session_ids[0] if session_ids else "")
        with self.request(
            "embed", "POST", EMBED_URL, data=form,
            headers={"Referer": SITE_URL, "Origin": "https://irumi.pknu.ac.kr"},
        ) as response:
            response.encoding = "utf-8"
            document = parse_embed(response.text, task)
        with self.request(
            "export", "POST", document.service_url, data=document.export_form(),
            headers={"Referer": EMBED_URL, "X-Requested-With": "XMLHttpRequest"},
        ) as response:
            response.encoding = "utf-8"
            filename = export_filename(response.text)
            report_id = response.headers.get("crownix-rdid")
            if not report_id:
                raise ProtocolError("PDF 내보내기 응답에 crownix-rdid가 없습니다.")
        with self.request(
            "download", "GET", REPORT_BASE + "/download", stream=True,
            params={
                "crownix-client": "html5viewer", "filename": filename, "rdid": report_id,
                "delete": "true", "attatchment": "true",
            },
            headers={"Referer": EMBED_URL},
        ) as response:
            data = bytearray()
            for block in response.iter_content(64 * 1024):
                data.extend(block)
                if len(data) > self.max_bytes:
                    raise ProtocolError("PDF 다운로드 크기 제한을 초과했습니다.")
        if not data.startswith(b"%PDF-"):
            raise ProtocolError("다운로드 응답이 PDF가 아닙니다. HTML/오류 응답을 저장하지 않습니다.")
        return bytes(data)
