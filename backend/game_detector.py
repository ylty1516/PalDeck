"""Detect Palworld installation and manage client mod directories."""

from __future__ import annotations

import re
import string
import winreg
from pathlib import Path


APP_ID = "1623730"


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


def _read_vdf(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeError):
        return ""


def _parse_library_folders(vdf_path: Path) -> list[Path]:
    libraries: list[Path] = []
    for match in re.finditer(r'"path"\s*"([^"]+)"', _read_vdf(vdf_path), re.I):
        raw = match.group(1).replace("\\\\", "\\")
        libraries.append(Path(raw))
    return libraries


def _manifest_installdir(library: Path) -> str | None:
    manifest = library / "steamapps" / f"appmanifest_{APP_ID}.acf"
    match = re.search(r'"installdir"\s*"([^"]+)"', _read_vdf(manifest), re.I)
    return match.group(1) if match else None


def _is_valid_game_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "Palworld.exe").is_file():
        return True
    shipping = path / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe"
    paks = path / "Pal" / "Content" / "Paks"
    return shipping.is_file() and paks.is_dir()


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _common_steam_candidates() -> list[Path]:
    candidates: list[Path] = []
    for drive in string.ascii_uppercase:
        for relative in (
            rf"{drive}:\Steam\steamapps\common\Palworld",
            rf"{drive}:\SteamLibrary\steamapps\common\Palworld",
            rf"{drive}:\Program Files (x86)\Steam\steamapps\common\Palworld",
            rf"{drive}:\Program Files\Steam\steamapps\common\Palworld",
        ):
            candidates.append(Path(relative))
    return candidates


def _describe_valid_installs(candidates: list[Path]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in _unique_paths(candidates):
        if _is_valid_game_root(path):
            found.append(
                {
                    "path": str(path),
                    "valid": True,
                    "has_ue4ss": has_ue4ss(path),
                    "mod_paths": describe_mod_paths(path),
                }
            )
    return found


def find_palworld_installs(
    *, steam_roots: list[Path] | None = None
) -> list[dict[str, object]]:
    """Find manifest-backed Palworld installs in Steam libraries."""
    supplied_roots = steam_roots is not None
    roots = list(steam_roots or [])
    if not supplied_roots:
        registry_root = _read_steam_path_from_registry()
        if registry_root:
            roots.append(registry_root)

    libraries: list[Path] = []
    for root in roots:
        libraries.append(Path(root))
        libraries.extend(
            _parse_library_folders(Path(root) / "steamapps" / "libraryfolders.vdf")
        )

    manifest_candidates: list[Path] = []
    for library in _unique_paths(libraries):
        installdir = _manifest_installdir(library)
        if installdir:
            manifest_candidates.append(library / "steamapps" / "common" / installdir)

    found = _describe_valid_installs(manifest_candidates)
    if found or supplied_roots:
        return found
    return _describe_valid_installs(_common_steam_candidates())


def has_ue4ss(game_root: Path | str) -> bool:
    win64 = Path(game_root) / "Pal" / "Binaries" / "Win64"
    markers = (
        win64 / "UE4SS.dll",
        win64 / "dwmapi.dll",
        win64 / "UE4SS-settings.ini",
        win64 / "ue4ss" / "UE4SS.dll",
        win64 / "ue4ss" / "dwmapi.dll",
        win64 / "ue4ss" / "UE4SS-settings.ini",
    )
    return any(marker.is_file() for marker in markers)


def resolve_ue4ss_mods_dir(game_root: Path | str) -> Path:
    """Resolve explicit nested UE4SS markers before the classic layout."""
    win64 = Path(game_root) / "Pal" / "Binaries" / "Win64"
    nested_root = win64 / "ue4ss"
    nested_mods = nested_root / "Mods"
    nested_markers = (
        nested_mods / "mods.txt",
        nested_root / "UE4SS-settings.ini",
        nested_root / "UE4SS.dll",
        nested_root / "dwmapi.dll",
    )
    if nested_mods.is_dir() or any(marker.is_file() for marker in nested_markers):
        return nested_mods

    classic_mods = win64 / "Mods"
    classic_markers = (
        classic_mods / "mods.txt",
        win64 / "UE4SS-settings.ini",
        win64 / "UE4SS.dll",
        win64 / "dwmapi.dll",
    )
    if classic_mods.is_dir() or any(marker.is_file() for marker in classic_markers):
        return classic_mods
    return classic_mods


def get_mod_directories(game_root: Path | str) -> dict[str, Path]:
    root = Path(game_root)
    win64 = root / "Pal" / "Binaries" / "Win64"
    return {
        "root": root,
        "paks": root / "Pal" / "Content" / "Paks",
        "tilde_mods": root / "Pal" / "Content" / "Paks" / "~mods",
        "logic_mods": root / "Pal" / "Content" / "Paks" / "LogicMods",
        "ue4ss_mods": resolve_ue4ss_mods_dir(root),
        "win64": win64,
        # Legacy paths remain descriptive only; ensure_mod_folders never creates them.
        "official_mods": root / "Mods",
        "workshop": root / "Mods" / "Workshop",
        "pal_mod_settings": root / "Mods" / "PalModSettings.ini",
    }


def describe_mod_paths(game_root: Path | str) -> dict[str, str]:
    return {key: str(value) for key, value in get_mod_directories(game_root).items()}


def ensure_mod_folders(game_root: Path | str) -> dict[str, object]:
    """Create only the folders used by manually installed Steam client mods."""
    root = Path(game_root)
    if not _is_valid_game_root(root):
        raise ValueError(f"无效的幻兽帕鲁游戏目录: {root}")

    dirs = get_mod_directories(root)
    created: list[str] = []
    for key in ("tilde_mods", "logic_mods", "ue4ss_mods"):
        path = dirs[key]
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
        elif not path.is_dir():
            raise ValueError(f"路径存在但不是文件夹: {path}")

    return {
        "ok": True,
        "game_path": str(root),
        "created": created,
        "settings_created": False,
        "has_ue4ss": has_ue4ss(root),
        "mod_paths": describe_mod_paths(root),
    }


def validate_game_path(
    path: str | Path, *, create: bool = False
) -> dict[str, object]:
    p = Path(path)
    valid = _is_valid_game_root(p)
    result: dict[str, object] = {
        "path": str(p),
        "valid": valid,
        "has_ue4ss": has_ue4ss(p) if valid else False,
        "mod_paths": describe_mod_paths(p) if valid else {},
    }
    if valid and create:
        result["ensure"] = ensure_mod_folders(p)
    return result
