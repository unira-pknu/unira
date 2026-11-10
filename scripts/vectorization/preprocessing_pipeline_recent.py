"""Crawl recent data and preprocess only files touched by that crawl run."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.preprocessing.storage import get_preprocessed_root
from scripts.preprocessing.preprocessing_pipeline import (  # noqa: E402
    DATASET_CHOICES,
    DEPARTMENT_DATASETS,
    SUPPORTED_EXTS,
    WEB_DATASETS,
    configure_logging,
    parse_file_exts,
    run_pipeline,
    selected_datasets,
)
from scripts.preprocessing.file_preprocessing import ARCHIVE_EXTS, ensure_output_path  # noqa: E402
from scripts.vectorization.load_to_supabase import PriorityRefreshError, load as load_to_supabase  # noqa: E402
from scripts.vectorization.vectorization import (  # noqa: E402
    DATASET_PATHS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_BACKEND,
    DEFAULT_DIMENSIONS,
    DEFAULT_SENTENCE_MODEL,
    HashEmbedder,
    make_embedder,
    run_batch as run_vectorization,
)
from scripts.preprocessing.rule import output_path_for as rule_output_path_for  # noqa: E402

RUNS_DIR = PROJECT_ROOT / "files" / "_runs"
log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run recent crawlers and preprocess only files updated by that run."
    )
    parser.add_argument("--dataset", choices=DATASET_CHOICES, default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-crawl",
        action="store_true",
        help="Do not run crawlers; collect files changed after --since instead.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO timestamp used with --skip-crawl, e.g. 2026-07-12T00:00:00.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to write the recent-run manifest JSON.",
    )
    parser.add_argument("--recent-pages", type=int, default=3)
    parser.add_argument(
        "--student-life-mode",
        choices=["guide", "pages", "ebook", "all"],
        default="guide",
    )
    parser.add_argument("--student-life-limit", type=int, default=5)
    parser.add_argument("--rule-max-law-items", type=int, default=20)
    parser.add_argument("--rule-max-bylaw-pages", type=int, default=2)
    parser.add_argument("--chunk-size", type=int, default=900)
    parser.add_argument("--chunk-overlap", type=int, default=120)
    parser.add_argument(
        "--skip-vectorization",
        action="store_true",
        help="Stop after preprocessing.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Stop after vectorization.",
    )
    parser.add_argument(
        "--backend",
        choices=["hash", "sentence-transformers"],
        default=DEFAULT_BACKEND,
    )
    parser.add_argument("--model-name", default=DEFAULT_SENTENCE_MODEL)
    parser.add_argument("--dimensions", type=int, default=DEFAULT_DIMENSIONS)
    parser.add_argument("--vector-batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--load-batch-size", type=int, default=200)
    parser.add_argument(
        "--pdf-ocr",
        choices=["auto", "never", "always"],
        default="auto",
    )
    parser.add_argument("--ocr-language", default="kor+eng")
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument(
        "--file-ext",
        "--file-exts",
        dest="file_exts",
        nargs="+",
        default=None,
        help="Limit attachment preprocessing to extensions such as pdf hwp xlsx.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.file_exts = parse_file_exts(args.file_exts)
    unsupported = sorted((args.file_exts or set()) - SUPPORTED_EXTS)
    if unsupported:
        parser.error(
            "unsupported --file-ext value(s): "
            + ", ".join(unsupported)
            + ". Supported: "
            + ", ".join(sorted(SUPPORTED_EXTS))
        )
    if args.recent_pages <= 0:
        parser.error("--recent-pages must be positive")
    if args.student_life_limit is not None and args.student_life_limit <= 0:
        parser.error("--student-life-limit must be positive")
    if args.rule_max_law_items <= 0:
        parser.error("--rule-max-law-items must be positive")
    if args.rule_max_bylaw_pages <= 0:
        parser.error("--rule-max-bylaw-pages must be positive")
    if args.vector_batch_size <= 0:
        parser.error("--vector-batch-size must be positive")
    if args.load_batch_size <= 0:
        parser.error("--load-batch-size must be positive")
    if args.skip_crawl and not args.since:
        parser.error("--skip-crawl requires --since")
    return args


def run_command(command: list[str]) -> None:
    log.info("[CRAWL] %s", " ".join(command))
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def crawl_dataset(name: str, args: argparse.Namespace) -> None:
    python = sys.executable
    if name in DEPARTMENT_DATASETS:
        run_command(
            [
                python,
                "scripts/crawlers/departments/engine.py",
                "--dataset",
                name,
                "--once",
                "--recent-only",
                str(args.recent_pages),
            ]
        )
    elif name == "pknu_notice":
        run_command(
            [
                python,
                "scripts/crawlers/pknu_notice.py",
                "--once",
                "--recent-only",
                str(args.recent_pages),
            ]
        )
    elif name == "pknu_student_life":
        command = [
            python,
            "scripts/crawlers/pknu_student_life.py",
            "--mode",
            args.student_life_mode,
        ]
        if args.student_life_mode in {"guide", "all"} and args.student_life_limit:
            command.extend(["--limit", str(args.student_life_limit)])
        run_command(command)
    elif name == "rule":
        run_command(
            [
                python,
                "scripts/crawlers/pknu_rule.py",
                "--laws",
                "--bylaws",
                "--max-law-items",
                str(args.rule_max_law_items),
                "--max-bylaw-pages",
                str(args.rule_max_bylaw_pages),
            ]
        )


def iter_files_changed_since(root: Path, since_ts: float, suffix: str | None = None) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if suffix and path.suffix.lower() != suffix:
            continue
        try:
            if path.stat().st_mtime >= since_ts:
                files.append(path.resolve())
        except OSError:
            continue
    return sorted(files)


def collect_targets(dataset_names: list[str], since_ts: float) -> dict[str, dict[str, list[Path]]]:
    targets: dict[str, dict[str, list[Path]]] = {}
    for name in dataset_names:
        if name == "rule":
            rule_root = PROJECT_ROOT / "files" / "rule" / "output"
            targets[name] = {
                "json": iter_files_changed_since(rule_root / "json", since_ts, ".json"),
                "html": iter_files_changed_since(rule_root / "html", since_ts, ".html"),
                "files": iter_files_changed_since(rule_root / "files", since_ts),
            }
        else:
            dataset = WEB_DATASETS[name]
            targets[name] = {
                "json": sorted({p for root in dataset.source_roots("json") for p in iter_files_changed_since(root, since_ts, ".json")}),
                "files": sorted({p for root in dataset.source_roots("files") for p in iter_files_changed_since(root, since_ts)}),
            }
    return targets


def write_manifest(
    manifest_path: Path,
    *,
    dataset_names: list[str],
    run_started_at: datetime,
    targets: dict[str, dict[str, list[Path]]],
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_started_at": run_started_at.isoformat(timespec="seconds"),
        "datasets": dataset_names,
        "targets": {
            dataset: {
                kind: [path.relative_to(PROJECT_ROOT).as_posix() for path in paths]
                for kind, paths in kinds.items()
            }
            for dataset, kinds in targets.items()
        },
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[MANIFEST] %s", manifest_path)


def count_targets(targets: dict[str, dict[str, list[Path]]]) -> int:
    return sum(len(paths) for kinds in targets.values() for paths in kinds.values())


def collect_archive_outputs(
    archive_file: Path,
    input_root: Path,
    output_root: Path,
) -> list[Path]:
    archive_rel = archive_file.resolve().relative_to(input_root.resolve()).with_suffix("")
    archive_output_root = output_root / "zip" / archive_rel
    if not archive_output_root.exists():
        return []
    return sorted(path.resolve() for path in archive_output_root.rglob("*.json") if path.is_file())


def collect_web_preprocessed_targets(
    dataset: str,
    target_kinds: dict[str, list[Path]],
) -> list[Path]:
    web_dataset = WEB_DATASETS[dataset]
    outputs: list[Path] = []
    for path in target_kinds.get("json", []):
        try:
            source_root = web_dataset.source_for(path, "json")
            out_path = ensure_output_path(
                path,
                source_root,
                web_dataset.processed_root(source_root, "json"),
                layout="flat",
            )
        except ValueError:
            continue
        if out_path.exists():
            outputs.append(out_path.resolve())

    for path in target_kinds.get("files", []):
        try:
            source_root = web_dataset.source_for(path, "files")
            processed_root = web_dataset.processed_root(source_root, "files")
            if path.suffix.lower() in ARCHIVE_EXTS:
                outputs.extend(
                    collect_archive_outputs(
                        path,
                        source_root,
                        processed_root,
                    )
                )
            else:
                out_path = ensure_output_path(
                    path,
                    source_root,
                    processed_root,
                    layout="by_ext",
                )
                if out_path.exists():
                    outputs.append(out_path.resolve())
        except ValueError:
            continue
    return sorted(dict.fromkeys(outputs))


def collect_rule_preprocessed_targets(target_kinds: dict[str, list[Path]]) -> list[Path]:
    rule_root = PROJECT_ROOT / "files" / "rule"
    output_root = get_preprocessed_root(PROJECT_ROOT, "rule")
    roots = {
        "json": rule_root / "output" / "json",
        "html": rule_root / "output" / "html",
        "files": rule_root / "output" / "files",
    }
    source_kinds = {"json": "json", "html": "html", "files": "file"}
    outputs: list[Path] = []
    for kind, paths in target_kinds.items():
        for path in paths:
            out_path = rule_output_path_for(
                path,
                roots[kind],
                output_root / source_kinds[kind],
            )
            if out_path.exists():
                outputs.append(out_path.resolve())
    return sorted(dict.fromkeys(outputs))


def collect_preprocessed_targets_from_manifest(
    dataset_names: list[str],
    raw_targets: dict[str, dict[str, list[Path]]],
) -> dict[str, list[Path]]:
    outputs: dict[str, list[Path]] = {}
    for dataset in dataset_names:
        target_kinds = raw_targets.get(dataset, {})
        if dataset == "rule":
            outputs[dataset] = collect_rule_preprocessed_targets(target_kinds)
        else:
            outputs[dataset] = collect_web_preprocessed_targets(dataset, target_kinds)
    return outputs


def make_recent_embedder(args: argparse.Namespace):
    if args.dry_run:
        return HashEmbedder(1)
    embedder_args = argparse.Namespace(
        backend=args.backend,
        model_name=args.model_name,
        dimensions=args.dimensions,
    )
    return make_embedder(embedder_args)


def vectorize_recent(
    dataset_names: list[str],
    preprocessed_targets: dict[str, list[Path]],
    args: argparse.Namespace,
) -> list[str]:
    vectorized: list[str] = []
    embedder = None
    for dataset in dataset_names:
        files = preprocessed_targets.get(dataset, [])
        if not files:
            log.info("[%s] No recently preprocessed files to vectorize", dataset)
            continue
        roots = DATASET_PATHS[dataset]
        log.info("[%s] Vectorizing %d recently preprocessed files", dataset, len(files))
        if embedder is None:
            embedder = make_recent_embedder(args)
        run_vectorization(
            input_root=roots["input_root"],
            output_root=roots["output_root"],
            project_root=PROJECT_ROOT,
            embedder=embedder,
            batch_size=args.vector_batch_size,
            dry_run=args.dry_run,
            dataset=dataset,
            changed_only=False,
            target_files=files,
        )
        if not args.dry_run:
            vectorized.append(dataset)
    return vectorized


def load_recent(vectorized_datasets: list[str], args: argparse.Namespace) -> None:
    # A failed priority refresh used to end the run at the first dataset (ce),
    # so notices, main pages and rules went unloaded for days (2026-11-03).
    # Load every dataset first, then fail the run so the job still shows red.
    refresh_failed: list[str] = []
    for dataset in vectorized_datasets:
        index_path = DATASET_PATHS[dataset]["output_root"] / "index.jsonl"
        log.info("[%s] Loading recent vector index to database", dataset)
        try:
            count = load_to_supabase(
                index_path=index_path,
                batch_size=args.load_batch_size,
                dataset=dataset,
                replace_sources=True,
            )
        except PriorityRefreshError:
            log.exception("[%s] loaded, priority refresh failed; continuing with the next dataset", dataset)
            refresh_failed.append(dataset)
            continue
        log.info("[%s] Loaded %d rows", dataset, count)
    if refresh_failed:
        raise RuntimeError(f"priority refresh failed after loading: {', '.join(refresh_failed)}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging()

    dataset_names = selected_datasets(args.dataset)
    if args.skip_crawl:
        run_started_at = datetime.fromisoformat(args.since)
    else:
        run_started_at = datetime.now()
        for dataset in dataset_names:
            crawl_dataset(dataset, args)

    since_ts = run_started_at.timestamp() - 1.0
    targets = collect_targets(dataset_names, since_ts)
    manifest_path = args.manifest or (
        RUNS_DIR / f"preprocessing_recent_{run_started_at.strftime('%Y%m%d_%H%M%S')}.json"
    )
    write_manifest(
        manifest_path,
        dataset_names=dataset_names,
        run_started_at=run_started_at,
        targets=targets,
    )

    total = count_targets(targets)
    if total == 0:
        log.info("No files were updated by the recent crawl run.")
        return

    log.info("Preprocessing %d recently updated files", total)
    run_pipeline(
        dataset=args.dataset,
        changed_only=False,
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        pdf_ocr_mode=args.pdf_ocr,
        ocr_language=args.ocr_language,
        ocr_dpi=args.ocr_dpi,
        file_exts=args.file_exts,
        targets=targets,
    )

    if args.skip_vectorization:
        return

    preprocessed_targets = collect_preprocessed_targets_from_manifest(dataset_names, targets)
    vectorized_datasets = vectorize_recent(dataset_names, preprocessed_targets, args)

    if args.skip_load or args.dry_run:
        return
    load_recent(vectorized_datasets, args)


if __name__ == "__main__":
    main()
