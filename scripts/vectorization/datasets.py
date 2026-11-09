"""Shared central preprocessing inputs and vector outputs."""
from pathlib import Path
from functools import lru_cache
from scripts.crawlers.departments.config import load_registry
from scripts.crawlers.common.storage import department_datasets
from scripts.preprocessing.storage import get_preprocessed_root

TABLE_FAMILIES = ("rag", "pknu_notice", "pknu_main", "rule", "pknu_syllabus")


def table_family(dataset: str, metadata: dict | None = None) -> str:
    """Route a record without changing its original ingestion identity."""
    if dataset in department_datasets():
        return "rag"
    if dataset == "pknu_main":
        origin = (metadata or {}).get("source_dataset")
        if origin not in (None, "", "pknu_main", "pknu_notice", "pknu_student_life"):
            raise ValueError(f"Unexpected main-site source dataset: {origin}")
        return "pknu_notice" if origin == "pknu_notice" else "pknu_main"
    if dataset == "pknu_student_life":
        return "pknu_main"
    if dataset in {"pknu_notice", "rule", "pknu_syllabus"}:
        return dataset
    raise ValueError(f"Dataset has no database table mapping: {dataset}")


def dataset_families(dataset: str) -> tuple[str, ...]:
    return ("pknu_main", "pknu_notice") if dataset == "pknu_main" else (table_family(dataset),)


@lru_cache(maxsize=1)
def _department_registry():
    return load_registry()


def department_attributes(dataset: str) -> dict:
    config = _department_registry()[dataset]
    return {
        "department_id": config.dataset,
        "department_name": config.name,
        "college_name": config.college,
        "department_homepage": config.base_url,
        "department_majors": list(config.majors),
    }


def get_vector_paths(project_root: Path, dataset: str):
    source = get_preprocessed_root(project_root, dataset)
    target = project_root / "vectorized" / source.name
    return {"input_root": source, "output_root": target}


def dataset_paths(project_root: Path):
    names = set(department_datasets()) | {"rule", "pknu_notice", "pknu_student_life", "pknu_main", "pknu_syllabus"}
    source = project_root / "preprocessed"
    if source.is_dir():
        names.update(p.name for p in source.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    return {name: get_vector_paths(project_root, name) for name in sorted(names)}


def database_tables(project_root: Path):
    """Five independent families; a main-site index can span notice/main tables."""
    names = set(department_datasets()) | {"rule", "pknu_notice", "pknu_student_life", "pknu_main", "pknu_syllabus"}
    result = {}
    for name in sorted(names):
        prefix = table_family(name)
        result[name] = {"index": get_vector_paths(project_root, name)["output_root"] / "index.jsonl",
                        "sources": f"{prefix}_sources", "chunks": f"{prefix}_chunks"}
    return result
