from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.preprocessing.storage import get_preprocessed_root
from scripts.crawlers.common.storage import get_dataset_root

DEFAULT_RUN_AT = "09:00"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "daily_pipeline.log"

log = logging.getLogger("daily_pipeline")
_is_running = False


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def run_command(name: str, command: list[str]) -> None:
    log.info("[%s] start: %s", name, " ".join(command))
    started_at = datetime.now()
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    elapsed = (datetime.now() - started_at).total_seconds()
    log.info("[%s] done in %.1fs", name, elapsed)


CE_DATA_ROOT = get_dataset_root(PROJECT_ROOT, "ce")


def run_preprocess_steps(python: str) -> None:
    """게시글 JSON 본문과 첨부파일을 각각 전처리한다."""
    json_input = CE_DATA_ROOT / "output" / "json"
    files_input = CE_DATA_ROOT / "output" / "files"
    json_output = get_preprocessed_root(PROJECT_ROOT, "ce") / "json"
    files_output = get_preprocessed_root(PROJECT_ROOT, "ce") / "files"
    crawl_json_root = json_input

    if json_input.exists():
        run_command(
            "preprocess-json",
            [
                python,
                "scripts/preprocessing/file_preprocessing.py",
                "--input-root",
                str(json_input),
                "--output-root",
                str(json_output),
                "--output-json-root",
                str(crawl_json_root),
                "--layout",
                "flat",
            ],
        )
    else:
        log.warning("Skipping preprocess-json; missing input: %s", json_input)

    if files_input.exists():
        run_command(
            "preprocess-files",
            [
                python,
                "scripts/preprocessing/file_preprocessing.py",
                "--input-root",
                str(files_input),
                "--output-root",
                str(files_output),
                "--output-json-root",
                str(crawl_json_root),
            ],
        )
    else:
        log.warning("Skipping preprocess-files; missing input: %s", files_input)


def run_pipeline(args: argparse.Namespace) -> None:
    global _is_running

    if _is_running:
        log.warning("Previous pipeline run is still in progress. Skipping this schedule.")
        return

    _is_running = True
    started_at = datetime.now()
    log.info("=" * 70)
    log.info("Daily pipeline started: %s", started_at.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        python = sys.executable
        run_command(
            "crawler",
            [python, "scripts/crawlers/departments/engine.py", "--dataset", "ce", "--once"],
        )
        run_preprocess_steps(python)
        run_command(
            "vectorization",
            [
                python,
                "scripts/vectorization/vectorization.py",
                "--dataset",
                "ce",
                "--backend",
                args.vector_backend,
                "--batch-size",
                str(args.vector_batch_size),
            ],
        )
        run_command(
            "load_to_supabase",
            [
                python,
                "scripts/vectorization/load_to_supabase.py",
                "--dataset",
                "ce",
                "--batch-size",
                str(args.load_batch_size),
            ],
        )
        run_command(
            "update_ce_priorities",
            [python, "scripts/ce/update_priorities.py"],
        )
        elapsed = (datetime.now() - started_at).total_seconds()
        log.info("Daily pipeline completed successfully in %.1fs", elapsed)
    except subprocess.CalledProcessError as exc:
        log.error("Pipeline failed at command: %s", exc.cmd)
        log.error("Exit code: %s", exc.returncode)
    except Exception:
        log.exception("Unexpected pipeline error")
    finally:
        _is_running = False
        log.info("=" * 70)


def run_scheduler(args: argparse.Namespace) -> None:
    job = schedule.every().day.at(args.run_at).do(run_pipeline, args)
    log.info(
        "Scheduler started. Pipeline will run every day at %s. Next scheduled run: %s",
        args.run_at,
        job.next_run,
    )

    if args.run_on_start:
        log.info("Running pipeline immediately on scheduler start.")
        run_pipeline(args)

    while True:
        schedule.run_pending()
        time.sleep(args.poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run scripts/crawlers/departments/engine.py --dataset ce -> "
            "scripts/preprocessing/file_preprocessing.py -> "
            "scripts/vectorization/vectorization.py -> scripts/vectorization/load_to_supabase.py every day at 09:00."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the full pipeline once immediately without starting the scheduler.",
    )
    parser.add_argument(
        "--run-on-start",
        dest="run_on_start",
        action="store_true",
        default=True,
        help="Run once immediately, then keep the daily scheduler active. This is the default.",
    )
    parser.add_argument(
        "--no-run-on-start",
        dest="run_on_start",
        action="store_false",
        help="Only start the scheduler without running the pipeline immediately.",
    )
    parser.add_argument(
        "--run-at",
        default=DEFAULT_RUN_AT,
        help="Daily run time in HH:MM format. Default: 09:00.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Scheduler polling interval in seconds. Default: 30.",
    )
    parser.add_argument(
        "--vector-backend",
        choices=["sentence-transformers", "hash"],
        default="sentence-transformers",
        help=(
            "Embedding backend for scripts/vectorization/vectorization.py. Default is sentence-transformers "
            "because scripts/vectorization/load_to_supabase.py expects 384-dimensional embeddings."
        ),
    )
    parser.add_argument(
        "--vector-batch-size",
        type=int,
        default=32,
        help="Batch size passed to scripts/vectorization/vectorization.py. Default: 32.",
    )
    parser.add_argument(
        "--load-batch-size",
        type=int,
        default=200,
        help="Batch size passed to scripts/vectorization/load_to_supabase.py. Default: 200.",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()

    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    if args.vector_batch_size <= 0:
        raise ValueError("--vector-batch-size must be positive")
    if args.load_batch_size <= 0:
        raise ValueError("--load-batch-size must be positive")

    if args.once:
        run_pipeline(args)
    else:
        run_scheduler(args)


if __name__ == "__main__":
    main()
