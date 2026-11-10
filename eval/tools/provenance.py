"""Small, credential-free provenance records for reproducible evaluation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def timestamp() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backend_configurations(rows: list[dict]) -> list[dict]:
    configurations = {}
    for row in rows:
        snapshot = row.get("evaluation_evidence")
        if not isinstance(snapshot, dict):
            continue
        configuration = {key: snapshot.get(key) for key in ("backend_revision", "system_prompt_sha256", "settings")}
        configurations[json.dumps(configuration, sort_keys=True)] = configuration
    return list(configurations.values())


def code_state() -> dict:
    def git(*args: str) -> str | None:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL,
            ).decode("utf-8").strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    paths = [
        *PROJECT_ROOT.glob("backend/app/**/*.py"),
        *PROJECT_ROOT.glob("backend/prompts/*.txt"),
        *PROJECT_ROOT.glob("eval/tools/*.py"),
        PROJECT_ROOT / "backend/requirements.txt",
        PROJECT_ROOT / "backend/requirements-runtime.txt",
        PROJECT_ROOT / "scripts/crawlers/departments/registry.json",
    ]
    hashes = {path.relative_to(PROJECT_ROOT).as_posix(): file_sha256(path)
              for path in sorted(paths) if path.is_file()}
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_status": git("status", "--porcelain"),
        "files_sha256": hashes,
        "code_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    }
