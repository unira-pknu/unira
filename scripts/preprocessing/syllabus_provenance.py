"""Join syllabus download receipts to PDFs; repair provenance without re-extraction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TASK_FIELDS = ("year", "term", "term_code", "course_no", "section", "course_name",
               "staff_name", "department", "level", "variant")


def inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    candidate.relative_to(root.resolve())
    return candidate


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_syllabus_index(project_root: Path) -> dict[str, dict]:
    root = project_root / "files/pknu_syllabus"
    receipt_path = root / "_state/downloads.json"
    if not receipt_path.exists():
        return {}
    state = json.loads(receipt_path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("downloads"), dict):
        raise ValueError("Unsupported syllabus download receipts")
    result = {}
    for task_id, receipt in state["downloads"].items():
        if receipt.get("status") != "downloaded":
            continue
        task = receipt.get("task") or {}
        if task.get("id") != task_id:
            raise ValueError(f"Receipt task ID mismatch: {task_id}")
        path = inside(root, receipt["path"])
        if path.suffix.lower() != ".pdf" or not path.stem.endswith("_" + task_id):
            raise ValueError(f"Receipt filename/task mismatch: {receipt['path']}")
        checksum = receipt.get("sha256", "")
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError(f"Invalid receipt hash: {task_id}")
        url = receipt.get("source_url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "irumi.pknu.ac.kr":
            raise ValueError(f"Unexpected syllabus source URL: {task_id}")
        for key in ("year", "term", "course_no", "section", "course_name", "variant"):
            if task.get(key) in (None, ""):
                raise ValueError(f"Missing {key}: {task_id}")
        relative = path.relative_to(project_root.resolve()).as_posix()
        if relative in result:
            raise ValueError(f"Duplicate receipt path: {relative}")
        result[relative] = {
            "source_dataset": "pknu_syllabus", "source_kind": "attachment",
            "document_kind": "syllabus", "document_id": f"pknu_syllabus:{task_id}",
            "source_id": task_id, "syllabus_task_id": task_id,
            "doc_title": (f"{task['year']}-{task['term']} {task['course_name']} "
                          f"({task['course_no']}-{task['section']}, {task['variant']})"),
            "doc_url": url, "source_page_url": url,
            "source_url": url, "source_site": f"{parsed.scheme}://{parsed.netloc}",
            # ReportingServer/download is a session-bound transport endpoint,
            # not a permanent public URL for this PDF.
            "source_link_kind": "syllabus_search_page", "attachment_url": "",
            "attachment_name": path.name, "attachment_sha256": checksum,
            "source_attachment_path": relative, "category": "강의계획서",
            "download_receipt_path": receipt_path.relative_to(project_root).as_posix(),
            "downloaded_at": receipt.get("saved_at"),
            **{key: task.get(key, "") for key in TASK_FIELDS},
            "_source_json_mtime": receipt_path.stat().st_mtime,
        }
    return result


def verify_syllabus_pdf(path: Path, provenance: dict) -> None:
    if sha256(path) != provenance["attachment_sha256"]:
        raise ValueError(f"Syllabus PDF hash mismatch: {path}")


def restored_document(document: dict, index: dict[str, dict], project_root: Path) -> dict:
    path = inside(project_root, document["source_path"])
    relative = path.relative_to(project_root.resolve()).as_posix()
    if relative not in index:
        raise ValueError(f"No syllabus download receipt: {relative}")
    provenance = {key: value for key, value in index[relative].items() if not key.startswith("_")}
    return {**document, "provenance": {**(document.get("provenance") or {}), **provenance}}


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repair(project_root: Path, *, apply: bool = False) -> dict:
    project_root = project_root.resolve()
    index = load_syllabus_index(project_root)
    folder = project_root / "files/preprocessed/pknu_syllabus"
    files = sorted(folder.rglob("*.json"))
    if not files or not index:
        raise ValueError("No syllabus preprocessing documents or download receipts")
    changed = []
    verified = set()
    # Validate all paths, receipts and PDF hashes before modifying any document.
    for position, path in enumerate(files, 1):
        path.resolve().relative_to(folder.resolve())
        raw = path.read_bytes()
        document = json.loads(raw)
        restored = restored_document(document, index, project_root)
        source = inside(project_root, document["source_path"])
        relative = source.relative_to(project_root).as_posix()
        if relative not in verified:
            verify_syllabus_pdf(source, index[relative])
            verified.add(relative)
        if document != restored:
            changed.append((path, hashlib.sha256(raw).hexdigest()))
        if position % 1000 == 0:
            print(f"Validated {position}/{len(files)} syllabus documents", flush=True)
    stats = {"documents": len(files), "verified_pdfs": len(verified),
             "changed": len(changed), "applied": apply, "backup": None}
    if not apply or not changed:
        return stats
    backup_root = project_root / "files/_archive/syllabus_provenance"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / f"preprocessed_{stamp}.zip"
    with zipfile.ZipFile(backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, checksum in changed:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError(f"Document changed during preflight: {path}")
            archive.writestr(path.relative_to(project_root).as_posix(), raw)
    # The complete backup is closed successfully before the first replacement.
    for position, (path, checksum) in enumerate(changed, 1):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError(f"Document changed during repair: {path}")
        document = json.loads(raw)
        restored = restored_document(document, index, project_root)
        atomic_write(path, json.dumps(restored, ensure_ascii=False, indent=2).encode("utf-8"))
        if position % 1000 == 0:
            print(f"Restored {position}/{len(changed)} syllabus documents", flush=True)
    stats["backup"] = backup.relative_to(project_root).as_posix()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Repair after validation and backup; default is dry run")
    args = parser.parse_args()
    print(json.dumps(repair(ROOT, apply=args.apply), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
