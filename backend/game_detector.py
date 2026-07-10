"""Detect Palworld installation and ensure mod directories exist."""

from __future__ import annotations

import os
import re
import winreg
from pathlib import Path
from typing import Any


APP_ID = "1623730"
PALWORLD_EXE_NAMES = ("Palworld.exe", "Palworld-Win64-Shipping.exe")


def _read_steam_path_from_registry() -> Path | None:
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for root, sub, name in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, name)
                path = Path(str(value).replace("/", "\\"))
                if path.exists():
                    return path
        except OSError:
            continue
    return None


def _parse_library_folders(vdf_path: Path) -> list[Path]:
    libraries: list[Path] = []
    if not vdf_path.is_file():
        return libraries
    text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    # Matches path"..." or "path" "..."
    for match in re.finditer(r'"path"\s*"([^"]+)"', text):
        raw = match.group(1).replace("\\\\", "\\")
        path = Path(raw)
        if path.exists():
            libraries.append(path)
    return libraries


def _is_valid_game_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "Palworld.exe").is_file():
        return True
    if (path / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").is_file():
        return True
    if (path / "Pal" / "Content" / "Paks").is_dir():
        return True
    return False


def find_palworld_installs() -> list[dict[str, Any]]:
    """Search Steam libraries and common paths for Palworld."""
    candidates: list[Path] = []
    steam = _read_steam_path_from_registry()
    libraries: list[Path] = []
    if steam:
        libraries.append(steam)
        libraries.extend(_parse_library_folders(steam / "steamapps" / "libraryfolders.vdf"))

    # Deduplicate libraries
    seen_lib: set[str] = set()
    unique_libs: list[Path] = []
    for lib in libraries:
        key = str(lib).lower()
        if key not in seen_lib:
            seen_lib.add(key)
            unique_libs.append(lib)

    for lib in unique_libs:
        candidates.append(lib / "steamapps" / "common" / "Palworld")
        # Steam workshop content path hint only; game root is common/Palworld

    # Common fallbacks
    for drive in "CDEFGHIJ":
        for rel in (
            rf"{drive}:\Steam\steamapps\common\Palworld",
            rf"{drive}:\SteamLibrary\steamapps\common\Palworld",
            rf"{drive}:\Program Files (x86)\Steam\steamapps\common\Palworld",
            rf"{drive}:\Program Files\Steam\steamapps\common\Palworld",
            rf"{drive}:\Games\Palworld",
            rf"{drive}:\XboxGames\Palworld\Content",
        ):
            candidates.append(Path(rel))

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.resolve() if path.exists() else path
        except OSError:
            resolved = path
        key = str(resolved).lower()
        if key in seen:
            continue
        if _is_valid_game_root(path):
            seen.add(key)
            found.append(
                {
                    "path": str(path),
                    "valid": True,
                    "has_ue4ss": has_ue4ss(path),
                    "mod_paths": describe_mod_paths(path),
                }
            )
    return found


def has_ue4ss(game_root: Path | str) -> bool:
    root = Path(game_root)
    win64 = root / "Pal" / "Binaries" / "Win64"
    markers = [
        win64 / "UE4SS.dll",
        win64 / "dwmapi.dll",
        win64 / "ue4ss" / "UE4SS.dll",
        win64 / "UE4SS-settings.ini",
        win64 / "ue4ss" / "UE4SS-settings.ini",
    ]
    return any(p.is_file() for p in markers)


def get_mod_directories(game_root: Path | str) -> dict[str, Path]:
    root = Path(game_root)
    win64 = root / "Pal" / "Binaries" / "Win64"
    # Prefer modern ue4ss/Mods if present, else classic Mods
    if (win64 / "ue4ss" / "Mods").is_dir() or (win64 / "ue4ss").is_dir():
        ue4ss_mods = win64 / "ue4ss" / "Mods"
    else:
        ue4ss_mods = win64 / "Mods"
    return {
        "root": root,
        "paks": root / "Pal" / "Content" / "Paks",
        "tilde_mods": root / "Pal" / "Content" / "Paks" / "~mods",
        "logic_mods": root / "Pal" / "Content" / "Paks" / "LogicMods",
        "ue4ss_mods": ue4ss_mods,
        "win64": win64,
        "official_mods": root / "Mods",
        "workshop": root / "Mods" / "Workshop",
        "pal_mod_settings": root / "Mods" / "PalModSettings.ini",
    }


def describe_mod_paths(game_root: Path | str) -> dict[str, str]:
    dirs = get_mod_directories(game_root)
    return {k: str(v) for k, v in dirs.items()}


def ensure_mod_folders(game_root: Path | str) -> dict[str, Any]:
    """Create standard mod folders and ensure official settings exist."""
    root = Path(game_root)
    if not _is_valid_game_root(root):
        raise ValueError(f"无效的幻兽帕鲁游戏目录: {root}")

    dirs = get_mod_directories(root)
    created: list[str] = []
    for key in ("tilde_mods", "logic_mods", "ue4ss_mods", "workshop", "official_mods"):
        path = dirs[key]
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
        elif not path.is_dir():
            raise ValueError(f"路径存在但不是文件夹: {path}")

    settings = dirs["pal_mod_settings"]
    settings_created = False
    if not settings.is_file():
        settings.write_text(
            "[PalModSettings]\n"
            "bGlobalEnableMod=True\n"
            "WorkshopRootDir=\n"
            "ConfigVersion=1.0\n"
            "bNeedShowErrorOnNextStart=False\n",
            encoding="utf-8",
        )
        settings_created = True
    else:
        # Ensure global enable is on so workshop/official mods can load
        text = settings.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"bGlobalEnableMod\s*=\s*False", text, re.I):
            text = re.sub(
                r"bGlobalEnableMod\s*=\s*False",
                "bGlobalEnableMod=True",
                text,
                flags=re.I,
            )
            settings.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "game_path": str(root),
        "created": created,
        "settings_created": settings_created,
        "has_ue4ss": has_ue4ss(root),
        "mod_paths": describe_mod_paths(root),
    }


def validate_game_path(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    valid = _is_valid_game_root(p)
    result: dict[str, Any] = {
        "path": str(p),
        "valid": valid,
        "has_ue4ss": has_ue4ss(p) if valid else False,
        "mod_paths": describe_mod_paths(p) if valid else {},
    }
    if valid:
        result["ensure"] = ensure_mod_folders(p)
    return result
