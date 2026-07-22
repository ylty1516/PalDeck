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


class ProcessCheckError(RuntimeError):
    """The operating system process check could not be completed safely."""


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
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessCheckError("无法确认 Palworld 是否正在运行") from exc
    if completed.returncode:
        raise ProcessCheckError(f"tasklist 失败，退出码 {completed.returncode}")
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
    except ProcessCheckError:
        raise
    except (OSError, TypeError) as exc:
        raise ProcessCheckError("无法确认 Palworld 是否正在运行") from exc


def is_directory_writable(path: str | os.PathLike[str]) -> bool:
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


# Backward-compatible name used by the first service revision.
check_directory_writable = is_directory_writable


def restart_as_admin(argv: Iterable[str | os.PathLike[str]]) -> None:
    """Restart an argv vector through Windows UAC, or raise a precise error."""
    arguments = [os.fspath(value) for value in argv]
    if not arguments:
        raise ValueError("argv must include an executable")
    if os.name != "nt":
        raise RuntimeError("administrator restart is only supported on Windows")
    parameters = subprocess.list2cmdline(arguments[1:]) or None
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", arguments[0], parameters, None, 1
    )
    code = int(result)
    if code <= 32:
        raise OSError(code, f"ShellExecuteW runas failed with error code {code}")


def run_as_admin(executable: str, parameters: str = "", directory: str | None = None) -> bool:
    """Legacy boolean elevation helper retained for existing callers."""
    if os.name != "nt":
        return False
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, parameters or None, directory or None, 1
    )
    return int(result) > 32
