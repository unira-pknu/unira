"""Canonical locations for derived preprocessing artifacts."""
from pathlib import Path


def get_preprocessed_root(project_root: Path, dataset: str) -> Path:
    if not dataset or dataset in {".", ".."} or any(c in dataset for c in ("/", "\\", ":")):
        raise ValueError(f"Invalid preprocessing dataset: {dataset!r}")
    return project_root.resolve() / "files" / "preprocessed" / dataset
