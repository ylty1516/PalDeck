"""Safe native-folder selections and short-lived local import grants."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from backend.manifest_store import validate_no_reparse_ancestors

SUPPORTED_SUFFIXES = frozenset({".zip", ".pak"})


class UnsafeSelectionError(ValueError):
    """A native picker result crosses a reparse or regular-file boundary."""


class SelectionExpiredError(KeyError):
    """A local selection grant is missing or expired."""


def _safe_regular_file(path: Path) -> Path:
    try:
        checked = validate_no_reparse_ancestors(path)
    except (OSError, ValueError) as exc:
        raise UnsafeSelectionError("selection contains a symlink or reparse point") from exc
    if checked.suffix.casefold() not in SUPPORTED_SUFFIXES or not checked.is_file():
        raise UnsafeSelectionError("selection is not a regular ZIP or PAK file")
    return checked.resolve(strict=True)


def list_supported_top_level(folder: Path) -> list[Path]:
    try:
        root = validate_no_reparse_ancestors(folder)
    except (OSError, ValueError) as exc:
        raise UnsafeSelectionError("selected directory is unsafe") from exc
    if not root.is_dir():
        raise UnsafeSelectionError("selected path is not a directory")
    found: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                if path.suffix.casefold() in SUPPORTED_SUFFIXES:
                    found.append(_safe_regular_file(path))
    except OSError as exc:
        raise UnsafeSelectionError("selected directory cannot be read") from exc
    return sorted(found, key=lambda path: (path.name.casefold(), path.name))


class SelectionRegistry:
    """Issue one-file opaque grants for paths returned by the native picker."""

    def __init__(self, *, ttl: float = 15 * 60, max_items: int = 100, clock: Callable[[], float] = time.monotonic):
        if ttl <= 0 or max_items < 1:
            raise ValueError("invalid selection registry limits")
        self.ttl = float(ttl)
        self.max_items = int(max_items)
        self.clock = clock
        self._items: dict[str, tuple[Path, float]] = {}
        self._lock = threading.RLock()

    def _cleanup(self) -> None:
        now = self.clock()
        for token, (_path, expires) in list(self._items.items()):
            if expires <= now:
                self._items.pop(token, None)

    def issue(self, paths: Iterable[Path]) -> list[dict[str, object]]:
        checked = [_safe_regular_file(Path(path)) for path in paths]
        with self._lock:
            self._cleanup()
            if len(self._items) + len(checked) > self.max_items:
                raise ValueError("too many active selection grants")
            result = []
            for path in checked:
                token = secrets.token_urlsafe(24)
                while token in self._items:
                    token = secrets.token_urlsafe(24)
                self._items[token] = (path, self.clock() + self.ttl)
                result.append({
                    "selection_token": token,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "kind": path.suffix.casefold().lstrip("."),
                })
            return result

    def resolve(self, token: object) -> Path:
        if not isinstance(token, str) or not token:
            raise SelectionExpiredError("selection grant expired")
        with self._lock:
            self._cleanup()
            item = self._items.get(token)
            if item is None:
                raise SelectionExpiredError("selection grant expired")
            return _safe_regular_file(item[0])

    def consume(self, token: object) -> None:
        if isinstance(token, str):
            with self._lock:
                self._items.pop(token, None)
