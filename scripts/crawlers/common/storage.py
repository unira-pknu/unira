"""Shared crawler paths, state persistence, and non-destructive migration helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


STATE_SCHEMA_VERSION = "1.0"
KST = timezone(timedelta(hours=9))


class StateLoadError(RuntimeError):
    pass


def _normalize_state_timestamp(value: object) -> object:
    """Convert known state timestamps to KST without discarding invalid legacy values."""
    if value is None or str(value).strip() == "":
        return value
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    else:
        parsed = parsed.astimezone(KST)
    return parsed.isoformat(timespec="seconds")


@dataclass(frozen=True)
class DatasetPaths:
    project_root: Path
    dataset: str
    root: Path
    state: Path
    output: Path
    json: Path
    html: Path
    files: Path
    deleted: Path
    runs: Path

    def category_json(self, category: str) -> Path:
        return self.json / safe_component(category, "category")

    def category_html(self, category: str) -> Path:
        return self.html / safe_component(category, "category")

    def document_json(self, category: str, slug: str, *, kind: str | None = None) -> Path:
        return self.category_json(category) / f"{safe_component(slug, 'slug')}.json"

    def document_html(self, category: str, slug: str) -> Path:
        return self.category_html(category) / f"{safe_component(slug, 'slug')}.html"

    def attachment_dir(self, category: str, slug: str) -> Path:
        return self.files / safe_component(category, "category") / safe_component(slug, "slug")

    def attachment_file(self, category: str, slug: str, filename: str) -> Path:
        return self.attachment_dir(category, slug) / filename

    def iter_document_json(self):
        yield from self.json.rglob("*.json")

    def deleted_json(self, category: str, slug: str) -> Path:
        return self.deleted / safe_component(category, "category") / f"{safe_component(slug, 'slug')}.json"

    def run_result(self, run_id: str) -> Path:
        return self.runs / f"{safe_component(run_id, 'run_id')}.json"


def safe_component(value: object, label: str) -> str:
    component = str(value or "").strip()
    if not component or component in {".", ".."} or Path(component).is_absolute():
        raise ValueError(f"invalid {label}: {value!r}")
    if component.endswith(".") or "/" in component or "\\" in component or re.search(r'[<>:"|?*\x00-\x1f]', component):
        raise ValueError(f"invalid {label}: {value!r}")
    stem = component.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError(f"invalid {label}: {value!r}")
    return component


@lru_cache(maxsize=1)
def department_storage_paths() -> dict[str, tuple[str, ...]]:
    registry = Path(__file__).resolve().parents[1] / "departments" / "registry.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    return {row["dataset"]: validate_storage_path(row["dataset"], row.get("storage_path"))
            for row in payload["departments"]}


def validate_storage_path(dataset: str, storage_path: Iterable[str] | None) -> tuple[str, ...]:
    if storage_path is None:
        return (safe_component(dataset, "dataset"),)
    if not isinstance(storage_path, (tuple, list)) or not storage_path:
        raise ValueError("storage_path must be a non-empty list of directory names")
    parts = tuple(safe_component(part, "storage_path") for part in storage_path)
    if parts[-1] != dataset:
        raise ValueError("storage_path must end with the dataset alias")
    return parts


def department_datasets() -> frozenset[str]:
    return frozenset(department_storage_paths())


def department_legacy_roots(project_root: Path, dataset: str) -> tuple[Path, Path]:
    files = project_root.resolve() / "files"
    dataset = safe_component(dataset, "dataset")
    return files / dataset, files / "department" / dataset


def get_dataset_root(
    project_root: Path, dataset: str, *, group: str | None = None,
    storage_path: Iterable[str] | None = None,
) -> Path:
    dataset_name = safe_component(dataset, "dataset")
    if group not in {None, "department"}:
        raise ValueError(f"unsupported dataset group: {group}")
    files = project_root.resolve() / "files"
    if dataset_name in {"pknu_notice", "pknu_student_life"}:
        return files / "pknu_main"
    if group == "department" or dataset_name in department_datasets():
        parts = (validate_storage_path(dataset_name, storage_path) if storage_path is not None
                 else department_storage_paths().get(dataset_name, (dataset_name,)))
        return files.joinpath("department", *parts)
    return files / dataset_name


def iter_dataset_roots(project_root: Path):
    """Yield dataset leaves, never the college/faculty container directories."""
    files = project_root.resolve() / "files"
    if files.is_dir():
        for root in sorted(files.iterdir()):
            if root.is_dir() and root.name == "department":
                def leaves(parent: Path):
                    for child in sorted(parent.iterdir()):
                        if not child.is_dir() or child.name.startswith("_"):
                            continue
                        if child.name in department_datasets() or any(
                            (child / marker).exists() for marker in
                            ("output", "preprocessed", "vectorized", "state.json")
                        ):
                            yield child
                        else:
                            yield from leaves(child)
                yield from leaves(root)
            elif root.is_dir() and not root.name.startswith("_") and root.name != "preprocessed":
                yield root


def get_dataset_paths(
    project_root: Path, dataset: str, *, group: str | None = None,
    storage_path: Iterable[str] | None = None,
) -> DatasetPaths:
    dataset_name = safe_component(dataset, "dataset")
    if dataset_name in {"pknu_notice", "pknu_student_life"}:
        from scripts.crawlers.main.paths import get_main_dataset_paths
        return get_main_dataset_paths(project_root, dataset_name)
    root = get_dataset_root(project_root, dataset_name, group=group, storage_path=storage_path)
    output = root / "output"
    return DatasetPaths(
        project_root=project_root.resolve(), dataset=dataset_name, root=root,
        state=root / "state.json", output=output, json=output / "json",
        html=output / "html", files=output / "files",
        deleted=output / "deleted", runs=output / "runs",
    )


def empty_state(dataset: str) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "dataset": dataset,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "items": {},
    }


def normalize_state(payload: dict[str, Any], dataset: str, legacy_kind: str = "items") -> dict[str, Any]:
    if payload.get("schema_version") == STATE_SCHEMA_VERSION and isinstance(payload.get("items"), dict):
        raw_items = payload["items"]
    elif legacy_kind == "posts" and isinstance(payload.get("posts"), dict):
        raw_items = payload["posts"]
    elif legacy_kind == "items" and isinstance(payload.get("items"), dict):
        raw_items = payload["items"]
    else:
        raw_items = payload

    items: dict[str, dict[str, Any]] = {}
    for key, raw in raw_items.items():
        meta = dict(raw) if isinstance(raw, dict) else {"value": raw}
        source_id = str(meta.get("source_id") or key)
        meta.update({
            "source_id": source_id,
            "slug": str(meta.get("slug") or ""),
            "content_hash": str(meta.get("content_hash") or ""),
            "last_seen_at": _normalize_state_timestamp(meta.get("last_seen_at")),
            "miss_count": int(meta.get("miss_count") or 0),
            "status": str(meta.get("status") or "active"),
        })
        if "updated_at" in meta:
            meta["updated_at"] = _normalize_state_timestamp(meta["updated_at"])
        items[str(key)] = meta
    state = empty_state(dataset)
    state["items"] = items
    return state


def _read_state(path: Path, dataset: str, legacy_kind: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateLoadError(f"state file is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise StateLoadError(f"state root must be an object: {path}")
    try:
        return normalize_state(payload, dataset, legacy_kind)
    except (TypeError, ValueError) as exc:
        raise StateLoadError(f"state content is invalid: {path}") from exc


def save_state_atomic(path: Path, state: dict[str, Any], dataset: str) -> Path:
    normalized = normalize_state(state, dataset)
    normalized["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Preserve the last known-good backup when the current state is corrupt.
            pass
        else:
            shutil.copy2(path, backup)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    state.clear()
    state.update(normalized)
    return path


def load_state_with_migration(
    paths: DatasetPaths,
    *,
    legacy_paths: Iterable[Path] = (),
    legacy_kind: str = "items",
) -> tuple[dict[str, Any], str]:
    if paths.state.exists():
        try:
            return _read_state(paths.state, paths.dataset, "items"), "current"
        except StateLoadError as current_error:
            backup = paths.state.with_suffix(paths.state.suffix + ".bak")
            if backup.exists():
                try:
                    return _read_state(backup, paths.dataset, "items"), "backup"
                except StateLoadError:
                    pass
            raise current_error

    for legacy_path in legacy_paths:
        if not legacy_path.exists():
            continue
        state = _read_state(legacy_path, paths.dataset, legacy_kind)
        save_state_atomic(paths.state, state, paths.dataset)
        return state, f"migrated:{legacy_path}"
    return empty_state(paths.dataset), "new"


def state_migration_plan(paths: DatasetPaths, legacy_paths: Iterable[Path]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if paths.state.exists():
        return [{"action": "skip", "reason": "target_exists", "target": str(paths.state)}]
    for source in legacy_paths:
        if source.exists():
            actions.append({"action": "copy_state", "source": str(source), "target": str(paths.state)})
    if not actions:
        actions.append({"action": "create_state", "target": str(paths.state)})
    return actions


def find_document_json(paths: DatasetPaths, slug: str, legacy_json_roots: Iterable[Path] = ()) -> Path | None:
    safe_slug = safe_component(slug, "slug")
    for root in (paths.json, *legacy_json_roots):
        if not root.exists():
            continue
        matches = list(root.rglob(f"{safe_slug}.json"))
        if matches:
            return matches[0]
    return None


def write_document(
    paths: DatasetPaths, doc: dict[str, Any], category: str, slug: str,
    raw_html: str | None = None,
) -> tuple[Path, Path | None]:
    from scripts.crawlers.main.paths import MainDatasetPaths, document_kind
    kind = document_kind(doc) if isinstance(paths, MainDatasetPaths) else None
    json_path = paths.document_json(category, slug, kind=kind)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path: Path | None = None
    if raw_html is not None:
        html_path = paths.document_html(category, slug)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(raw_html, encoding="utf-8")
    return json_path, html_path


def archive_document(paths: DatasetPaths, category: str, slug: str) -> dict[str, str]:
    sources = {
        "json": paths.document_json(category, slug),
        "html": paths.document_html(category, slug),
        "files": paths.attachment_dir(category, slug),
    }
    asset_root = paths.deleted / safe_component(category, "category") / safe_component(slug, "slug")
    targets = {
        "json": paths.deleted_json(category, slug),
        "html": asset_root / "page.html",
        "files": asset_root / "files",
    }
    for name, source in sources.items():
        if source.exists() and targets[name].exists():
            raise FileExistsError(f"archive target already exists: {targets[name]}")
    moved: dict[str, str] = {}
    for name, source in sources.items():
        if not source.exists():
            continue
        target = targets[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        moved[name] = target.as_posix()
    return moved
