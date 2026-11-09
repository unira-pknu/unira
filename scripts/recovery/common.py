"""Shared local I/O and receipts; no database or model imports."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from scripts.vectorization.recovery_control import replace_with_retry
from scripts.artifact_paths import PROJECT_ROOT, resolve_artifact_path


def local_path(path):
    path = Path(path)
    if not path.is_absolute() or path.is_relative_to(PROJECT_ROOT):
        return resolve_artifact_path(PROJECT_ROOT, path)
    return path


def read(path):
    return json.loads(local_path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with local_path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inside(path, *, root, boundary=None):
    root = Path(root).resolve()
    resolved = resolve_artifact_path(root, path)
    limit = resolve_artifact_path(root, boundary or root)
    if not resolved.is_relative_to(limit):
        raise ValueError(f"Path outside workspace/run: {path}")
    return resolved


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
        replace_with_retry(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def event(stage, **values):
    print(json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)


def receipt(run, **values):
    path = Path(run) / "receipt.json"
    state = read(path) if path.exists() else {}
    status = values.get("status")
    # Failed/interrupted attempts retain their evidence until a successful transition.
    if status and status not in {"needs_attention", "interrupted"}:
        if state.get("error"):
            state.setdefault("recovered_events", []).append({
                "phase": state.get("failed_phase"), "error": state["error"],
                "recorded_at": state.get("updated_at"),
            })
        state.pop("error", None)
        state.pop("failed_phase", None)
    state.update(values, updated_at=datetime.now(timezone.utc).isoformat())
    atomic(path, state)
