"""
[rule 문서의 우선순위 점수를 계산하고 DB에 반영하는 스크립트, update_priorities.py]

rule 문서 1개(source)마다 우선 순위 점수를 계산해서 public.rule_sources.priority_* 컬럼에 저장한다. 
이 점수는 RAG 검색에서 문서 랭킹을 보정하기 위한 보조 신호이며, embedding similarity를 대체하는 절대 점수가 아니다.

기본 실행:
    - public.rule_sources + public.rule_chunks에서 active rule 문서를 읽는다.
    - 각 source별 priority_score와 priority_details를 계산한다.
    - rule_sources.priority_score, priority_details, priority_updated_at을 갱신한다..

현재 우선순위 계산식은 scripts/rule/priority.py에 구현되어 있다.
    priority_score =
        0.30 * authority_score
      + 0.40 * student_relevance_score
      + 0.15 * recency_score
      + 0.15 * source_quality_score

점수 기준:
    - authority_score          : 학칙/규정 문서를 세칙/지침보다 높게 평가한다.
    - student_relevance_score  : 졸업, 교육과정, 학점, 수강처럼 학생 Q&A에 직접 관련된 문서를 높게 평가하고, 내부 행정/연구센터 중심 문서는 낮게 평가한다.
    - recency_score            : 최근 개정되었거나 최신성이 높은 문서를 높게 평가한다.
    - source_quality_score     : 제목, URL, 본문, 원본 첨부파일 메타데이터가 충분한 문서를 높게 평가한다.
    
    - 폐지/삭제/실효/종료된 문서는 큰 폭으로 감점한다. 
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    psycopg = None
    dict_row = None

    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.rule.priority import aggregate_source_records, calculate_rule_priority  # noqa: E402
from scripts.vectorization.datasets import get_vector_paths

DEFAULT_INDEX_PATH = get_vector_paths(PROJECT_ROOT, "rule")["output_root"] / "index.jsonl"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "update_rule_priorities.log"

log = logging.getLogger(__name__)


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def with_connect_timeout(conninfo: str, timeout_seconds: int = 10) -> str:
    parts = urlsplit(conninfo)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("connect_timeout", str(timeout_seconds))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def connect() -> Any:
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg is required for database updates. Install backend requirements first.")

    load_dotenv(PROJECT_ROOT / "backend" / ".env")
    conninfo = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if conninfo:
        return psycopg.connect(
            with_connect_timeout(conninfo),
            row_factory=dict_row,
            prepare_threshold=None,
        )

    required = ["PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing database configuration. Set DATABASE_URL or SUPABASE_DB_URL, "
            f"or set {', '.join(required)}."
        )

    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.getenv("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        sslmode=os.getenv("PGSSLMODE", "require"),
        connect_timeout=10,
        row_factory=dict_row,
        prepare_threshold=None,
    )


def fetch_rule_documents(conn: Any) -> list[dict[str, Any]]:
    query = """
        select
            s.id,
            coalesce(s.title, s.source_slug) as title,
            s.metadata,
            coalesce(string_agg(c.content, E'\n\n' order by c.chunk_index), '') as content
        from public.rule_sources as s
        left join public.rule_chunks as c on c.source_id = s.id
        where s.status = 'active'
        group by s.id, s.title, s.source_slug, s.metadata
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return list(cur.fetchall())


def iter_index_records(index_path: Path) -> Iterable[dict[str, Any]]:
    with index_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc


def build_updates(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for doc in documents:
        metadata = doc.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        score, details = calculate_rule_priority(
            title=str(doc.get("title") or ""),
            content=str(doc.get("content") or ""),
            metadata=metadata,
        )
        updates.append(
            {
                "id": doc["id"],
                "title": doc.get("title") or "",
                "priority_score": score,
                "priority_details": Jsonb(details),
            }
        )
    return updates


def log_preview(updates: list[dict[str, Any]], limit: int) -> None:
    for row in sorted(updates, key=lambda item: item["priority_score"], reverse=True)[:limit]:
        log.info("[PREVIEW] %.4f %s %s", row["priority_score"], row["id"], row["title"])


def update_rule_priorities(conn: Any, dry_run: bool, preview_limit: int) -> int:
    documents = fetch_rule_documents(conn)
    updates = build_updates(documents)
    log.info("loaded rule_sources=%d", len(updates))

    if dry_run:
        log_preview(updates, preview_limit)
        return len(updates)

    update_sql = """
        update public.rule_sources
        set
            priority_score = %(priority_score)s,
            priority_details = %(priority_details)s,
            priority_updated_at = now()
        where id = %(id)s
    """
    with conn.cursor() as cur:
        cur.executemany(update_sql, updates)
    conn.commit()
    return len(updates)


def preview_from_index(index_path: Path, preview_limit: int) -> int:
    documents = aggregate_source_records(iter_index_records(index_path))
    updates = build_updates(documents)
    log.info("loaded local rule_sources=%d from %s", len(updates), index_path)
    log_preview(updates, preview_limit)
    return len(updates)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Update rule document priority scores.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from-index",
        action="store_true",
        help="Read local files/vectorized/rule/index.jsonl for preview. Requires --dry-run.",
    )
    parser.add_argument("--index-path", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--preview-limit", type=int, default=20)
    args = parser.parse_args()

    if args.preview_limit <= 0:
        raise ValueError("--preview-limit must be positive")
    if args.from_index:
        if not args.dry_run:
            raise ValueError("--from-index is preview-only; pass --dry-run")
        count = preview_from_index(args.index_path, args.preview_limit)
    else:
        with connect() as conn:
            count = update_rule_priorities(conn, args.dry_run, args.preview_limit)

    log.info("done: processed %d rule source priorities", count)


if __name__ == "__main__":
    main()
