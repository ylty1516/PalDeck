"""Minimal pywebview desktop bridge with no arbitrary command surface."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from backend.import_selection import SelectionRegistry, list_supported_top_level


def _native_handle(window: Any) -> int:
    native = getattr(window, 'native', None)
    handle = getattr(native, 'Handle', None)
    if handle is None:
        return 0
    to_int64 = getattr(handle, 'ToInt64', None)
    try:
        return int(to_int64() if callable(to_int64) else handle)
    except (TypeError, ValueError, OverflowError):
        return 0


def _begin_native_window_drag(window: Any) -> bool:
    if sys.platform != 'win32':
        return False
    hwnd = _native_handle(window)
    if hwnd <= 0:
        return False
    user32 = ctypes.windll.user32
    native_hwnd = wintypes.HWND(hwnd)
    owner_pid = wintypes.DWORD()
    if not user32.IsWindow(native_hwnd):
        return False
    user32.GetWindowThreadProcessId(native_hwnd, ctypes.byref(owner_pid))
    if owner_pid.value != os.getpid():
        return False
    user32.ReleaseCapture()
    user32.SendMessageW(native_hwnd, 0x00A1, 2, 0)
    return True


def _native_window_state(window: Any) -> str | None:
    value = str(getattr(getattr(window, 'native', None), 'WindowState', '')).casefold()
    for state in ('maximized', 'minimized', 'normal'):
        if state in value:
            return state
    return None


class DesktopBridge:
    """Expose only audited window-state operations to the trusted frontend."""

    def __init__(self, *, custom_chrome: bool = True, selection_registry: SelectionRegistry | Any | None = None,
                 native_drag: Callable[[Any], bool] | None = None):
        self._window: Any | None = None
        self._state = "normal"
        self._custom_chrome = bool(custom_chrome)
        self._selection_registry = selection_registry
        self._native_drag = native_drag or _begin_native_window_drag
        self._folder_dialog_type: Any | None = None
        self._lock = threading.RLock()

    def bind(self, window: Any, *, folder_dialog_type: Any | None = None) -> None:
        if window is None:
            raise ValueError("window is required")
        with self._lock:
            if self._window is not None and self._window is not window:
                raise RuntimeError("window is already bound")
            self._window = window
            self._folder_dialog_type = folder_dialog_type

    def _require_window(self) -> Any:
        if self._state == "closed":
            raise RuntimeError("window is closed")
        if self._window is None:
            raise RuntimeError("window is not ready")
        return self._window

    def get_state(self) -> dict[str, object]:
        with self._lock:
            return {"state": self._state, "custom_chrome": self._custom_chrome}

    def minimize(self) -> dict[str, str]:
        with self._lock:
            self._require_window().minimize()
            self._state = "minimized"
            return {"state": self._state}

    def toggle_maximize(self) -> dict[str, str]:
        with self._lock:
            window = self._require_window()
            if self._state == "maximized":
                window.restore()
                self._state = "normal"
            else:
                window.maximize()
                self._state = "maximized"
            return {"state": self._state}

    def begin_drag(self) -> dict[str, bool]:
        with self._lock:
            window = self._require_window()
        started = bool(self._native_drag(window))
        native_state = _native_window_state(window)
        if native_state:
            with self._lock:
                self._state = native_state
        return {'started': started}

    def choose_mod_folder(self) -> dict[str, list[dict[str, object]]]:
        with self._lock:
            window = self._require_window()
            if self._selection_registry is None or self._folder_dialog_type is None:
                raise RuntimeError("folder picker is unavailable")
            selected = window.create_file_dialog(self._folder_dialog_type, allow_multiple=False)
            if not selected:
                return {"items": []}
            paths = list_supported_top_level(Path(selected[0]))
            return {"items": self._selection_registry.issue(paths)}

    def close(self) -> dict[str, str]:
        with self._lock:
            self._require_window().destroy()
            self._state = "closed"
            return {"state": self._state}
