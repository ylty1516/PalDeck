"""Shared re-entrant thread and OS-released cross-process game write lock."""

from __future__ import annotations

import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .manifest_store import validate_no_reparse_ancestors

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_DEPTHS = threading.local()
_LOCK_NAME = ".mod-service.lock"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def game_lock_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / _LOCK_NAME


def _open_safe_lock_file(path: Path) -> BinaryIO:
    handle = open(path, "a+b", buffering=0)
    try:
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & _REPARSE_POINT)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise PermissionError("游戏目录锁文件不安全")
        if opened.st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        return handle
    except BaseException:
        handle.close()
        raise


def _try_os_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def game_write_lock(
    data_dir: str | os.PathLike[str], *, timeout: float = 10.0
) -> Iterator[None]:
    """Serialize writes; the OS releases the persistent file lock on process exit."""
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

    handle: BinaryIO | None = None
    locked = False
    deadline = time.monotonic() + timeout
    try:
        directory.mkdir(parents=True, exist_ok=True)
        validate_no_reparse_ancestors(directory)
        handle = _open_safe_lock_file(lock_path)
        while not _try_os_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError("等待游戏目录跨进程写事务锁超时")
            time.sleep(0.02)
        locked = True
        depths[key] = 1
        yield
    finally:
        depths.pop(key, None)
        if handle is not None:
            try:
                if locked:
                    _unlock(handle)
            finally:
                handle.close()
        thread_lock.release()
