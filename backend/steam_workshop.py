"""Exact, read-only discovery of Palworld Steam Workshop metadata."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

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
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024
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


class WorkshopNotFoundError(LookupError):
    """A valid Workshop ID is absent from the authoritative scan."""

    def __init__(self, workshop_id: str):
        self.workshop_id = workshop_id
        super().__init__(f"Workshop ID not found: {workshop_id}")


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
    identity: tuple[int, int]


@dataclass(frozen=True)
class _DependencyGraph:
    mods_by_id: dict[str, WorkshopMod]
    forward: dict[str, tuple[str, ...]]
    reverse: dict[str, tuple[str, ...]]
    missing: dict[str, tuple[str, ...]]


class SteamWorkshopService:
    """Scan Workshop metadata and manage Palworld's ActiveModList."""

    def __init__(
        self,
        steam_root: str | Path | None = None,
        game_root: str | Path | None = None,
        *,
        steam_roots: list[str | Path] | None = None,
        game_running=is_palworld_running,
        lock_root: str | Path | None = None,
    ) -> None:
        self.game_root = Path(game_root) if game_root is not None else None
        self.lock_root = Path(lock_root) if lock_root is not None else self.game_root
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
        self._scan_lock = threading.RLock()
        self.last_scan_paths: list[str] = []

    @property
    def settings_path(self) -> Path:
        if self.game_root is None:
            raise ValueError("game_root is required for Workshop state changes")
        return self.game_root / "Palworld" / "Mods" / "PalModSettings.ini"

    def scan(self, *, force: bool = False) -> list[WorkshopMod]:
        with self._scan_lock:
            return self._scan_locked(force=force)

    def _scan_locked(self, *, force: bool) -> list[WorkshopMod]:
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

    def list_mods(self, *, force: bool = False) -> list[dict[str, object]]:
        """Return scanned records with state derived from the current settings file."""
        mods = self.scan(force=force)
        active: set[str] = set()
        if self.game_root is not None and self.settings_path.exists():
            document = _read_settings(self.settings_path)
            if _global_enabled(document.text):
                active = {
                    package.casefold()
                    for package in _active_packages(document.text)
                }
        result: list[dict[str, object]] = []
        for mod in mods:
            item = mod.to_dict()
            enabled = mod.package_name.casefold() in active if mod.package_name else False
            item.update({
                "name": mod.mod_name or f"Workshop {mod.workshop_id}",
                "enabled": enabled,
                "status": "enabled" if enabled else ("disabled" if mod.valid else "conflict"),
            })
            result.append(item)
        return result

    def active_ue4ss_mods(self) -> list[dict[str, object]]:
        return [
            item for item in self.list_mods()
            if item["enabled"] is True
            and any(str(kind).casefold() == "ue4ss" for kind in item["install_types"])
        ]

    def set_enabled(
        self,
        workshop_id: str,
        enabled: bool,
        *,
        confirm_dependents: bool = False,
        conflict_validator: Callable[[WorkshopMod], None] | None = None,
    ) -> dict[str, object]:
        """Atomically update ActiveModList using only the latest trusted scan."""
        if type(enabled) is not bool or type(confirm_dependents) is not bool:
            raise ValueError("enabled and confirm_dependents must be boolean")
        if not isinstance(workshop_id, str) or _WORKSHOP_ID.fullmatch(workshop_id) is None:
            raise ValueError("workshop_id must be a positive decimal ID")
        if self.game_root is None:
            raise ValueError("game_root is required for Workshop state changes")

        if self.lock_root is None:
            raise ValueError("lock_root is required for Workshop state changes")
        with game_write_lock(self.lock_root):
            if self.game_running():
                raise GameRunningError("Palworld 正在运行，无法修改 Workshop 模组")
            mods = tuple(self.scan(force=True))
            target = self._trusted_mod(workshop_id, mods)
            graph = _build_dependency_graph(mods)
            additions = _dependency_order(target, graph) if enabled else []
            if conflict_validator is not None:
                for addition in additions:
                    conflict_validator(addition)
            settings_existed = self.settings_path.exists()
            if settings_existed:
                document = _read_settings(self.settings_path)
            else:
                if _has_reparse_ancestor(self.settings_path) or not _is_real_directory(self.settings_path.parent):
                    raise ValueError("PalModSettings.ini parent is unsafe or missing")
                document = _SettingsDocument(
                    self.settings_path, "utf-8", b"", "\n", "", (0, 0),
                )
            original = document.bom + document.text.encode(document.encoding)
            active = _active_packages(document.text) if _global_enabled(document.text) else []
            affected: list[str] = []

            if not settings_existed and not enabled:
                result = target.to_dict()
                result.update({"enabled": False, "needs_restart": True})
                return result

            if enabled:
                updated = _updated_settings(
                    document,
                    enable_packages=[mod.package_name for mod in additions],
                    enable_global=True,
                )
            else:
                affected = _enabled_dependents(target, graph, active)
                if affected and not confirm_dependents:
                    raise WorkshopDependencyError(
                        {
                            "reason": "enabled_dependents",
                            "dependents": affected,
                        }
                    )
                updated = _updated_settings(
                    document,
                    remove_package=target.package_name,
                )

            backup: tuple[Path, tuple[int, int]] | None = None
            created_identity = None
            if updated != original:
                if settings_existed:
                    backup = _atomic_write(
                        document.path,
                        updated,
                        original,
                        document.identity,
                    )
                else:
                    created_identity = _atomic_create(document.path, updated)
            try:
                confirmed = _read_settings(document.path)
                confirmed_raw = confirmed.bom + confirmed.text.encode(confirmed.encoding)
                if confirmed_raw != updated:
                    raise RuntimeError("PalModSettings.ini 写入确认失败")
                confirmed_active = _active_packages(confirmed.text)
                authoritative_enabled = any(
                    package.casefold() == target.package_name.casefold()
                    for package in confirmed_active
                )
                if authoritative_enabled is not enabled:
                    raise RuntimeError("PalModSettings.ini 写入确认失败")
                if enabled and not _global_enabled(confirmed.text):
                    raise RuntimeError("PalModSettings.ini 全局开关写入确认失败")
            except BaseException:
                if backup is not None:
                    _restore_backup(document.path, backup[0], backup[1])
                elif created_identity is not None:
                    _unlink_if_identity(document.path, created_identity)
                raise
            else:
                if backup is not None:
                    _unlink_if_identity(backup[0], backup[1])

            result = target.to_dict()
            result.update({"enabled": authoritative_enabled, "needs_restart": True})
            if affected and confirm_dependents:
                result["affected_dependents"] = affected
            return result

    @staticmethod
    def _trusted_mod(
        workshop_id: str, mods: tuple[WorkshopMod, ...]
    ) -> WorkshopMod:
        matches = [mod for mod in mods if mod.workshop_id == workshop_id]
        if len(matches) != 1:
            raise WorkshopNotFoundError(workshop_id)
        if not matches[0].valid:
            raise ValueError("invalid Workshop records cannot be toggled")
        return matches[0]

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
    raw, opened = read_safe_file(
        path,
        max_bytes=_MAX_SETTINGS_BYTES,
        reparse_checker=_is_reparse,
    )
    if raw.startswith(b"\xef\xbb\xbf"):
        bom, encoding, payload = b"\xef\xbb\xbf", "utf-8", raw[3:]
    elif raw.startswith(b"\xff\xfe"):
        bom, encoding, payload = b"\xff\xfe", "utf-16-le", raw[2:]
    else:
        bom, encoding, payload = b"", "utf-8", raw
    text = payload.decode(encoding)
    newline = "\r\n" if "\r\n" in text else "\n"
    return _SettingsDocument(
        path,
        encoding,
        bom,
        newline,
        text,
        (opened.st_dev, opened.st_ino),
    )


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
    *,
    enable_packages: list[str] | None = None,
    remove_package: str | None = None,
    enable_global: bool = False,
) -> bytes:
    lines = document.text.splitlines(keepends=True)
    section = _settings_range(lines)
    if section is None:
        if not enable_packages:
            return document.bom + document.text.encode(document.encoding)
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += document.newline
        lines.append(f"[PalModSettings]{document.newline}")
        section = (len(lines) - 1, len(lines))

    start, end = section
    before, body, after = lines[: start + 1], lines[start + 1 : end], lines[end:]
    if remove_package is not None:
        remove_key = remove_package.casefold()
        body = [
            line
            for line in body
            if not (
                (parsed := _key_value(line)) is not None
                and parsed[0] == "activemodlist"
                and parsed[1].casefold() == remove_key
            )
        ]
    elif enable_packages is not None:
        ordered_packages = list(
            dict.fromkeys(package.casefold() for package in enable_packages)
        )
        package_names = {
            package.casefold(): package for package in enable_packages
        }
        closure_keys = set(ordered_packages)
        rewritten: list[str] = []
        closure_insertion: int | None = None
        has_global = False
        for line in body:
            parsed = _key_value(line)
            if parsed is not None and parsed[0] == "bglobalenablemod":
                has_global = True
                rewritten.append(
                    f"bGlobalEnableMod=True{document.newline}" if enable_global else line
                )
                continue
            if (
                parsed is not None
                and parsed[0] == "activemodlist"
                and parsed[1].casefold() in closure_keys
            ):
                if closure_insertion is None:
                    closure_insertion = len(rewritten)
                continue
            rewritten.append(line)
        if enable_global and not has_global:
            global_insertion = next(
                (
                    index
                    for index, line in enumerate(rewritten)
                    if (parsed := _key_value(line)) is not None
                    and parsed[0] == "activemodlist"
                ),
                0,
            )
            rewritten.insert(
                global_insertion, f"bGlobalEnableMod=True{document.newline}"
            )
            if closure_insertion is not None and global_insertion <= closure_insertion:
                closure_insertion += 1
        if closure_insertion is None:
            active_indices = [
                index
                for index, line in enumerate(rewritten)
                if (parsed := _key_value(line)) is not None
                and parsed[0] == "activemodlist"
            ]
            closure_insertion = (
                active_indices[-1] + 1 if active_indices else len(rewritten)
            )
        rewritten[closure_insertion:closure_insertion] = [
            f"ActiveModList={package_names[key]}{document.newline}"
            for key in ordered_packages
        ]
        body = rewritten

    return document.bom + "".join(before + body + after).encode(document.encoding)


def _build_dependency_graph(mods: tuple[WorkshopMod, ...]) -> _DependencyGraph:
    by_id = {mod.workshop_id: mod for mod in mods if mod.valid}
    by_package: dict[str, WorkshopMod] = {}
    ambiguous: set[str] = set()
    for mod in by_id.values():
        key = mod.package_name.casefold()
        if key in by_package:
            ambiguous.add(key)
        else:
            by_package[key] = mod
    for key in ambiguous:
        by_package.pop(key, None)

    forward: dict[str, tuple[str, ...]] = {}
    reverse_lists: dict[str, list[str]] = {workshop_id: [] for workshop_id in by_id}
    missing: dict[str, tuple[str, ...]] = {}
    for mod in by_id.values():
        resolved_ids: list[str] = []
        unresolved: list[str] = []
        for dependency in mod.dependencies:
            resolved = (
                by_id.get(dependency)
                if _WORKSHOP_ID.fullmatch(dependency)
                else by_package.get(dependency.casefold())
            )
            if resolved is None:
                unresolved.append(dependency)
            else:
                resolved_ids.append(resolved.workshop_id)
                reverse_lists[resolved.workshop_id].append(mod.workshop_id)
        forward[mod.workshop_id] = tuple(dict.fromkeys(resolved_ids))
        if unresolved:
            missing[mod.workshop_id] = tuple(unresolved)
    return _DependencyGraph(
        by_id,
        forward,
        {key: tuple(values) for key, values in reverse_lists.items()},
        missing,
    )


def _dependency_order(
    target: WorkshopMod, graph: _DependencyGraph
) -> list[WorkshopMod]:
    colors: dict[str, int] = {}
    positions: dict[str, int] = {target.workshop_id: 0}
    stack: list[tuple[str, int]] = [(target.workshop_id, 0)]
    ordered: list[WorkshopMod] = []
    colors[target.workshop_id] = 1
    while stack:
        workshop_id, dependency_index = stack[-1]
        unresolved = graph.missing.get(workshop_id)
        if unresolved:
            raise WorkshopDependencyError(
                {
                    "reason": "missing_dependencies",
                    "workshop_id": workshop_id,
                    "missing": list(unresolved),
                }
            )
        dependencies = graph.forward[workshop_id]
        if dependency_index >= len(dependencies):
            stack.pop()
            positions.pop(workshop_id, None)
            colors[workshop_id] = 2
            ordered.append(graph.mods_by_id[workshop_id])
            continue
        dependency_id = dependencies[dependency_index]
        stack[-1] = (workshop_id, dependency_index + 1)
        color = colors.get(dependency_id, 0)
        if color == 2:
            continue
        if color == 1:
            first = positions[dependency_id]
            cycle = [item[0] for item in stack[first:]] + [dependency_id]
            raise WorkshopDependencyError(
                {"reason": "dependency_cycle", "cycle": cycle}
            )
        colors[dependency_id] = 1
        positions[dependency_id] = len(stack)
        stack.append((dependency_id, 0))
    return ordered


def _enabled_dependents(
    target: WorkshopMod,
    graph: _DependencyGraph,
    active: list[str],
) -> list[str]:
    active_keys = {package.casefold() for package in active}
    reached: set[str] = set()
    pending = list(graph.reverse.get(target.workshop_id, ()))
    while pending:
        workshop_id = pending.pop()
        if workshop_id in reached:
            continue
        reached.add(workshop_id)
        pending.extend(graph.reverse.get(workshop_id, ()))
    return sorted(
        (
            workshop_id
            for workshop_id in reached
            if graph.mods_by_id[workshop_id].package_name.casefold() in active_keys
        ),
        key=int,
    )


def _safe_file_identity(
    path: Path, expected: tuple[int, int] | None = None
) -> tuple[int, int]:
    if any(_is_reparse(candidate) for candidate in (path, *path.parents)):
        raise UnsafeSteamFileError("unsafe settings path")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UnsafeSteamFileError("unavailable settings path") from error
    identity = (metadata.st_dev, metadata.st_ino)
    if not stat.S_ISREG(metadata.st_mode) or (expected is not None and identity != expected):
        raise UnsafeSteamFileError("settings file changed")
    return identity


def _unlink_if_identity(path: Path, expected: tuple[int, int] | None) -> bool:
    """Best-effort cleanup that never removes a replaced or reparse file."""
    if expected is None:
        return False
    try:
        if any(_is_reparse(candidate) for candidate in (path, *path.parents)):
            return False
        metadata = path.lstat()
        identity = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISREG(metadata.st_mode) or identity != expected:
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _write_exclusive(path: Path, content: bytes) -> tuple[int, int]:
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        if any(_is_reparse(candidate) for candidate in path.parents):
            raise UnsafeSteamFileError("unsafe settings directory")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        created_identity = (opened.st_dev, opened.st_ino)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("failed to write PalModSettings.ini transaction file")
            offset += written
        os.fsync(descriptor)
        identity = _safe_file_identity(path, created_identity)
        os.close(descriptor)
        descriptor = None
        return identity
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_if_identity(path, created_identity)
        raise


def _safe_replace(
    source: Path,
    destination: Path,
    *,
    source_identity: tuple[int, int],
    destination_identity: tuple[int, int],
) -> tuple[int, int]:
    _safe_file_identity(source, source_identity)
    _safe_file_identity(destination, destination_identity)
    os.replace(source, destination)
    return _safe_file_identity(destination, source_identity)


def _atomic_create(path: Path, content: bytes) -> tuple[int, int]:
    """Publish a new settings file without exposing partial content or replacing a race."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_created = False
    linked = False
    try:
        identity = _write_exclusive(temporary, content)
        temporary_created = True
        if path.exists() or _has_reparse_ancestor(path):
            raise FileExistsError("PalModSettings.ini appeared during creation")
        os.link(temporary, path)
        linked = True
        published = _safe_file_identity(path, identity)
        _unlink_if_identity(temporary, identity)
        temporary_created = False
        _safe_file_identity(path, published)
        return published
    except BaseException:
        if linked:
            _unlink_if_identity(path, identity)
        if temporary_created:
            _unlink_if_identity(temporary, identity)
        raise


def _atomic_write(
    path: Path,
    content: bytes,
    original: bytes,
    original_identity: tuple[int, int],
) -> tuple[Path, tuple[int, int]]:
    backup = path.with_name(f".{path.name}.{uuid.uuid4().hex}.backup")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    backup_created = False
    temporary_created = False
    backup_identity: tuple[int, int] | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        backup_identity = _write_exclusive(backup, original)
        backup_created = True
        temporary_identity = _write_exclusive(temporary, content)
        temporary_created = True
        _safe_replace(
            temporary,
            path,
            source_identity=temporary_identity,
            destination_identity=original_identity,
        )
        temporary_created = False
        return backup, backup_identity
    except BaseException:
        if temporary_created:
            _unlink_if_identity(temporary, temporary_identity)
        if backup_created:
            _unlink_if_identity(backup, backup_identity)
        raise


def _restore_backup(
    path: Path, backup: Path, backup_identity: tuple[int, int]
) -> None:
    _safe_replace(
        backup,
        path,
        source_identity=_safe_file_identity(backup, backup_identity),
        destination_identity=_safe_file_identity(path),
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
