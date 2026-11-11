"""Keep pytest scratch files in a fresh, workspace-local directory."""

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Run before pytest's TempPathFactory is created. An explicit CLI basetemp
    # still takes precedence; never reuse a directory from another test run.
    if config.option.basetemp is not None:
        return
    workspace = Path(__file__).resolve().parent
    scratch_root = (workspace / "var" / "pytest").resolve()
    if not scratch_root.is_relative_to(workspace):
        raise pytest.UsageError("pytest scratch directory must stay inside the workspace")
    scratch_root.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(scratch_root / f"run-{uuid4().hex}")
