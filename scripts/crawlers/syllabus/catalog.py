"""Enumerate public offerings for direct HTTP report downloads."""

from __future__ import annotations

import hashlib
import html
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from scripts.crawlers.departments.tls import mount_verified_legacy_tls

BASE_URL = "https://irumi.pknu.ac.kr"
ENDPOINT = BASE_URL + "/ezworks/noSessionController"
SQL_ID = "LtPdocInq.findLtPdocInq"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (campus-rag course list; PKNU capstone)",
    "ezclienttype": "nexacro17",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/xml, text/xml, */*",
    "Content-Type": "text/xml",
}
SITE_URL = (
    "https://irumi.pknu.ac.kr/nxui/launch.html"
    "?screenid=Desktop_screen_quick&menuId=U020913"
)
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "files" / "pknu_syllabus"
TERMS = {
    "1": ("U0003001", "1학기"),
    "summer": ("U0003003", "여름학기"),
    "2": ("U0003002", "2학기"),
    "winter": ("U0003004", "겨울학기"),
}
VARIANTS = {"kor": "KOR_YN", "eng": "ENG_YN", "tech": "TECH_YN"}


def recent_years(end_year: int | None = None) -> list[int]:
    end_year = end_year or datetime.now(timezone(timedelta(hours=9))).year
    return list(range(end_year - 2, end_year + 1))


def build_request(year: int, term: str, *, course_name: str = "", staff_name: str = "") -> str:
    params = {
        "_sqlId": SQL_ID, "_method": "getList",
        "YY": str(year), "SHTM_CD": TERMS[term][0],
        "UNIV_GRSC_CLSF": "", "COLG_CD": "", "OPEN_SUST_MJ_CD": "",
        "SBJT_NM": course_name, "STAFF_NM": staff_name, "SBJ_FG": "",
    }
    parameters = "".join(
        f'<Parameter id="{key}">{html.escape(value)}</Parameter>' for key, value in params.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Root xmlns="http://www.nexacroplatform.com/platform/dataset">'
        f"<Parameters>{parameters}</Parameters>"
        '<Dataset id="__DS_TRANS_INFO__"><ColumnInfo>'
        '<Column id="strAction" type="string" size="256" />'
        '<Column id="strSvcID" type="string" size="256" />'
        '<Column id="strURL" type="string" size="256" />'
        "</ColumnInfo><Rows><Row>"
        '<Col id="strAction">uni</Col>'
        '<Col id="strSvcID">fn_search[^]1[^]getList</Col>'
        f'<Col id="strURL">{ENDPOINT}</Col>'
        "</Row></Rows></Dataset></Root>"
    )


def request_body(year: int, term: str) -> bytes:
    return build_request(year, term).encode("utf-8")


def read_sections(xml: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml)
    nodes = list(root.iter())
    errors = [node for node in nodes if node.tag.rsplit("}", 1)[-1] == "Parameter" and node.get("id") == "ErrorCode"]
    if len(errors) != 1 or errors[0].text != "0":
        raise ValueError("목록 서버가 오류를 반환했습니다. 빈 학기로 간주하지 않습니다.")
    datasets = [node for node in nodes if node.tag.rsplit("}", 1)[-1] == "Dataset" and node.get("id") == "output1"]
    if len(datasets) != 1:
        raise ValueError("목록 데이터셋 output1이 없습니다. 빈 학기로 간주하지 않습니다.")
    return [
        {col.attrib["id"]: (col.text or "").strip() for col in row if col.tag.rsplit("}", 1)[-1] == "Col"}
        for row in datasets[0].iter() if row.tag.rsplit("}", 1)[-1] == "Row"
    ]


def task_id(task: dict) -> str:
    identity = {key: task[key] for key in (
        "year", "term", "course_no", "section", "staff_no", "department", "variant",
    )}
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def tasks_from_rows(year: int, term: str, rows: list[dict], variants: list[str]) -> tuple[list, list]:
    tasks, missing = [], []
    seen = set()
    for row in rows:
        for key in ("COURSE_NO", "DCLSS_NO", "SBJT_KOR_NM", "UNIV_GRSC_CLSF"):
            if not row.get(key):
                raise ValueError(f"목록 필수 값 {key} 누락: {row!r}")
        if row.get("YY", str(year)) != str(year) or row.get("SHTM_CD", TERMS[term][0]) != TERMS[term][0]:
            raise ValueError("요청 연도·학기와 목록 응답이 다릅니다.")
        base = {
            "year": year, "term": term, "term_code": TERMS[term][0],
            "course_no": row["COURSE_NO"], "section": row["DCLSS_NO"],
            "course_name": row["SBJT_KOR_NM"], "staff_no": row.get("STAFF_NO", ""),
            "staff_name": row.get("STAFF_NM", ""), "level": row["UNIV_GRSC_CLSF"],
            "department": row.get("DEPT_NM", ""),
        }
        if not any(row.get(flag) == "Y" for flag in VARIANTS.values()):
            missing.append({**base, "reason": "no_published_syllabus"})
        for variant in variants:
            if row.get(VARIANTS[variant]) != "Y":
                continue
            task = {**base, "variant": variant}
            task["id"] = task_id(task)
            if task["id"] in seen:
                raise ValueError(f"동일 식별자의 중복 강의: {task['id']}")
            seen.add(task["id"])
            tasks.append(task)
    return tasks, missing


def collect_catalog(years: list[int], terms: list[str], variants: list[str]) -> dict:
    tasks, missing, batches = [], [], []
    with requests.Session() as session:
        session.headers.update(HEADERS)
        # irumi currently requires legacy cipher negotiation with Python 3.12.
        # Scope the existing verified adapter to this exact HTTPS host only.
        mount_verified_legacy_tls(session, ENDPOINT)
        for year in years:
            for term in terms:
                response = session.post(ENDPOINT, data=request_body(year, term), timeout=(15, 120))
                response.raise_for_status()
                rows = read_sections(response.text)
                batch_tasks, unpublished = tasks_from_rows(year, term, rows, variants)
                tasks.extend(batch_tasks)
                missing.extend(unpublished)
                batches.append({"year": year, "term": term, "sections": len(rows), "tasks": len(batch_tasks)})
                print(f"{year} {TERMS[term][1]}: 강의 {len(rows)}, 계획서 {len(batch_tasks)}", flush=True)
                time.sleep(1)
    return {
        "version": 1, "source_url": SITE_URL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "years": years, "terms": terms, "variants": variants,
        "batches": batches, "tasks": tasks, "unpublished": missing,
    }


def load_catalog(path: Path) -> dict:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("version") != 1 or not isinstance(catalog.get("tasks"), list):
        raise ValueError("지원하지 않는 목록 파일입니다.")
    ids = set()
    for task in catalog["tasks"]:
        if type(task.get("year")) is not int or task["year"] < 2021:
            raise ValueError("목록의 연도는 2021 이상의 정수여야 합니다.")
        if any(not isinstance(task.get(key), str) or not task[key] for key in (
            "course_no", "section", "course_name", "level",
        )):
            raise ValueError("목록의 강의 식별정보가 올바르지 않습니다.")
        if task["term"] not in TERMS or task["variant"] not in VARIANTS or task["id"] != task_id(task):
            raise ValueError("목록 작업 식별자가 올바르지 않습니다.")
        if task.get("term_code") != TERMS[task["term"]][0]:
            raise ValueError("목록의 학기 코드가 일치하지 않습니다.")
        if task["id"] in ids:
            raise ValueError("목록에 중복 작업이 있습니다.")
        ids.add(task["id"])
    return catalog
