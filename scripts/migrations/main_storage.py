"""Move legacy main-site data into category folders and rewrite local references.

Run without --apply to inspect the plan. Existing targets are never overwritten
with different data, and every move is constrained to the repository.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from scripts.crawlers.common.storage import get_dataset_paths, normalize_state
from scripts.crawlers.main.paths import main_root, page_json, page_root, derived_root, asset_type, document_kind


ROOT = Path(__file__).resolve().parents[2]
DATASETS = ("pknu_student_life", "pknu_notice", "pknu_main")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    if resolved == root.resolve():
        raise ValueError("workspace root cannot be a migration target")
    return resolved


def build_plan(root: Path) -> dict[Path, Path]:
    target_root = main_root(root)
    moves: dict[Path, Path] = {}
    categories: dict[tuple[str, str, str], str] = {}

    def add(source: Path, target: Path):
        source, target = inside(root, source), inside(root, target)
        if source != target:
            if source in moves and moves[source] != target:
                raise ValueError(f"conflicting migration for {source}")
            moves[source] = target

    # Resolve documents first so assets follow their parent page's category.
    for dataset in DATASETS[:2]:
        old = root / "files" / dataset
        paths = get_dataset_paths(root, dataset)
        for source in (old / "output" / "json").rglob("*.json"):
            category = source.relative_to(old / "output" / "json").parts[0]
            doc = json.loads(source.read_text(encoding="utf-8"))
            if dataset == "pknu_student_life" and re.search(r"/main/103(?:$|[?])", str(doc.get("url", ""))):
                category = "수강신청"
            categories[(dataset, source.parent.name, source.stem)] = category
            add(source, paths.document_json(category, source.stem, kind=document_kind(doc)))

    # Link each specialized main-site image to its originating page.
    image_targets: dict[str, Path] = {}
    old_main = root / "files" / "pknu_main"
    for source in (old_main / "output" / "major_program").glob("*.json"):
        doc = json.loads(source.read_text(encoding="utf-8"))
        for image in doc.get("images", []):
            if image.get("saved_path"):
                image_targets[image["saved_path"]] = page_root(root, doc["page_id"]) / "files" / f'main_{doc["page_id"]}' / "images" / Path(image["saved_path"]).name

    for dataset in DATASETS:
        old = root / "files" / dataset
        paths = get_dataset_paths(root, dataset) if dataset != "pknu_main" else None
        if dataset == "pknu_main":
            legacy_entries = [p for p in old.iterdir() if p.name in {"output", "preprocessed", "vectorized", "state.json", "state.json.bak"} or p.name.startswith("_retired")] if old.exists() else []
            sources = [source for entry in legacy_entries for source in (entry.rglob("*") if entry.is_dir() else [entry])]
        else:
            sources = old.rglob("*")
        for source in sorted(sources):
            if not source.is_file() or source in moves:
                continue
            rel = source.relative_to(old)
            parts = rel.parts
            if parts[0] in {"preprocessed", "vectorized"}:
                add(source, derived_root(root, dataset) / rel)
            elif parts[0].startswith("_retired"):
                add(source, target_root / "_archive" / dataset / rel)
            elif parts[0] in {"state.json", "state.json.bak"}:
                add(source, target_root / "_state" / (dataset + ".json" + (".bak" if parts[0].endswith(".bak") else "")))
            elif parts[0] == "output":
                tail = parts[1:]
                if dataset != "pknu_main" and tail and tail[0] in {"html", "files"} and len(tail) >= 3:
                    category, slug = tail[1], Path(tail[2]).stem if tail[0] == "html" else tail[2]
                    category = categories.get((dataset, category, slug), category)
                    if tail[0] == "html":
                        add(source, paths.document_html(category, slug))
                    else:
                        asset_rel = Path(*tail[3:])
                        if asset_rel.parts and asset_rel.parts[0] == "images":
                            target = paths.attachment_dir(category, slug) / asset_rel
                        else:
                            target = paths.attachment_dir(category, slug) / asset_type(source.name) / asset_rel
                        add(source, target)
                elif tail and tail[0] == "runs":
                    add(source, target_root / "_runs" / dataset / Path(*tail[1:]))
                elif tail and (tail[0].startswith("_interrupted") or tail[0] == "deleted"):
                    add(source, target_root / "_archive" / dataset / Path(*tail))
                elif dataset == "pknu_main" and tail and tail[0] == "pknu_today":
                    if "runs" in tail:
                        add(source, target_root / "_runs" / "pknu_today" / source.name)
                    else:
                        pid = int(re.search(r"main_(\d+)_", source.name).group(1))
                        add(source, page_root(root, pid) / "json" / "posts" / source.name)
                elif dataset == "pknu_main" and tail and tail[0] == "images":
                    add(source, image_targets.get(source.relative_to(root).as_posix(), target_root / "_discovery" / "images" / source.name))
                elif len(tail) > 1 and tail[0] in {"academic_calendar", "major_program", "organization", "교육과정", "학사정보"}:
                    match = re.search(r"main_(\d+)", source.name)
                    if match:
                        pid = int(match.group(1))
                        add(source, target_root / "_runs" / dataset / source.name if "route" in source.stem else page_json(root, pid, source.name))
                    else:
                        add(source, target_root / "_archive" / dataset / rel)
                else:
                    add(source, target_root / "_runs" / dataset / Path(*tail))

    # Keep derived filenames aligned with the new preprocessing layout, so a
    # subsequent run updates the existing vector instead of creating a duplicate.
    derived_exact: dict[Path, Path] = {}
    derived_prefixes: list[tuple[Path, Path]] = []
    for source, target in list(moves.items()):
        for dataset in DATASETS[:2]:
            old_output = root / "files" / dataset / "output"
            if not source.is_relative_to(old_output):
                continue
            rel = source.relative_to(old_output)
            if rel.parts[0] not in {"json", "files"}:
                continue
            kind = rel.parts[0]
            marker = target.parts.index(kind, len(root.parts) + 2)
            leaf = Path(*target.parts[:marker])
            section = get_dataset_paths(root, dataset).output
            source_rel = Path(*target.parts[marker + 1:])
            new_processed = derived_root(root, dataset) / "preprocessed" / kind / leaf.relative_to(section)
            old_processed = root / "files" / dataset / "preprocessed" / kind
            if kind == "json":
                before = old_processed / Path(*rel.parts[1:])
                after = new_processed / source_rel
            elif source.suffix.lower() == ".zip":
                before = old_processed / "zip" / Path(*rel.parts[1:]).with_suffix("")
                after = new_processed / "zip" / source_rel.with_suffix("")
                derived_prefixes.append((before, after))
                continue
            else:
                ext = source.suffix.lower().lstrip(".") or "unknown"
                before = (old_processed / ext / Path(*rel.parts[1:])).with_suffix(".json")
                after = (new_processed / ext / source_rel).with_suffix(".json")
            derived_exact[before] = after
    for source in list(moves):
        for dataset in DATASETS[:2]:
            old_root = root / "files" / dataset
            for stage in ("preprocessed", "vectorized"):
                if not source.is_relative_to(old_root / stage):
                    continue
                canonical = old_root / "preprocessed" / source.relative_to(old_root / stage)
                target = derived_exact.get(canonical)
                if target is None:
                    for before, after in derived_prefixes:
                        if canonical.is_relative_to(before):
                            target = after / canonical.relative_to(before)
                            break
                if target is not None:
                    moves[source] = derived_root(root, dataset) / stage / target.relative_to(derived_root(root, dataset) / "preprocessed")

    discovery = root / "files" / "_discovery" / "pknu_main"
    for source in discovery.rglob("*"):
        if source.is_file():
            add(source, target_root / "_discovery" / source.relative_to(discovery))
    for dataset in DATASETS[:2]:
        source = root / f"state_{dataset}.json"
        if source.exists():
            current = root / "files" / dataset / "state.json"
            target = target_root / "_archive" / dataset / "legacy_state.json" if current.exists() else target_root / "_state" / f"{dataset}.json"
            add(source, target)
    return moves


def migrate(root: Path, *, apply: bool, progress=None) -> dict:
    root = root.resolve()
    def announce(message: str):
        if progress:
            progress(message)
    announce("Building migration plan")
    moves = build_plan(root)
    old_roots = {root / "files" / ds for ds in DATASETS[:2]}
    prefixes = {f"files/{ds}/preprocessed/": f"files/pknu_main/_derived/{ds}/preprocessed/" for ds in DATASETS}
    prefixes.update({f"files/{ds}/vectorized/": f"files/pknu_main/_derived/{ds}/vectorized/" for ds in DATASETS})
    prefixes["files/_discovery/pknu_main/"] = "files/pknu_main/_discovery/"
    exact = {source.relative_to(root).as_posix(): target.relative_to(root).as_posix() for source, target in moves.items()}
    announce(f"Checking hashes and collisions for {len(moves)} files")
    signatures = {source: digest(source) for source in moves}
    targets: dict[Path, Path] = {}
    for source, target in moves.items():
        previous = targets.get(target)
        if previous and signatures[previous] != signatures[source]:
            raise FileExistsError(f"two different files target {target}")
        targets[target] = source
        if target.exists() and digest(target) != signatures[source]:
            raise FileExistsError(f"different target already exists: {target}")

    # Preserve valid local references while ignoring references already missing
    # in legacy data. Do not modify source text, IDs, hashes or embedded URLs.
    valid_refs: set[str] = set()
    string_cache: dict[str, str] = {}
    root_prefix = root.as_posix() + "/"
    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) if key not in {"content", "text", "raw_ocr_text", "id", "source_id", "document_id", "content_hash", "embedding", "embeddings", "vector"} else item for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, str):
            return value
        if value in string_cache:
            return string_cache[value]
        normalized = value.replace("\\", "/")
        absolute = normalized.casefold().startswith(root_prefix.casefold())
        relative = normalized[len(root_prefix):] if absolute else normalized
        target = exact.get(relative)
        # Archive members use a virtual path; only the archive itself is local.
        archive, separator, member = relative.partition("!/")
        if target is None and separator and archive in exact:
            target = exact[archive] + separator + member
        if target is None:
            for before, after in prefixes.items():
                if relative == before.rstrip("/"):
                    target = after.rstrip("/")
                    break
                if relative.startswith(before):
                    target = after + relative[len(before):]
                    break
        if target is None:
            string_cache[value] = value
            return value
        if separator and archive in exact:
            valid_refs.add(exact[archive])
        elif relative in exact or (root / relative).is_file():
            valid_refs.add(target)
        result = (root_prefix + target) if absolute else target
        string_cache[value] = result
        return result

    changes: dict[Path, object] = {}
    announce("Rewriting JSON references in memory")
    # Shared review artifacts can link to the relocated originals too.
    sources = set(moves)
    for shared in (root / "files" / "_reviewed", root / "files" / "_discovery"):
        sources.update(p for p in shared.rglob("*.json") if p.is_file())
    for source in sorted(sources):
        if source.suffix not in {".json", ".jsonl"} and source.name not in {"state.json.bak"}:
            continue
        try:
            if source.suffix == ".jsonl":
                payload = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
            else:
                payload = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        updated = rewrite(payload)
        target = moves.get(source, source)
        if target.parent.name == "_state":
            dataset = target.name.removesuffix(".bak").removesuffix(".json")
            updated = normalize_state(updated, dataset, "posts" if dataset == "pknu_notice" else "items")
        if updated != payload:
            changes[target] = updated

    report = {"file_count": len(moves), "rewritten_files": len(changes),
              "bytes": sum(p.stat().st_size for p in moves),
              "destinations": dict(Counter(target.relative_to(main_root(root)).parts[0] for target in moves.values())),
              "applied": apply}
    if not apply:
        return report
    announce(f"Moving {len(moves)} files and verifying their hashes")
    for source, target in moves.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            source.unlink()  # identical collision checked before moving anything
        else:
            source.replace(target)
        if digest(target) != signatures[source]:
            raise RuntimeError(f"file changed during migration: {target}")
    announce(f"Saving {len(changes)} updated JSON files")
    for target, payload in changes.items():
        temporary = target.with_suffix(target.suffix + ".tmp")
        if target.suffix == ".jsonl":
            data = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in payload) + "\n"
        else:
            data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(target)
    missing = sorted(ref for ref in valid_refs if not (root / ref).is_file())
    if missing:
        raise RuntimeError("migrated references missing: " + ", ".join(missing[:10]))
    cleanup_roots = [*old_roots, root / "files" / "pknu_main" / "output", root / "files" / "pknu_main" / "preprocessed",
                     root / "files" / "pknu_main" / "_retired_duplicates_20260930", root / "files" / "_discovery" / "pknu_main"]
    for old in cleanup_roots:
        inside(root, old)
        if old.exists():
            for directory in sorted((p for p in old.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                inside(root, directory)
                if not any(directory.iterdir()):
                    directory.rmdir()
            if not any(old.iterdir()):
                old.rmdir()
    report["verified_references"] = len(valid_refs)
    report["binary_files_verified"] = sum(source.suffix not in {".json", ".jsonl"} for source in moves)
    report_path = main_root(root) / "_runs" / "storage_migration.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    map_path = report_path.with_name("storage_migration_paths.json")
    map_path.write_text(json.dumps(exact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(ROOT, apply=args.apply, progress=lambda message: print(message, flush=True)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
