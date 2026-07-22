import copy
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class JsonStore:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        key = os.path.normcase(str(self.path.resolve(strict=False)))
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _read_unlocked(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return copy.deepcopy(default)

    def read(self, default: Any) -> Any:
        with self._lock:
            return self._read_unlocked(default)

    def _write_unlocked(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def write(self, value: Any) -> None:
        with self._lock:
            self._write_unlocked(value)

    @contextmanager
    def locked(self):
        """Hold the path-shared reentrant lock across a compound operation."""
        with self._lock:
            yield

    def update(
        self,
        mutator: Callable[[Any], Any | None],
        default: Any | None = None,
    ) -> Any:
        """Atomically read, mutate, and replace this JSON document."""
        with self._lock:
            current = self._read_unlocked({} if default is None else default)
            result = mutator(current)
            updated = current if result is None else result
            self._write_unlocked(updated)
            return copy.deepcopy(updated)
