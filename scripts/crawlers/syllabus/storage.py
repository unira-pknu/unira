"""Verified original PDFs and crash-safe progress, compatible with existing IDs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    # Windows may briefly deny a rename while an indexer, scanner or editor
    # holds the destination. Keep the previous JSON intact and retry the same
    # fully flushed temporary file; persistent permission errors still fail.
    deadline = time.monotonic() + 5
    while True:
        try:
            temporary.replace(path)
            break
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


@contextmanager
def exclusive_run(path: Path, *, wait_timeout: float = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_timeout
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"실행 잠금이 있습니다: {path}. 다른 실행이 없는지 확인하세요.") from exc
            time.sleep(0.1)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def collection_run(output: Path, *, year: int | None = None, term: str | None = None):
    """Different terms may run together; overlapping/global runs may not."""
    directory = output / "_state"
    global_lock = directory / "run.lock"
    scoped_lock = directory / f"run-{year}-{term}.lock" if year is not None else global_lock
    with ExitStack() as held:
        with exclusive_run(directory / "coordination.lock", wait_timeout=30):
            conflicts = [global_lock, scoped_lock] if year is not None else [global_lock, *directory.glob("run-*.lock")]
            if any(path.exists() for path in conflicts):
                raise RuntimeError("동일 학기 또는 전체 범위의 수집이 이미 실행 중입니다.")
            held.enter_context(exclusive_run(scoped_lock))
        yield


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def target_path(output: Path, task: dict) -> Path:
    def safe(value: str) -> str:
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:45] or "unknown"
    filename = "_".join(safe(str(task[key])) for key in ("course_no", "section", "course_name", "variant"))
    destination = output / str(task["year"]) / task["term"] / f"{filename}_{task['id']}.pdf"
    if not destination.resolve().is_relative_to(output.resolve()):
        raise ValueError("결과 경로가 지정한 저장 폴더 밖을 가리킵니다.")
    return destination


def save_pdf(data: bytes, task: dict, output: Path) -> dict:
    from scripts.crawlers.syllabus.catalog import SITE_URL
    from scripts.crawlers.syllabus.documents import inspect_pdf
    from scripts.crawlers.syllabus.protocol import EMBED_URL, REPORT_BASE

    identity = inspect_pdf(data, task)
    destination = target_path(output, task)
    if destination.exists():
        raise FileExistsError(f"이력에 없는 기존 결과를 덮어쓰지 않습니다: {destination}")
    incoming = output / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(suffix=".pdf", prefix=task["id"] + "_", dir=incoming)
    source = Path(name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish only complete bytes, atomically and without replacing originals.
    # Keep the temporary name until the receipt is committed.
    os.link(source, destination)
    checksum = hashlib.sha256(data).hexdigest()
    if digest(destination) != checksum:
        raise ValueError("저장한 PDF의 해시가 일치하지 않습니다.")
    return {
        "status": "downloaded", "path": destination.relative_to(output).as_posix(),
        "incoming_path": source.relative_to(output).as_posix(),
        "sha256": checksum, "bytes": len(data), "task": task, **identity,
        "transport": "direct_http", "source_url": SITE_URL, "embed_url": EMBED_URL,
        "service_url": REPORT_BASE + "/service", "download_url": REPORT_BASE + "/download",
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "downloads": {}}
    if state.get("version") != 1 or not isinstance(state.get("downloads"), dict):
        raise ValueError("저장 상태 파일이 올바르지 않습니다.")
    return state


def load_state(path: Path) -> dict:
    with exclusive_run(path.with_suffix(".lock"), wait_timeout=30):
        return _read_state(path)


def progress_scope(task: dict) -> str:
    return f"{task['year']}-{task['term']}"


def commit_progress(path: Path, *, entry: dict | None = None, error: dict | None = None) -> dict:
    """Read and merge under one lock so parallel terms retain every receipt."""
    with exclusive_run(path.with_suffix(".lock"), wait_timeout=30):
        state = _read_state(path)
        if entry is not None:
            state["downloads"][entry["task"]["id"]] = entry
            scope = progress_scope(entry["task"])
            state.setdefault("errors", {}).pop(scope, None)
            previous = state.get("last_error")
            if previous and (previous.get("scope") == scope or previous.get("task") and progress_scope(previous["task"]) == scope):
                state.pop("last_error", None)
        if error is not None:
            state["last_error"] = error
            state.setdefault("errors", {})[error.get("scope", "unknown")] = error
        atomic_json(path, state)
        return state


def is_complete(output: Path, task: dict, state: dict) -> bool:
    entry = state.get("downloads", {}).get(task["id"])
    if not entry or entry.get("status") != "downloaded":
        return False
    path = target_path(output, task)
    if entry.get("path") != path.relative_to(output).as_posix():
        raise ValueError("저장 상태의 경로가 예상 경로와 다릅니다.")
    if not path.is_file() or digest(path) != entry["sha256"]:
        raise ValueError(f"기존 성공 파일이 없거나 변경됐습니다: {path}")
    return True
