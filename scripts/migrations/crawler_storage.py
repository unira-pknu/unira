"""Plan/apply crawler state migration or reorganize saved department datasets."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.common.storage import (  # noqa: E402
    KST, department_legacy_roots, department_storage_paths, empty_state,
    get_dataset_paths, load_state_with_migration, save_state_atomic,
    state_migration_plan, validate_storage_path,
)


DATASETS = ("ce", "pknu_notice", "pknu_student_life", "rule")
LEGACY_FILES = {
    "ce": (PROJECT_ROOT / "state.json",),
    "pknu_notice": (PROJECT_ROOT / "state_pknu_notice.json",),
    "pknu_student_life": (PROJECT_ROOT / "state_pknu_student_life.json",),
    "rule": (),
}
LEGACY_KINDS = {"pknu_notice": "posts"}


def _inside_files(project_root: Path, path: Path) -> Path:
    files = (project_root / "files").resolve()
    resolved = path.resolve()
    if resolved == files or not resolved.is_relative_to(files):
        raise ValueError(f"Migration path escapes the files directory: {path}")
    return resolved


def plan_department_layout(
    project_root: Path, storage_paths: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Refuse overlapping directories and collisions before any mutation."""
    project_root = project_root.resolve()
    entries = []
    destinations: set[Path] = set()
    for dataset, parts in (storage_paths if storage_paths is not None else department_storage_paths()).items():
        parts = validate_storage_path(dataset, parts)
        target = _inside_files(project_root, project_root.joinpath("files", "department", *parts))
        if target in destinations:
            raise ValueError(f"Duplicate migration destination: {target}")
        destinations.add(target)
        sources = [root for root in department_legacy_roots(project_root, dataset)
                   if root.resolve() != target and root.exists()]
        for source in sources:
            _inside_files(project_root, source)
            if not source.is_dir() or source.is_symlink() or source.is_junction():
                raise ValueError(f"Migration source must be a regular directory: {source}")
            if target.is_relative_to(source.resolve()) or source.resolve().is_relative_to(target):
                raise ValueError(f"Overlapping migration directories: {source}, {target}")
        if len(sources) > 1 or (sources and target.exists()):
            raise FileExistsError(f"Migration destination/source collision for {dataset}: {target}")
        if target.exists() and not target.is_dir():
            raise FileExistsError(f"Migration destination is not a directory: {target}")
        status = "move" if sources else "already_organized" if target.is_dir() else "no_saved_files"
        entries.append({
            "dataset": dataset, "status": status,
            "source": sources[0].relative_to(project_root).as_posix() if sources else None,
            "target": target.relative_to(project_root).as_posix(),
        })
    return {"dry_run": True, "entries": entries,
            "moves": sum(entry["status"] == "move" for entry in entries)}


class DepartmentPathRewriter:
    """Replace only local path prefixes; preserve document IDs/text/vectors."""

    PROTECTED_FIELDS = {
        "id", "source_id", "document_id", "chunk_id", "slug", "content_hash",
        "content", "text", "raw_ocr_text", "html_text", "page_content",
        "embedding", "embeddings", "vector",
    }

    def __init__(self, project_root: Path, entries: list[dict[str, Any]]):
        self.project_root = project_root.resolve()
        self.prefixes: list[tuple[str, str]] = []
        self.relative_prefixes: list[tuple[str, str]] = []
        self.replacements = 0
        self.verified_references = 0
        self.original_files: set[str] = set()
        for entry in entries:
            target = entry["target"]
            for root in department_legacy_roots(project_root, entry["dataset"]):
                source = root.relative_to(self.project_root).as_posix()
                if source == target:
                    continue
                self.relative_prefixes.append((source, target))
                self.prefixes.extend((
                    (source, target),
                    ((self.project_root / source).as_posix(), (self.project_root / target).as_posix()),
                    ((self.project_root / source).as_uri(), (self.project_root / target).as_uri()),
                ))
        self.prefixes.sort(key=lambda pair: len(pair[0]), reverse=True)
        self.targets = dict(self.relative_prefixes)
        self.absolute_head = self.project_root.as_posix() + "/"
        self.uri_head = self.project_root.as_uri() + "/"

    def replace_path(self, value: str) -> str:
        normalized = value.replace("\\", "/")
        index = normalized.casefold().find("files/")
        if index < 0:
            return value
        head = normalized[:index]
        if head.casefold() not in {"", self.absolute_head.casefold(), self.uri_head.casefold()}:
            return value
        parts = normalized[index:].split("/")
        depth = 3 if len(parts) > 1 and parts[1] == "department" else 2
        source = "/".join(parts[:depth])
        target = self.targets.get(source)
        if target is None:
            return value
        target = quote(target, safe="/") if head == self.uri_head else target
        updated = head + target + normalized[index + len(source):]
        self.replacements += 1
        local = normalized[index:].split("!/", 1)[0].split("#", 1)[0]
        if local in self.original_files:
            self.verified_references += 1
        return updated.replace("/", "\\") if "\\" in value and "/" not in value else updated

    def rewrite(self, value: Any, field: str | None = None) -> Any:
        if field in self.PROTECTED_FIELDS and (isinstance(value, str) or field in {"embedding", "embeddings", "vector"}):
            return value
        if isinstance(value, str):
            return self.replace_path(value)
        if isinstance(value, list):
            return [self.rewrite(item, field) for item in value]
        if isinstance(value, dict):
            return {self.replace_path(key): self.rewrite(item, key) for key, item in value.items()}
        return value

    def might_contain_paths(self, raw: bytes) -> bool:
        return b"files/" in raw or b"files\\\\" in raw or b"files\\" in raw


def _snapshot_tree(root: Path):
    """Record file identity, size, and timestamp without rereading binary data."""
    snapshot = {}
    for directory, folders, filenames in os.walk(root, followlinks=False):
        for name in folders:
            child = Path(directory) / name
            if child.is_symlink() or child.is_junction():
                raise ValueError(f"Refusing to migrate linked directory: {child}")
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Refusing to migrate linked file: {path}")
            stat = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return snapshot


def _rewrite_json_file(path: Path, rewriter: DepartmentPathRewriter, warnings: list[dict]) -> bool:
    before = rewriter.replacements
    stat = path.stat()
    jsonl = path.suffix == ".jsonl"
    if not jsonl:
        raw = path.read_bytes()
        if not rewriter.might_contain_paths(raw):
            return False
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            warnings.append({"path": str(path), "error": str(exc)})
            return False
        updated = rewriter.rewrite(value)
        if rewriter.replacements == before:
            return False
        newline = "\r\n" if b"\r\n" in raw else "\n"
        text = json.dumps(updated, ensure_ascii=False, indent=2).replace("\n", newline)
        if raw.endswith(b"\n"):
            text += newline
        replacement = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    handle = tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            if jsonl:
                with path.open("rb") as source:
                    for line_number, line in enumerate(source, 1):
                        count = rewriter.replacements
                        output_line = line
                        if rewriter.might_contain_paths(line):
                            try:
                                value = json.loads(line.decode("utf-8-sig"))
                            except (ValueError, UnicodeError) as exc:
                                warnings.append({"path": str(path), "line": line_number, "error": str(exc)})
                            else:
                                updated = rewriter.rewrite(value)
                                if rewriter.replacements != count:
                                    ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                                    bom = b"\xef\xbb\xbf" if line.startswith(b"\xef\xbb\xbf") else b""
                                    output_line = bom + json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + ending
                        handle.write(output_line)
            else:
                handle.write(replacement)
            if rewriter.replacements == before:
                return False
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        # Path-only edits must not force OCR/preprocessing to run again.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _save_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _rewrite_review_link(match: re.Match, path: Path, rewriter: DepartmentPathRewriter) -> str:
    link = urlsplit(html.unescape(match.group("link")))
    if link.scheme or link.netloc or not link.path:
        return match.group(0)
    source = (path.parent / unquote(link.path)).resolve()
    updated = rewriter.replace_path(source.as_posix())
    if updated == source.as_posix():
        return match.group(0)
    relative = Path(os.path.relpath(updated, path.parent)).as_posix()
    url = urlunsplit(("", "", quote(relative, safe="/"), link.query, link.fragment))
    return match.group("opening") + html.escape(url, quote=True) + match.group("closing")


def migrate_department_layout(
    project_root: Path, *, apply: bool = False,
    storage_paths: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    report = plan_department_layout(project_root, storage_paths)
    if not apply:
        return report
    report.update({"dry_run": False, "status": "in_progress", "started_at": datetime.now(KST).isoformat(timespec="seconds")})
    report_path = project_root / "files" / "department" / "_migration" / "department_layout.json"
    _inside_files(project_root, report_path)
    _save_report(report_path, report)
    rewriter = DepartmentPathRewriter(project_root, report["entries"])
    report.update({"moved_datasets": 0, "verified_files": 0, "verified_bytes": 0,
                   "rewritten_files": 0, "warnings": []})
    try:
        # Complete all link/identity checks before the first directory is moved.
        snapshots = {}
        for entry in report["entries"]:
            if entry["status"] == "move":
                source = project_root / entry["source"]
                snapshots[entry["dataset"]] = _snapshot_tree(source)
                rewriter.original_files.update(f"{entry['source']}/{name}" for name in snapshots[entry["dataset"]])
        for entry in report["entries"]:
            if entry["status"] != "move":
                continue
            source = _inside_files(project_root, project_root / entry["source"])
            target = _inside_files(project_root, project_root / entry["target"])
            if target.exists():
                raise FileExistsError(f"Migration destination appeared during preflight: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            snapshot = snapshots[entry["dataset"]]
            after = _snapshot_tree(target)
            if after != snapshot:
                raise RuntimeError(f"File preservation check failed for {entry['dataset']}")
            report["moved_datasets"] += 1
            report["verified_files"] += len(snapshot)
            report["verified_bytes"] += sum(meta[0] for meta in snapshot.values())
            _save_report(report_path, report)
            print(f"Moved {entry['dataset']}: {len(snapshot)} files verified", flush=True)
        files = project_root / "files"
        # Current derived roots live outside files. Frozen recovery evidence is
        # deliberately excluded: rewriting those files would invalidate hashes.
        reference_roots = (files, project_root / "preprocessed", project_root / "vectorized",
                           project_root / "var/reviewed", project_root / "eval")
        candidates = [path for root in reference_roots
                      for pattern in ("*.json", "*.jsonl", "*.json.bak", "*.json.legacy.bak")
                      for path in root.rglob(pattern)]
        candidates.extend(project_root.glob("*.json"))
        for path in candidates:
            if path == report_path or path.is_relative_to(files / "department" / "_migration"):
                continue
            if _rewrite_json_file(path, rewriter, report["warnings"]):
                report["rewritten_files"] += 1
                if report["rewritten_files"] % 5000 == 0:
                    print(f"Updated local references in {report['rewritten_files']} files", flush=True)
        review_roots = (project_root / "var/reviewed", files / "_reviewed")
        for path in (path for root in review_roots for path in root.rglob("*")):
            if path.suffix not in {".html", ".md"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            updated = text
            if path.suffix == ".html":
                updated = re.sub(
                    r'''(?P<opening>\b(?:href|src)\s*=\s*["'])(?P<link>[^"']+)(?P<closing>["'])''',
                    lambda match: _rewrite_review_link(match, path, rewriter), updated,
                )
            for source, target in rewriter.prefixes:
                updated = updated.replace(source + "/", target + "/")
            if updated != text:
                stat = path.stat()
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(updated, encoding="utf-8")
                temporary.replace(path)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                report["rewritten_files"] += 1
        report.update({"status": "complete", "rewritten_references": rewriter.replacements,
                       "verified_existing_file_references": rewriter.verified_references,
                       "finished_at": datetime.now(KST).isoformat(timespec="seconds")})
    except Exception as exc:
        report.update({"status": "interrupted", "error": str(exc)})
        raise
    finally:
        _save_report(report_path, report)
    report["report_path"] = report_path.relative_to(project_root).as_posix()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan crawler state path migration (dry-run by default).")
    parser.add_argument("--dataset", choices=(*DATASETS, "all"), default="all")
    parser.add_argument("--department-layout", action="store_true", help="Reorganize saved department folders and update local paths; no crawling.")
    parser.add_argument("--apply", action="store_true", help="Apply the selected migration (dry-run by default).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.department_layout:
        print(json.dumps(migrate_department_layout(PROJECT_ROOT, apply=args.apply), ensure_ascii=False, indent=2))
        return 0
    selected = DATASETS if args.dataset == "all" else (args.dataset,)
    report: list[dict[str, object]] = []
    for dataset in selected:
        paths = get_dataset_paths(PROJECT_ROOT, dataset)
        legacy = LEGACY_FILES[dataset]
        plan = state_migration_plan(paths, legacy)
        entry: dict[str, object] = {"dataset": dataset, "dry_run": not args.apply, "actions": plan}
        if args.apply and not paths.state.exists():
            if any(path.exists() for path in legacy):
                _, origin = load_state_with_migration(
                    paths, legacy_paths=legacy, legacy_kind=LEGACY_KINDS.get(dataset, "items")
                )
                entry["result"] = origin
            else:
                save_state_atomic(paths.state, empty_state(dataset), dataset)
                entry["result"] = "created"
        elif args.apply:
            entry["result"] = "skipped_target_exists"
        report.append(entry)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
