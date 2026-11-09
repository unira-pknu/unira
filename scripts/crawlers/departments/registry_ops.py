"""Safe planning and application of reviewed department discovery results."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from scripts.crawlers.departments.config import DepartmentConfig, SectionConfig
from scripts.crawlers.departments.freshness import (
    assess_result_freshness, probe_discovery_warnings,
)
from scripts.vectorization.recovery_control import replace_with_retry


SECTION_FIELDS = ("id", "name", "category", "kind", "path", "document_type", "bbs_id")


def safe_site_key(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", value.lower()).strip("._") or "site"


def save_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def accepted_sections(
    discovery: dict[str, Any], *, adapter_name: str = "numeric_cms"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize usable candidates while reporting entries requiring manual review."""
    sections: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for raw in discovery.get("sections") or []:
        if raw.get("status") == "unsupported":
            continue
        section = {field: raw.get(field) for field in SECTION_FIELDS}
        section["enabled"] = True
        section_id = str(section.get("id") or "").strip()
        kind = section.get("kind")
        if not section_id or section_id in seen_ids:
            warnings.append(f"skipped invalid or duplicate section id: {section_id!r}")
            continue
        if kind not in {"board", "static_page"}:
            warnings.append(f"skipped unsupported section kind: {section_id}")
            continue
        if kind == "board" and adapter_name == "numeric_cms" and not section.get("bbs_id"):
            warnings.append(f"skipped board without bbs_id: {section_id}")
            continue
        if not str(section.get("path") or "").startswith("/"):
            warnings.append(f"skipped section without absolute path: {section_id}")
            continue
        try:
            SectionConfig.from_dict(section)
        except ValueError as exc:
            warnings.append(f"skipped invalid section {section_id}: {exc}")
            continue
        seen_ids.add(section_id)
        sections.append(section)
    return sections, warnings


def plan_discovery_updates(
    registry_payload: dict[str, Any],
    configs: Iterable[DepartmentConfig],
    discovery_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an updated payload and a non-mutating review report."""
    updated = json.loads(json.dumps(registry_payload))
    items = {item["dataset"]: item for item in updated.get("departments") or []}
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for config in configs:
        if not config.source_catalog_key:
            skipped.append({"dataset": config.dataset, "reason": "missing_source_catalog_key"})
            continue
        path = discovery_root / safe_site_key(config.source_catalog_key) / "discovery.json"
        if not path.exists():
            skipped.append({"dataset": config.dataset, "reason": "discovery_not_found", "path": path.as_posix()})
            continue
        try:
            discovery = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"dataset": config.dataset, "reason": "invalid_discovery", "message": str(exc)})
            continue
        freshness = assess_result_freshness(discovery, config)
        if not freshness["fresh"]:
            skipped.append({
                "dataset": config.dataset,
                "reason": "legacy_discovery" if freshness["status"] == "legacy" else "stale_discovery",
                "freshness": freshness,
                "path": path.as_posix(),
            })
            continue
        probe_path = path.with_name("probe.json")
        probe: dict[str, Any] | None = None
        if probe_path.exists():
            try:
                probe = json.loads(probe_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                skipped.append({
                    "dataset": config.dataset, "reason": "invalid_probe", "message": str(exc),
                })
                continue
            probe_freshness = assess_result_freshness(probe, config)
            if not probe_freshness["fresh"]:
                skipped.append({
                    "dataset": config.dataset,
                    "reason": "legacy_probe" if probe_freshness["status"] == "legacy" else "stale_probe",
                    "freshness": probe_freshness,
                    "path": probe_path.as_posix(),
                })
                continue
        status_warnings = probe_discovery_warnings(probe, discovery)
        if status_warnings:
            skipped.append({
                "dataset": config.dataset, "reason": "probe_discovery_status_conflict",
                "warnings": status_warnings,
            })
            continue
        if discovery.get("status") not in {"success", "partial_success"}:
            skipped.append({"dataset": config.dataset, "reason": "discovery_not_successful", "status": discovery.get("status")})
            continue
        sections, warnings = accepted_sections(discovery, adapter_name=config.adapter)
        if not sections:
            skipped.append({"dataset": config.dataset, "reason": "no_usable_sections", "warnings": warnings})
            continue
        item = items[config.dataset]
        before = len(item.get("sections") or [])
        item["sections"] = sections
        item["enabled"] = True
        if discovery.get("site_prefix"):
            item["site_prefix"] = discovery["site_prefix"]
        changes.append({
            "dataset": config.dataset, "sections_before": before,
            "sections_after": len(sections), "warnings": warnings,
            "freshness": freshness,
            "discovery_path": path.as_posix(),
        })
    report = {
        "status": "ready" if changes else "no_changes",
        "dry_run": True,
        "changes": changes,
        "skipped": skipped,
        "summary": {"changed": len(changes), "skipped": len(skipped)},
    }
    return updated, report
