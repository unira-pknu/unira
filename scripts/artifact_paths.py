"""Artifact locations and read compatibility for byte-preserved historical receipts."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELOCATIONS = (
    ("files/preprocessed", "preprocessed"),
    ("files/vectorized", "vectorized"),
    ("files/_archive", "var/recovery"),
)


def resolve_artifact_path(project_root: Path, path: str | Path) -> Path:
    """Resolve known old prefixes without rewriting signed plans or vector JSONL."""
    root = project_root.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path outside workspace: {path}")
    relative = candidate.relative_to(root)
    for old, new in RELOCATIONS:
        prefix = Path(old)
        if relative.is_relative_to(prefix):
            target = root / new / relative.relative_to(prefix)
            # Legacy fixtures/archives can still be read before migration. Never
            # silently select one of two independently populated locations.
            if candidate.exists():
                if target.exists():
                    raise ValueError(f"Both legacy and canonical artifacts exist: {candidate}, {target}")
                return candidate
            target = target.resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Relocated artifact outside workspace: {path}")
            return target
    return candidate


@lru_cache(maxsize=16)
def _migration(root: str, modified: int) -> dict:
    return json.loads((Path(root) / "var/storage_paths.json").read_text(encoding="utf-8-sig"))


def reference_path(path: Path, project_root: Path) -> str:
    """Keep historical provenance/ownership IDs stable after physical relocation."""
    root = project_root.resolve()
    relative = resolve_artifact_path(root, path).relative_to(root)
    marker = root / "var/storage_paths.json"
    if marker.is_file():
        migration = _migration(str(root), marker.stat().st_mtime_ns)
        for old, new in RELOCATIONS:
            prefix = Path(new)
            if not relative.is_relative_to(prefix):
                continue
            suffix = relative.relative_to(prefix)
            if new == "var/recovery" and (not suffix.parts or suffix.parts[0] not in migration.get("legacy_recovery_runs", [])):
                continue
            if old in migration.get("moved_roots", []):
                return (Path(old) / suffix).as_posix()
    return relative.as_posix()
