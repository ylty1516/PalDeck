"""Exact, read-only discovery of Palworld Steam Workshop metadata."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .game_detector import (
    APP_ID,
    find_steam_libraries,
    get_keyvalues_value,
    load_steam_keyvalues,
)

_WORKSHOP_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_MAX_TEXT = {"ModName": 256, "PackageName": 128, "Author": 256, "Version": 64}
_MAX_DEPENDENCIES = 128
_MAX_RULES = 64
_MAX_TARGET = 512


@dataclass(frozen=True)
class WorkshopMod:
    workshop_id: str
    mod_name: str
    package_name: str
    author: str
    version: str
    dependencies: tuple[str, ...]
    install_types: tuple[str, ...]
    install_targets: tuple[str, ...]
    source_dir: Path
    updated_at: int
    valid: bool
    error: str | None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.update(
            {
                "id": f"steam-workshop:{self.workshop_id}",
                "source": "steam_workshop",
                "source_dir": str(self.source_dir),
                "dependencies": list(self.dependencies),
                "install_types": list(self.install_types),
                "install_targets": list(self.install_targets),
                "can_delete": False,
                "can_toggle": self.valid,
            }
        )
        return value


class SteamWorkshopService:
    """Scan only Palworld's fixed Workshop manifest and content directory."""

    def __init__(
        self,
        steam_root: str | Path | None = None,
        game_root: str | Path | None = None,
        *,
        steam_roots: list[str | Path] | None = None,
    ) -> None:
        del game_root  # Reserved for later state management; discovery is read-only.
        if steam_roots is not None and steam_root is not None:
            raise ValueError("provide steam_root or steam_roots, not both")
        if steam_roots is not None:
            roots = [Path(root) for root in steam_roots]
        elif steam_root is not None:
            roots = [Path(steam_root)]
        else:
            roots = None
        self._libraries = find_steam_libraries(steam_roots=roots)
        self._cache_fingerprint: tuple[object, ...] | None = None
        self._cache: list[WorkshopMod] | None = None
        self.last_scan_paths: list[str] = []

    def scan(self, *, force: bool = False) -> list[WorkshopMod]:
        self.last_scan_paths = []
        candidates: list[tuple[str, Path, Path, Path]] = []
        fingerprint: list[object] = []

        for library in self._libraries:
            workshop = library / "steamapps" / "workshop"
            manifest = workshop / f"appworkshop_{APP_ID}.acf"
            content = workshop / "content" / APP_ID
            self._record(manifest)
            manifest_fingerprint = _path_fingerprint(manifest)
            fingerprint.append((str(manifest), manifest_fingerprint))

            if manifest_fingerprint is None:
                self._record(content)
                ids = self._fallback_ids(content)
                fingerprint.append((str(content), _path_fingerprint(content)))
            else:
                ids = self._manifest_ids(manifest)

            for workshop_id in ids:
                item_dir = content / workshop_id
                info_path = item_dir / "Info.json"
                self._record(info_path)
                fingerprint.append((str(info_path), _path_fingerprint(info_path)))
                candidates.append((workshop_id, content, item_dir, info_path))

        key = tuple(fingerprint)
        if not force and self._cache is not None and key == self._cache_fingerprint:
            return self._cache

        mods = [
            self._read_item(workshop_id, content, item_dir, info)
            for workshop_id, content, item_dir, info in candidates
        ]
        mods.sort(key=lambda item: (int(item.workshop_id), str(item.source_dir).casefold()))
        self._cache_fingerprint = key
        self._cache = mods
        return mods

    def _record(self, path: Path) -> None:
        self.last_scan_paths.append(str(path))

    @staticmethod
    def _manifest_ids(manifest: Path) -> list[str]:
        root = get_keyvalues_value(load_steam_keyvalues(manifest), "AppWorkshop")
        if not isinstance(root, dict) or get_keyvalues_value(root, "appid") != APP_ID:
            return []
        installed = get_keyvalues_value(root, "WorkshopItemsInstalled")
        if not isinstance(installed, dict):
            return []
        return sorted(
            (
                key
                for key, value in installed.items()
                if _WORKSHOP_ID.fullmatch(key) and isinstance(value, dict)
            ),
            key=int,
        )

    @staticmethod
    def _fallback_ids(content: Path) -> list[str]:
        if _is_reparse(content) or not _is_real_directory(content):
            return []
        try:
            children = list(content.iterdir())
        except OSError:
            return []
        return sorted(
            (
                child.name
                for child in children
                if _WORKSHOP_ID.fullmatch(child.name)
                and _is_real_directory(child)
                and not _is_reparse(child)
            ),
            key=int,
        )

    @staticmethod
    def _read_item(
        workshop_id: str, content_root: Path, item_dir: Path, info_path: Path
    ) -> WorkshopMod:
        def invalid(message: str) -> WorkshopMod:
            return WorkshopMod(
                workshop_id,
                "",
                "",
                "",
                "",
                (),
                (),
                (),
                item_dir,
                0,
                False,
                message,
            )
        if _is_reparse(content_root) or _is_reparse(item_dir):
            return invalid("Workshop content or item is a symbolic link or reparse point")
        if not _is_real_directory(item_dir):
            return invalid("Workshop item directory is missing or invalid")
        if not _is_within(info_path, item_dir):
            return invalid("Info.json path escapes Workshop item directory")
        if _is_reparse(info_path) or not _is_regular_file(info_path):
            return invalid("Info.json is not a regular file")

        try:
            raw = info_path.read_text(encoding="utf-8")
            value = json.loads(raw)
            parsed = _validate_info(value)
            updated_at = info_path.stat().st_mtime_ns
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return invalid(f"Invalid Info.json: {error}")

        return WorkshopMod(
            workshop_id=workshop_id,
            mod_name=parsed["ModName"],
            package_name=parsed["PackageName"],
            author=parsed["Author"],
            version=parsed["Version"],
            dependencies=parsed["Dependencies"],
            install_types=parsed["InstallTypes"],
            install_targets=parsed["InstallTargets"],
            source_dir=item_dir,
            updated_at=updated_at,
            valid=True,
            error=None,
        )


def _validate_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("root must be an object")

    result: dict[str, Any] = {}
    for field, limit in _MAX_TEXT.items():
        item = value.get(field)
        if not isinstance(item, str) or not item or len(item) > limit:
            raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
        result[field] = item

    package_name = result["PackageName"]
    if _SAFE_NAME.fullmatch(package_name) is None:
        raise ValueError("PackageName contains unsafe characters")

    dependencies = value.get("Dependencies")
    if not isinstance(dependencies, list) or len(dependencies) > _MAX_DEPENDENCIES:
        raise ValueError("Dependencies must be a bounded array")
    checked_dependencies: list[str] = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, str)
            or not dependency
            or len(dependency) > 128
            or _SAFE_NAME.fullmatch(dependency) is None
        ):
            raise ValueError("Dependencies contains an invalid identifier")
        checked_dependencies.append(dependency)

    rules = value.get("InstallRule")
    if not isinstance(rules, list) or not rules or len(rules) > _MAX_RULES:
        raise ValueError("InstallRule must be a non-empty bounded array")
    types: list[str] = []
    targets: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("InstallRule entries must be objects")
        rule_type = rule.get("Type")
        target = rule.get("Target")
        if (
            not isinstance(rule_type, str)
            or not rule_type
            or len(rule_type) > 64
            or _SAFE_NAME.fullmatch(rule_type) is None
        ):
            raise ValueError("InstallRule Type is invalid")
        if (
            not isinstance(target, str)
            or not target
            or len(target) > _MAX_TARGET
            or not _safe_target(target)
        ):
            raise ValueError("InstallRule Target is invalid")
        types.append(rule_type)
        targets.append(target)

    result["Dependencies"] = tuple(checked_dependencies)
    result["InstallTypes"] = tuple(types)
    result["InstallTargets"] = tuple(targets)
    return result


def _safe_target(value: str) -> bool:
    path = PureWindowsPath(value.replace("/", "\\"))
    return (
        not path.is_absolute()
        and not path.drive
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _path_fingerprint(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, getattr(info, "st_file_attributes", 0))


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False
