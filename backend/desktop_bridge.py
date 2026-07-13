"""Minimal pywebview desktop bridge with no arbitrary command surface."""

from __future__ import annotations

import threading
from typing import Any


class DesktopBridge:
    """Expose only audited window-state operations to the trusted frontend."""

    def __init__(self, *, custom_chrome: bool = True):
        self._window: Any | None = None
        self._state = "normal"
        self._custom_chrome = bool(custom_chrome)
        self._lock = threading.RLock()

    def bind(self, window: Any) -> None:
        if window is None:
            raise ValueError("window is required")
        with self._lock:
            if self._window is not None and self._window is not window:
                raise RuntimeError("window is already bound")
            self._window = window

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

    def close(self) -> dict[str, str]:
        with self._lock:
            self._require_window().destroy()
            self._state = "closed"
            return {"state": self._state}
