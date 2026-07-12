"""Exact, read-only discovery of Palworld Steam Workshop metadata."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .game_lock import game_write_lock
from .mod_service import GameRunningError
from .process_utils import is_palworld_running

from .game_detector import (
    APP_ID,
    UnsafeSteamFileError,
    find_steam_libraries,
    get_keyvalues_value,
    load_steam_keyvalues,
    read_safe_file,
)

_WORKSHOP_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_MAX_TEXT = {"ModName": 256, "PackageName": 128, "Author": 256, "Version": 64}
_MAX_DEPENDENCIES = 128
_MAX_RULES = 64
_MAX_TARGET = 512
_MAX_INFO_BYTES = 1024 * 1024
_MAX_WORKSHOP_ITEMS = 4096
_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


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


class WorkshopDependencyError(RuntimeError):
    """A Workshop dependency graph prevents the requested state change."""

    def __init__(self, details: dict[str, object]):
        self.details = details
        super().__init__("Workshop 模组依赖冲突")


@dataclass(frozen=True)
class _SettingsDocument:
    path: Path
    encoding: str
    bom: bytes
    newline: str
    text: str


class SteamWorkshopService:
    """Scan Workshop metadata and manage Palworld's ActiveModList."""

    def __init__(
        self,
        steam_root: str | Path | None = None,
        game_root: str | Path | None = None,
        *,
        steam_roots: list[str | Path] | None = None,
        game_running=is_palworld_running,
    ) -> None:
        self.game_root = Path(game_root) if game_root is not None else None
        self.game_running = game_running
        if steam_roots is not None and steam_root is not None:
            raise ValueError("provide steam_root or steam_roots, not both")
        if steam_roots is not None:
            roots = [Path(root) for root in steam_roots]
        elif steam_root is not None:
            roots = [Path(steam_root)]
        else:
            roots = None
        self._steam_roots = roots
        self._cache_fingerprint: tuple[object, ...] | None = None
        self._cache: tuple[WorkshopMod, ...] | None = None
        self.last_scan_paths: list[str] = []

    @property
    def settings_path(self) -> Path:
        if self.game_root is None:
            raise ValueError("game_root is required for Workshop state changes")
        return self.game_root / "Palworld" / "Mods" / "PalModSettings.ini"

    def scan(self, *, force: bool = False) -> list[WorkshopMod]:
        self.last_scan_paths = []
        candidates: list[tuple[str, Path, Path, Path]] = []
        fingerprint: list[object] = []
        libraries = find_steam_libraries(steam_roots=self._steam_roots)
        fingerprint.append(tuple(str(library) for library in libraries))

        for library in libraries:
            workshop = library / "steamapps" / "workshop"
            manifest = workshop / f"appworkshop_{APP_ID}.acf"
            content = workshop / "content" / APP_ID
            self._record(manifest)
            manifest_fingerprint = _path_fingerprint(manifest)
            fingerprint.append((str(manifest), manifest_fingerprint))
            self._record(content)
            fingerprint.append((str(content), _path_safety_fingerprint(content)))

            if manifest_fingerprint is None:
                ids = self._fallback_ids(content)
            else:
                ids = self._manifest_ids(manifest)

            for workshop_id in ids:
                item_dir = content / workshop_id
                info_path = item_dir / "Info.json"
                self._record(info_path)
                fingerprint.append((str(item_dir), _path_safety_fingerprint(item_dir)))
                fingerprint.append(
                    (str(info_path), _path_safety_fingerprint(info_path))
                )
                candidates.append((workshop_id, content, item_dir, info_path))

        key = tuple(fingerprint)
        if not force and self._cache is not None and key == self._cache_fingerprint:
            return list(self._cache)

        mods = [
            self._read_item(workshop_id, content, item_dir, info)
            for workshop_id, content, item_dir, info in candidates
        ]
        mods.sort(key=lambda item: (int(item.workshop_id), str(item.source_dir).casefold()))
        self._cache_fingerprint = key
        self._cache = tuple(mods)
        return list(self._cache)

    def set_enabled(
        self,
        workshop_id: str,
        enabled: bool,
        *,
        confirm_dependents: bool = False,
    ) -> dict[str, object]:
        """Atomically update ActiveModList using only the latest trusted scan."""
        if type(enabled) is not bool or type(confirm_dependents) is not bool:
            raise ValueError("enabled and confirm_dependents must be boolean")
        target, mods = self._trusted_mod(workshop_id)
        if self.game_root is None:
            raise ValueError("game_root is required for Workshop state changes")

        with game_write_lock(self.game_root):
            if self.game_running():
                raise GameRunningError("Palworld 正在运行，无法修改 Workshop 模组")
            document = _read_settings(self.settings_path)
            active = _active_packages(document.text)
            affected: list[str] = []

            if enabled:
                additions = _dependency_order(target, mods)
                desired = _deduplicate(active + [mod.package_name for mod in additions])
                global_enabled = True
            else:
                affected = _enabled_dependents(target, mods, active)
                if affected and not confirm_dependents:
                    raise WorkshopDependencyError(
                        {
                            "reason": "enabled_dependents",
                            "dependents": affected,
                        }
                    )
                desired = [
                    package
                    for package in _deduplicate(active)
                    if package.casefold() != target.package_name.casefold()
                ]
                global_enabled = None

            updated = _updated_settings(document, desired, global_enabled)
            if updated != document.bom + document.text.encode(document.encoding):
                _atomic_write(document.path, updated)

            confirmed = _read_settings(document.path)
            confirmed_active = _active_packages(confirmed.text)
            authoritative_enabled = any(
                package.casefold() == target.package_name.casefold()
                for package in confirmed_active
            )
            if [value.casefold() for value in _deduplicate(confirmed_active)] != [
                value.casefold() for value in desired
            ]:
                raise RuntimeError("PalModSettings.ini 写入确认失败")
            if authoritative_enabled is not enabled:
                raise RuntimeError("PalModSettings.ini 写入确认失败")
            if enabled and not _global_enabled(confirmed.text):
                raise RuntimeError("PalModSettings.ini 全局开关写入确认失败")

            result = target.to_dict()
            result.update({"enabled": authoritative_enabled, "needs_restart": True})
            if affected and confirm_dependents:
                result["affected_dependents"] = affected
            return result

    def _trusted_mod(
        self, workshop_id: str
    ) -> tuple[WorkshopMod, tuple[WorkshopMod, ...]]:
        if not isinstance(workshop_id, str) or _WORKSHOP_ID.fullmatch(workshop_id) is None:
            raise ValueError("workshop_id must be a positive decimal ID")
        if self._cache is None:
            raise ValueError("a trusted Workshop scan is required")
        matches = [mod for mod in self._cache if mod.workshop_id == workshop_id]
        if len(matches) != 1:
            raise ValueError("Workshop ID is not present in the latest scan")
        if not matches[0].valid:
            raise ValueError("invalid Workshop records cannot be toggled")
        return matches[0], self._cache

    def _record(self, path: Path) -> None:
        self.last_scan_paths.append(str(path))

    @staticmethod
    def _manifest_ids(manifest: Path) -> list[str]:
        root = get_keyvalues_value(
            load_steam_keyvalues(manifest, reparse_checker=_is_reparse), "AppWorkshop"
        )
        if not isinstance(root, dict) or get_keyvalues_value(root, "appid") != APP_ID:
            return []
        installed = get_keyvalues_value(root, "WorkshopItemsInstalled")
        if not isinstance(installed, dict):
            return []
        ids = [
            key
            for key, value in installed.items()
            if _WORKSHOP_ID.fullmatch(key) and isinstance(value, dict)
        ]
        if len(ids) > _MAX_WORKSHOP_ITEMS:
            return []
        return sorted(ids, key=int)

    @staticmethod
    def _fallback_ids(content: Path) -> list[str]:
        if _has_reparse_ancestor(content) or not _is_real_directory(content):
            return []
        ids: list[str] = []
        examined = 0
        try:
            for child in content.iterdir():
                examined += 1
                if examined > _MAX_WORKSHOP_ITEMS:
                    return []
                if (
                    _WORKSHOP_ID.fullmatch(child.name)
                    and _is_real_directory(child)
                    and not _has_reparse_ancestor(child)
                ):
                    ids.append(child.name)
        except OSError:
            return []
        return sorted(ids, key=int)

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
        if (
            _has_reparse_ancestor(content_root)
            or _has_reparse_ancestor(item_dir)
            or _has_reparse_ancestor(info_path)
            or not _is_real_directory(item_dir)
            or not _is_within(info_path, item_dir)
        ):
            return invalid("unsafe_path: Workshop path is unsafe")

        try:
            raw, opened = read_safe_file(
                info_path,
                max_bytes=_MAX_INFO_BYTES,
                reparse_checker=_is_reparse,
            )
            value = json.loads(raw.decode("utf-8"))
            parsed = _validate_info(value)
            updated_at = opened.st_mtime_ns
        except (
            UnsafeSteamFileError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
            MemoryError,
        ):
            return invalid("invalid_info: Workshop metadata is invalid")

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


def _read_settings(path: Path) -> _SettingsDocument:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        bom, encoding, payload = b"\xef\xbb\xbf", "utf-8", raw[3:]
    elif raw.startswith(b"\xff\xfe"):
        bom, encoding, payload = b"\xff\xfe", "utf-16-le", raw[2:]
    else:
        bom, encoding, payload = b"", "utf-8", raw
    text = payload.decode(encoding)
    newline = "\r\n" if "\r\n" in text else "\n"
    return _SettingsDocument(path, encoding, bom, newline, text)


def _settings_range(lines: list[str]) -> tuple[int, int] | None:
    for index, line in enumerate(lines):
        if line.strip().casefold() != "[palmodsettings]":
            continue
        for end in range(index + 1, len(lines)):
            stripped = lines[end].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                return index, end
        return index, len(lines)
    return None


def _key_value(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith((";", "#")) or "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key.strip().casefold(), value.strip()


def _active_packages(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    section = _settings_range(lines)
    if section is None:
        return []
    _, end = section
    return [
        parsed[1]
        for line in lines[section[0] + 1 : end]
        if (parsed := _key_value(line)) is not None
        and parsed[0] == "activemodlist"
        and parsed[1]
    ]


def _global_enabled(text: str) -> bool:
    lines = text.splitlines(keepends=True)
    section = _settings_range(lines)
    if section is None:
        return False
    _, end = section
    for line in lines[section[0] + 1 : end]:
        parsed = _key_value(line)
        if parsed is not None and parsed[0] == "bglobalenablemod":
            return parsed[1].casefold() == "true"
    return False


def _updated_settings(
    document: _SettingsDocument,
    active: list[str],
    global_enabled: bool | None,
) -> bytes:
    lines = document.text.splitlines(keepends=True)
    section = _settings_range(lines)
    if section is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += document.newline
        start = len(lines)
        lines.extend([f"[PalModSettings]{document.newline}"])
        end = len(lines)
    else:
        start, end = section

    target_indices: list[int] = []
    preserved_global: str | None = None
    for index in range(start + 1, end):
        parsed = _key_value(lines[index])
        if parsed is None:
            continue
        if parsed[0] == "bglobalenablemod":
            target_indices.append(index)
            if preserved_global is None:
                preserved_global = lines[index]
        elif parsed[0] == "activemodlist":
            target_indices.append(index)

    insertion = min(target_indices, default=end)
    target_set = set(target_indices)
    if global_enabled is True:
        replacement = [f"bGlobalEnableMod=True{document.newline}"]
    elif preserved_global is not None:
        replacement = [preserved_global]
    else:
        replacement = []
    replacement.extend(
        f"ActiveModList={package}{document.newline}" for package in _deduplicate(active)
    )
    rewritten = [line for index, line in enumerate(lines) if index not in target_set]
    removed_before = sum(1 for index in target_indices if index < insertion)
    rewritten[insertion - removed_before : insertion - removed_before] = replacement
    return document.bom + "".join(rewritten).encode(document.encoding)


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _mod_indexes(
    mods: tuple[WorkshopMod, ...],
) -> tuple[dict[str, WorkshopMod], dict[str, WorkshopMod]]:
    by_id = {mod.workshop_id: mod for mod in mods if mod.valid}
    by_package: dict[str, WorkshopMod] = {}
    ambiguous: set[str] = set()
    for mod in mods:
        if not mod.valid:
            continue
        key = mod.package_name.casefold()
        if key in by_package:
            ambiguous.add(key)
        else:
            by_package[key] = mod
    for key in ambiguous:
        by_package.pop(key, None)
    return by_id, by_package


def _resolve_dependency(
    dependency: str,
    by_id: dict[str, WorkshopMod],
    by_package: dict[str, WorkshopMod],
) -> WorkshopMod | None:
    if _WORKSHOP_ID.fullmatch(dependency):
        return by_id.get(dependency)
    return by_package.get(dependency.casefold())


def _dependency_order(
    target: WorkshopMod, mods: tuple[WorkshopMod, ...]
) -> list[WorkshopMod]:
    by_id, by_package = _mod_indexes(mods)
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[WorkshopMod] = []

    def visit(mod: WorkshopMod) -> None:
        if mod.workshop_id in visiting:
            first = visiting.index(mod.workshop_id)
            raise WorkshopDependencyError(
                {
                    "reason": "dependency_cycle",
                    "cycle": visiting[first:] + [mod.workshop_id],
                }
            )
        if mod.workshop_id in visited:
            return
        visiting.append(mod.workshop_id)
        missing: list[str] = []
        dependencies: list[WorkshopMod] = []
        for dependency in mod.dependencies:
            resolved = _resolve_dependency(dependency, by_id, by_package)
            if resolved is None:
                missing.append(dependency)
            else:
                dependencies.append(resolved)
        if missing:
            raise WorkshopDependencyError(
                {
                    "reason": "missing_dependencies",
                    "workshop_id": mod.workshop_id,
                    "missing": missing,
                }
            )
        for dependency in dependencies:
            visit(dependency)
        visiting.pop()
        visited.add(mod.workshop_id)
        ordered.append(mod)

    visit(target)
    return ordered


def _enabled_dependents(
    target: WorkshopMod,
    mods: tuple[WorkshopMod, ...],
    active: list[str],
) -> list[str]:
    active_keys = {package.casefold() for package in active}
    by_id, by_package = _mod_indexes(mods)

    def depends_on(mod: WorkshopMod, examined: set[str]) -> bool:
        if mod.workshop_id in examined:
            return False
        examined.add(mod.workshop_id)
        for dependency in mod.dependencies:
            resolved = _resolve_dependency(dependency, by_id, by_package)
            if resolved is None:
                continue
            if resolved.workshop_id == target.workshop_id or depends_on(resolved, examined):
                return True
        return False

    dependents = [
        mod.workshop_id
        for mod in mods
        if mod.valid
        and mod.workshop_id != target.workshop_id
        and mod.package_name.casefold() in active_keys
        and depends_on(mod, set())
    ]
    return sorted(dependents, key=int)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("failed to write PalModSettings.ini temporary file")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


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
    normalized = value.replace("/", "\\")
    raw_parts = normalized.split("\\")
    if (
        ":" in normalized
        or any(part in ("", ".", "..") for part in raw_parts)
        or any(part.endswith((".", " ")) for part in raw_parts)
    ):
        return False
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive:
        return False
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            part in ("", ".", "..")
            or part.endswith((".", " "))
            or stem in _WINDOWS_DEVICES
        ):
            return False
    return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _has_reparse_ancestor(path: Path) -> bool:
    return any(_is_reparse(candidate) for candidate in (path, *path.parents))


def _path_safety_fingerprint(path: Path) -> tuple[object, ...]:
    """Revalidate the complete ancestor chain before allowing a cache hit."""
    chain = tuple(
        (str(candidate), _path_fingerprint(candidate), _is_reparse(candidate))
        for candidate in (path, *path.parents)
    )
    return (chain, _is_real_directory(path))


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
