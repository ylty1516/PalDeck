"""
桌面/独立 EXE 入口：启动幻兽帕鲁 Mod 管理面板。
PyInstaller 打包后双击即可运行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _resource_root() -> Path:
    """Project root at runtime (dev or frozen)."""
    if getattr(sys, "frozen", False):
        # onefile: _MEIPASS has bundled data; onedir: next to exe
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _writable_data_dir() -> Path:
    """Use an explicit data directory when supplied, otherwise stay portable."""
    configured = os.environ.get("PALMOD_DATA_DIR")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent / "data"


def main() -> None:
    root = _resource_root()
    # Ensure imports resolve
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Point modules at writable data next to exe
    os.environ.setdefault("PALMOD_DATA_DIR", str(_writable_data_dir()))
    os.environ.setdefault("PALMOD_ROOT", str(root))

    from backend.app import main as app_main

    app_main(root=root, data_dir=_writable_data_dir())


if __name__ == "__main__":
    main()
