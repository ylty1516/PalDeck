"""Shared re-entrant thread and cross-process lock for game filesystem writes."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .manifest_store import validate_no_reparse_ancestors

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_DEPTHS = threading.local()
_LOCK_NAME = ".mod-service.lock"


def game_lock_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / _LOCK_NAME


@contextmanager
def game_write_lock(
    data_dir: str | os.PathLike[str], *, timeout: float = 10.0
) -> Iterator[None]:
    """Serialize all writes sharing one data directory, including nested calls."""
    directory = Path(data_dir)
    validate_no_reparse_ancestors(directory)
    lock_path = game_lock_path(directory)
    key = os.path.normcase(str(Path(os.path.abspath(lock_path))))
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError("等待游戏目录写事务锁超时")

    depths = getattr(_LOCK_DEPTHS, "values", None)
    if depths is None:
        depths = {}
        _LOCK_DEPTHS.values = depths
    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
            thread_lock.release()
        return

    acquired_file = False
    deadline = time.monotonic() + timeout
    try:
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(descriptor)
                acquired_file = True
                depths[key] = 1
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待游戏目录跨进程写事务锁超时")
                time.sleep(0.02)
        yield
    finally:
        if acquired_file:
            depths.pop(key, None)
            lock_path.unlink(missing_ok=True)
        thread_lock.release()
