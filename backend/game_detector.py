"""Detect Palworld installation and manage client mod directories."""

from __future__ import annotations

import os
import stat
import string
import winreg
from pathlib import Path, PureWindowsPath
from typing import Callable


APP_ID = "1623730"
MAX_KEYVALUES_BYTES = 4 * 1024 * 1024
MAX_KEYVALUES_TOKENS = 200_000
MAX_KEYVALUES_DEPTH = 64
UE4SS_FRAMEWORK_MODS = frozenset({
    "bpmodloadermod",
    "cheatmanagermod",
    "consolecommandsmod",
    "keybinds",
    "uobjecthook",
})


def is_ue4ss_framework_mod(name: str) -> bool:
    """Return whether a Mods child is shipped as UE4SS framework infrastructure."""
    return name.casefold() in UE4SS_FRAMEWORK_MODS


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


class UnsafeSteamFileError(ValueError):
    """A Steam metadata file could not be read within the safety boundary."""


def _path_is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def read_safe_file(
    path: Path,
    *,
    max_bytes: int,
    reparse_checker: Callable[[Path], bool] | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read a bounded regular file through a verified, non-following descriptor."""
    path = Path(path)
    checker = reparse_checker or _path_is_reparse
    if any(checker(candidate) for candidate in (path, *path.parents)):
        raise UnsafeSteamFileError("unsafe file")
    try:
        before = path.lstat()
    except OSError as error:
        raise UnsafeSteamFileError("unavailable file") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise UnsafeSteamFileError("invalid file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            any(checker(candidate) for candidate in (path, *path.parents))
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > max_bytes
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise UnsafeSteamFileError("file changed")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeSteamFileError("file too large")
            chunks.append(chunk)
        if any(checker(candidate) for candidate in (path, *path.parents)):
            raise UnsafeSteamFileError("file changed")
        return b"".join(chunks), opened
    except OSError as error:
        raise UnsafeSteamFileError("unavailable file") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _tokenize_keyvalues(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if char in "{}":
            tokens.append(char)
            if len(tokens) > MAX_KEYVALUES_TOKENS:
                raise ValueError("too many KeyValues tokens")
            index += 1
            continue
        if char != '"':
            raise ValueError("invalid KeyValues token")

        index += 1
        value: list[str] = []
        while index < len(text) and text[index] != '"':
            if text[index] == "\\" and index + 1 < len(text):
                escaped = text[index + 1]
                if escaped in ('"', "\\"):
                    value.append(escaped)
                    index += 2
                    continue
            value.append(text[index])
            index += 1
        if index >= len(text):
            raise ValueError("unterminated KeyValues string")
        tokens.append("".join(value))
        if len(tokens) > MAX_KEYVALUES_TOKENS:
            raise ValueError("too many KeyValues tokens")
        index += 1
    return tokens


def _parse_keyvalues(text: str) -> dict[str, object]:
    tokens = _tokenize_keyvalues(text)
    index = 0

    def parse_object(*, nested: bool, depth: int = 0) -> dict[str, object]:
        nonlocal index
        parsed: dict[str, object] = {}
        while index < len(tokens):
            key = tokens[index]
            index += 1
            if key == "}":
                if nested:
                    return parsed
                raise ValueError("unexpected KeyValues closing brace")
            if key == "{":
                raise ValueError("unexpected KeyValues opening brace")
            if index >= len(tokens):
                raise ValueError("missing KeyValues value")
            value = tokens[index]
            index += 1
            if value == "{":
                if depth >= MAX_KEYVALUES_DEPTH:
                    raise ValueError("KeyValues nesting is too deep")
                parsed[key] = parse_object(nested=True, depth=depth + 1)
            elif value == "}":
                raise ValueError("missing KeyValues value")
            else:
                parsed[key] = value
        if nested:
            raise ValueError("unterminated KeyValues object")
        return parsed

    return parse_object(nested=False)


def load_steam_keyvalues(
    path: Path, *, reparse_checker: Callable[[Path], bool] | None = None
) -> dict[str, object]:
    """Strictly parse a bounded Steam KeyValues file, failing closed."""
    try:
        raw, _ = read_safe_file(
            path, max_bytes=MAX_KEYVALUES_BYTES, reparse_checker=reparse_checker
        )
        return _parse_keyvalues(raw.decode("utf-8-sig"))
    except (UnsafeSteamFileError, UnicodeError, ValueError, RecursionError):
        return {}


def get_keyvalues_value(mapping: dict[str, object], key: str) -> object | None:
    """Look up a Steam KeyValues key using its case-insensitive semantics."""
    wanted = key.casefold()
    for candidate, value in mapping.items():
        if candidate.casefold() == wanted:
            return value
    return None


def _parse_library_folders(vdf_path: Path) -> list[Path]:
    root = get_keyvalues_value(load_steam_keyvalues(vdf_path), "libraryfolders")
    if not isinstance(root, dict):
        return []

    libraries: list[Path] = []
    for entry in root.values():
        if not isinstance(entry, dict):
            continue
        raw = get_keyvalues_value(entry, "path")
        if isinstance(raw, str) and raw:
            libraries.append(Path(raw))
    return libraries


def _is_safe_installdir(value: str) -> bool:
    windows_path = PureWindowsPath(value)
    return bool(
        value.strip()
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not windows_path.is_absolute()
        and not windows_path.drive
    )


def _manifest_installdir(library: Path) -> str | None:
    manifest = library / "steamapps" / f"appmanifest_{APP_ID}.acf"
    app_state = get_keyvalues_value(load_steam_keyvalues(manifest), "AppState")
    if not isinstance(app_state, dict):
        return None
    appid = get_keyvalues_value(app_state, "appid")
    installdir = get_keyvalues_value(app_state, "installdir")
    if appid != APP_ID or not isinstance(installdir, str):
        return None
    return installdir if _is_safe_installdir(installdir) else None


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


def find_steam_libraries(
    *, steam_roots: list[Path] | None = None
) -> list[Path]:
    """Return configured Steam libraries without probing game/content directories."""
    roots = list(steam_roots or [])
    if steam_roots is None:
        registry_root = _read_steam_path_from_registry()
        if registry_root:
            roots.append(registry_root)

    libraries: list[Path] = []
    for root in roots:
        root = Path(root)
        libraries.append(root)
        libraries.extend(
            _parse_library_folders(root / "steamapps" / "libraryfolders.vdf")
        )
    return _unique_paths(libraries)


def find_palworld_installs(
    *, steam_roots: list[Path] | None = None
) -> list[dict[str, object]]:
    """Find manifest-backed Palworld installs in Steam libraries."""
    supplied_roots = steam_roots is not None

    manifest_candidates: list[Path] = []
    for library in find_steam_libraries(steam_roots=steam_roots):
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
        win64 / "xinput1_3.dll",
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
    targets = [dirs[key] for key in ("tilde_mods", "logic_mods", "ue4ss_mods")]
    for path in targets:
        if path.exists() and not path.is_dir():
            raise ValueError(f"路径存在但不是文件夹: {path}")

    created: list[str] = []
    for path in targets:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))

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
