"""UTF-8 search smoke test wrapper for query_supabase."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

QUESTIONS = [
    "졸업 요건 알려줘",
    "장학금 신청 관련 공지 알려줘",
    "수강신청 관련 공지 알려줘",
    "컴퓨터공학전공 학사 안내 알려줘",
    "복수전공이나 전과 관련 내용 알려줘",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUERY_SCRIPT = PROJECT_ROOT / "scripts" / "vectorization" / "query_supabase.py"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for question in QUESTIONS:
        print("=" * 60)
        print("질문:", question)
        subprocess.run(
            [
                sys.executable,
                str(QUERY_SCRIPT),
                question,
                "--top-k",
                "5",
                "--min-similarity",
                "0.35",
            ],
            cwd=PROJECT_ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
