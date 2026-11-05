"""Run from the repository root: python -m scripts.crawlers.syllabus crawl --year 2026 --term 2."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.crawlers.syllabus.catalog import (
    DEFAULT_OUTPUT, ROOT, SITE_URL, TERMS, VARIANTS, collect_catalog, load_catalog, recent_years,
)
from scripts.crawlers.syllabus.client import AccessDenied, ReportClient
from scripts.crawlers.syllabus.protocol import EMBED_URL, REPORT_BASE
from scripts.crawlers.syllabus.storage import (
    atomic_json, collection_run, commit_progress, exclusive_run, is_complete, load_state,
    progress_scope, save_pdf,
)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")
    return number


def nonnegative(value: str) -> float:
    number = float(value)
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("0 이상의 유한한 수가 필요합니다.")
    return number


def prepare_crawl(args: argparse.Namespace) -> dict:
    """Reuse the scoped public catalog for direct HTTP downloads."""
    if args.year < 2021:
        raise ValueError("이 저장기는 2021년 이후의 계획서를 대상으로 합니다.")
    args.manifest = args.manifest or args.output.resolve() / "_state" / f"catalog-{args.year}-{args.term}.json"
    with exclusive_run(args.manifest.with_suffix(".lock")):
        if args.manifest.exists() and not args.refresh_catalog:
            catalog = load_catalog(args.manifest)
            print(f"저장된 목록 재사용: {args.manifest}", flush=True)
        else:
            catalog = collect_catalog([args.year], [args.term], list(dict.fromkeys(args.variants)))
            atomic_json(args.manifest, catalog)
            print(f"목록 저장: {args.manifest}", flush=True)
    if (
        catalog.get("years") != [args.year] or catalog.get("terms") != [args.term]
        or not set(args.variants).issubset(catalog.get("variants", []))
    ):
        raise ValueError("저장된 목록의 범위가 요청과 다릅니다. --refresh-catalog로 목록을 갱신하세요.")
    return catalog


def selected_tasks(catalog: dict, args: argparse.Namespace) -> list[dict]:
    return [task for task in catalog["tasks"]
            if task["variant"] in args.variants
            and (not args.course_no or task["course_no"] == args.course_no)
            and (not args.section or task["section"] == args.section)]


def record_error(state_path: Path, exc: Exception, *, task=None, client=None, scope="unknown"):
    response = getattr(exc, "response", None)
    status = client.http_status if client else getattr(response, "status_code", None)
    denied = isinstance(exc, AccessDenied) or status in (401, 403)
    error = {
        "status": "deferred_access_denied" if denied else "failed", "task": task,
        "scope": progress_scope(task) if task else scope,
        "stage": client.stage if client else "catalog", "http_status": status,
        "source_url": SITE_URL, "embed_url": EMBED_URL,
        "service_url": REPORT_BASE + "/service", "download_url": REPORT_BASE + "/download",
        "error": str(exc), "at": datetime.now(timezone.utc).isoformat(),
    }
    commit_progress(state_path, error=error)


def download_tasks(args: argparse.Namespace, catalog: dict, state: dict) -> int:
    output = args.output.resolve()
    state_path = output / "_state" / "downloads.json"
    selected = selected_tasks(catalog, args)
    if not selected and (args.course_no or args.section):
        raise ValueError("선택 조건에 게시된 계획서가 없습니다.")
    pending = [task for task in selected if not is_complete(output, task, state)]
    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        print("이번 목록에 미수집 계획서가 없습니다.")
        return 0
    print(f"직접 HTTP 수집: 이번 실행 {len(pending)}건 (선택 범위 {len(selected)}건)", flush=True)
    with ReportClient(timeout=args.timeout) as client:
        for index, task in enumerate(pending):
            try:
                if index:
                    time.sleep(args.delay)
                print(f"[{index + 1}/{len(pending)}] {task['year']}-{task['term']} {task['course_no']}/{task['section']} {task['variant']}", flush=True)
                data = client.pdf(task)
                entry = save_pdf(data, task, output)
                state = commit_progress(state_path, entry=entry)
                (output / entry["incoming_path"]).unlink()
                print(f"저장: {entry['pages']}페이지, {entry['bytes']}바이트, {entry['path']}", flush=True)
            except Exception as exc:
                record_error(state_path, exc, task=task, client=client)
                raise
    print(f'이번 실행 완료. 상태 확인: python -m scripts.crawlers.syllabus status --manifest "{args.manifest}" --output "{output}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="부경대 비로그인 강의계획서 직접 HTTP PDF 수집")
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="공개 강의 목록만 저장(기본 최근 3개년)")
    years = catalog.add_mutually_exclusive_group()
    years.add_argument("--year", type=positive, help="한 학년도만 선택")
    years.add_argument("--end-year", type=positive, help="최근 3개년의 마지막 연도")
    catalog.add_argument("--terms", nargs="+", choices=list(TERMS), default=list(TERMS))
    catalog.add_argument("--variants", nargs="+", choices=list(VARIANTS), default=list(VARIANTS))
    catalog.add_argument("--manifest", type=Path, default=DEFAULT_OUTPUT / "_state" / "catalog.json")
    for name, help_text in (
        ("crawl", "한 학기 전체 수집: 목록 재사용 및 이어서 다운로드"),
        ("run", "저장된 목록의 PDF 다운로드"),
        ("status", "목록 대비 실제 파일과 해시 확인"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        command.add_argument("--manifest", type=Path, default=None if name == "crawl" else DEFAULT_OUTPUT / "_state" / "catalog.json")
        command.add_argument("--variants", nargs="+", choices=list(VARIANTS), default=list(VARIANTS))
        command.add_argument("--course-no", default="", help="학수번호로 범위 제한")
        command.add_argument("--section", help="분반으로 범위 제한")
        if name == "crawl":
            command.add_argument("--year", type=positive, required=True)
            command.add_argument("--term", choices=list(TERMS), required=True)
            command.add_argument("--refresh-catalog", action="store_true", help="공개 목록 다시 조회")
        if name != "status":
            command.add_argument("--limit", type=positive, help="이번 실행의 최대 새 PDF 수(기본 전체)")
            command.add_argument("--timeout", type=positive, default=120)
            command.add_argument("--delay", type=nonnegative, default=1, help="PDF 사이 대기 초(기본 1)")
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            scope = [args.year] if args.year else recent_years(args.end_year)
            if min(scope) < 2021:
                raise ValueError("2021년 이후의 계획서를 선택하세요.")
            if not args.manifest.resolve().is_relative_to((ROOT / "files").resolve()):
                raise ValueError("목록은 프로젝트 files/ 안에 저장해야 합니다.")
            with exclusive_run(args.manifest.with_suffix(".lock")):
                result = collect_catalog(scope, list(dict.fromkeys(args.terms)), list(dict.fromkeys(args.variants)))
                atomic_json(args.manifest, result)
            print(f"목록 저장: {args.manifest}; 대상 {len(result['tasks'])}, 미작성 강의 {len(result['unpublished'])}")
            return 0
        if not args.output.resolve().is_relative_to((ROOT / "files").resolve()):
            raise ValueError("결과는 프로젝트 files/ 안에 저장해야 합니다.")
        if args.command == "status":
            catalog = load_catalog(args.manifest)
            state = load_state(args.output / "_state" / "downloads.json")
            selected = selected_tasks(catalog, args)
            completed = sum(is_complete(args.output, task, state) for task in selected)
            print(f"저장 {completed}/{len(selected)}; 미작성 강의 {len(catalog['unpublished'])}")
            for batch in catalog["batches"]:
                print(f"{batch['year']} {TERMS[batch['term']][1]}: 강의 {batch['sections']}, 계획서 {batch['tasks']}")
            return 0
        year = args.year if args.command == "crawl" else None
        term = args.term if args.command == "crawl" else None
        with collection_run(args.output, year=year, term=term):
            state_path = args.output / "_state" / "downloads.json"
            state = load_state(state_path)
            try:
                if args.command == "crawl" and args.manifest and not args.manifest.resolve().is_relative_to((ROOT / "files").resolve()):
                    raise ValueError("목록은 프로젝트 files/ 안에 저장해야 합니다.")
                catalog = prepare_crawl(args) if args.command == "crawl" else load_catalog(args.manifest)
            except Exception as exc:
                record_error(state_path, exc, scope=f"{year}-{term}" if year is not None else "all")
                raise
            return download_tasks(args, catalog, state)
    except KeyboardInterrupt:
        print("중단했습니다. 완료 파일은 유지됩니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
