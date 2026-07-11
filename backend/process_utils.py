"""Small, injectable operating-system helpers used by mod transactions."""

from __future__ import annotations

import csv
import ctypes
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable

_PROCESS_NAMES = {"palworld.exe", "palworld-win64-shipping.exe"}


def _tasklist_process_names() -> list[str]:
    if os.name != "nt":
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=flags,
        )
    except OSError:
        return []
    if completed.returncode:
        return []
    return [row[0] for row in csv.reader(completed.stdout.splitlines()) if row]


def is_palworld_running(
    process_names_provider: Callable[[], Iterable[str]] | None = None,
) -> bool:
    """Return whether either Palworld executable is running.

    Non-Windows hosts safely report false unless a provider is injected.
    """
    provider = process_names_provider or _tasklist_process_names
    try:
        return any(str(name).casefold() in _PROCESS_NAMES for name in provider())
    except (OSError, TypeError):
        return False


def check_directory_writable(path: str | os.PathLike[str]) -> bool:
    """Test directory writability with a real temporary file and always clean it."""
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=".palmod-write-", dir=directory)
        os.close(descriptor)
        Path(filename).unlink()
        return True
    except OSError:
        try:
            if "filename" in locals():
                Path(filename).unlink(missing_ok=True)
        except OSError:
            pass
        return False


def run_as_admin(executable: str, parameters: str = "", directory: str | None = None) -> bool:
    """Request elevation through ShellExecuteW; unsupported platforms return false."""
    if os.name != "nt":
        return False
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, parameters or None, directory or None, 1
    )
    return int(result) > 32
