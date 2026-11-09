"""Resume policy and transaction boundaries for selective ingestion jobs.

Importing this module performs no database access or job execution.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import os
import time


def replace_with_retry(source: Path, target: Path, *, attempts: int = 8) -> None:
    """Keep the old checkpoint intact while Windows readers briefly hold it open."""
    if attempts < 1:
        raise ValueError("Replacement attempts must be positive")
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == attempts - 1:
                raise
            time.sleep(min(0.05 * 2 ** attempt, 0.5))


class PeerWorkerStopped(RuntimeError):
    """A worker stopped because another worker reported the original error."""


@dataclass(frozen=True)
class LoadOptions:
    workers: int = 1
    chunk_batch_size: int = 32
    source_batch_size: int = 16
    transaction_chunk_limit: int = 256
    statement_timeout_seconds: int = 600
    max_batch_retries: int = 2

    def __post_init__(self):
        if not 1 <= self.workers <= 4:
            raise ValueError('workers must be between 1 and 4')
        for name in ('chunk_batch_size', 'source_batch_size', 'transaction_chunk_limit', 'statement_timeout_seconds'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if self.max_batch_retries < 0:
            raise ValueError('max_batch_retries must not be negative')


def checkpoint_ids(progress: dict, expected_ids: Iterable[str]) -> set[str]:
    """Reject damaged checkpoints rather than silently skipping unknown rows."""
    completed = progress.get('completed', [])
    concurrent = progress.get('concurrently_present', [])
    if not isinstance(completed, list) or not isinstance(concurrent, list):
        raise ValueError('Checkpoint source IDs must be lists')
    values = completed + concurrent
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError('Checkpoint contains an invalid source ID')
    if len(set(values)) != len(values):
        raise ValueError('Checkpoint contains duplicate source IDs')
    result = set(values)
    if not result.issubset(set(expected_ids)):
        raise ValueError('Checkpoint contains IDs outside the confirmed selection')
    return result


def resume_plan(state: dict, progress: dict, expected_ids: Iterable[str]) -> list[str]:
    expected = set(expected_ids)
    done = checkpoint_ids(progress, expected)
    if state.get('failed_phase') == 'prepare':
        raise ValueError('Resolve the preparation error before resuming')
    if state.get('status') == 'complete':
        if done != expected or not state.get('verification') or state.get('search_verification', {}).get('status') != 'passed':
            raise ValueError('Completion receipt is inconsistent with verification/checkpoints')
        return []
    loaded = progress.get('status') in ('loaded', 'complete')
    if loaded and done != expected:
        raise ValueError('Loaded checkpoint does not cover the confirmed selection')
    phases = [] if loaded else ['load']
    if not loaded or state.get('priorities_updated') != len(progress.get('completed', [])):
        phases.append('priorities')
    # Recheck real database contents before search, even when resuming a search failure.
    return phases + ['verify', 'search']


def write_chunk_batches(rows: list, *, batch_size: int, max_retries: int,
                        savepoint: Callable, write: Callable, retryable: tuple,
                        on_retry: Callable) -> None:
    """Retry failed statements inside savepoints; the caller owns the outer transaction.

    Never update a document checkpoint here: only the caller can do so after
    committing its outer source transaction.
    """
    if batch_size <= 0 or max_retries < 0:
        raise ValueError('Invalid retry/batch configuration')
    offset = 0
    size = batch_size
    attempts = 0
    while offset < len(rows):
        part = rows[offset:offset + size]
        try:
            with savepoint():
                write(part)
        except retryable as error:
            if attempts >= max_retries:
                raise
            attempts += 1
            size = max(1, min(size, len(part)) // 2)
            on_retry(error, attempts, size)
        else:
            offset += len(part)
            attempts = 0


@contextmanager
def recovery_lock(path: Path):
    """Prevent two pipeline invocations for the same run without a stale PID file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('This recovery pipeline is already running') from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
