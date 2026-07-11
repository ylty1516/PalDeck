"""Transactional management for PAK and LogicMods file groups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Iterable

from .archive_utils import inspect_and_extract
from .domain import AuditStatus, ManifestAudit, ManifestFile, ModKind, ModManifest
from .game_detector import ensure_mod_folders, get_mod_directories, is_ue4ss_framework_mod
from .manifest_store import ManifestStore, validate_no_reparse_ancestors
from .process_utils import check_directory_writable, is_palworld_running

_SIDECARS = (".pak", ".utoc", ".ucas")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_DEPTHS = threading.local()


def _locked_write(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._transaction_lock():
            return method(self, *args, **kwargs)
    return locked


class GameRunningError(RuntimeError):
    """A filesystem mutation was rejected while Palworld was running."""


class ModConflictError(RuntimeError):
    def __init__(self, details: dict[str, object]):
        self.details = details
        super().__init__("模组文件冲突")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _safe_label(value: str) -> str:
    cleaned = _INVALID_NAME.sub("_", value).strip().rstrip(". ")
    return (cleaned or "Mod")[:80].rstrip(". ") or "Mod"


class ModService:
    def __init__(
        self,
        game_root: str | os.PathLike[str],
        data_dir: str | os.PathLike[str],
        game_running=is_palworld_running,
    ) -> None:
        self.game_root = Path(game_root)
        self.data_dir = Path(data_dir)
        dirs = get_mod_directories(self.game_root)
        self.store = ManifestStore(
            self.data_dir / "manifests",
            known_roots=(dirs["tilde_mods"], dirs["logic_mods"], dirs["ue4ss_mods"]),
        )
        self.game_running = game_running
        self._migrate_legacy_once()

    @contextmanager
    def _transaction_lock(self, timeout: float = 10.0):
        validate_no_reparse_ancestors(self.data_dir)
        key = str(Path(os.path.abspath(self.data_dir))).casefold()
        with _LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        if not thread_lock.acquire(timeout=timeout):
            raise TimeoutError("等待 Mod 事务锁超时")
        depths = getattr(_LOCK_DEPTHS, "values", None)
        if depths is None:
            depths = {}
            _LOCK_DEPTHS.values = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
                thread_lock.release()
            return
        lock_path = self.data_dir / ".mod-service.lock"
        acquired_file = False
        deadline = time.monotonic() + timeout
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(descriptor)
                    acquired_file = True
                    depths[key] = 1
                    break
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("等待 Mod 跨进程事务锁超时")
                    time.sleep(0.02)
            yield
        finally:
            if acquired_file:
                depths.pop(key, None)
                lock_path.unlink(missing_ok=True)
            thread_lock.release()

    def _migrate_legacy_once(self) -> None:
        legacy_path = self.data_dir / "mods_registry.json"
        marker = self.data_dir / "legacy-migration-v1.done"
        if marker.is_file() or not legacy_path.is_file():
            return
        with self._transaction_lock():
            if marker.is_file():
                return
            self._assert_stopped()
            dirs = get_mod_directories(self.game_root)
            known = {
                ModKind.PAK: validate_no_reparse_ancestors(dirs["tilde_mods"]),
                ModKind.LOGICPAK: validate_no_reparse_ancestors(dirs["logic_mods"]),
            }
            try:
                raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = []
            entries = raw if isinstance(raw, list) else []
            moved: list[tuple[Path, Path]] = []
            created_ids: list[str] = []
            try:
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    source = Path(str(item.get("install_path", "")))
                    if not source.name.casefold().endswith(".pak.disabled") or not source.is_file():
                        continue
                    try:
                        manifest_id = uuid.UUID(str(item.get("id", ""))).hex
                        kind = ModKind(str(item.get("mod_type", "pak")))
                    except ValueError:
                        continue
                    if kind not in known:
                        continue
                    safe_source = validate_no_reparse_ancestors(source)
                    if safe_source.parent.resolve() != known[kind].resolve():
                        continue
                    try:
                        self.store.get(manifest_id)
                        continue
                    except KeyError:
                        pass
                    relative_name = source.name[:-len(".disabled")]
                    disabled = self.data_dir / "disabled" / manifest_id / relative_name
                    validate_no_reparse_ancestors(disabled)
                    disabled.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, disabled)
                    moved.append((disabled, source))
                    manifest = ModManifest(
                        id=manifest_id,
                        name=str(item.get("name", source.stem)),
                        kind=kind,
                        install_root=safe_source.parent.resolve(),
                        source_name=str(item.get("source_name", source.name)),
                        nexus_id=item.get("nexus_id") if type(item.get("nexus_id")) is int else None,
                        installed_at=str(item.get("installed_at") or datetime.now(timezone.utc).isoformat()),
                        enabled=False,
                        files=(ManifestFile(relative_name, disabled.stat().st_size, _sha256(disabled)),),
                    )
                    self.store.save(manifest)
                    created_ids.append(manifest_id)
                self.store.migrate_legacy_registry(legacy_path)
                marker.parent.mkdir(parents=True, exist_ok=True)
                temporary = marker.with_name(f".{marker.name}.tmp")
                temporary.write_text("1\n", encoding="ascii")
                os.replace(temporary, marker)
            except BaseException:
                marker.with_name(f".{marker.name}.tmp").unlink(missing_ok=True)
                for manifest_id in created_ids:
                    self.store.delete(manifest_id)
                for disabled, source in reversed(moved):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if disabled.exists():
                        os.replace(disabled, source)
                raise

    def _assert_stopped(self) -> None:
        if self.game_running():
            raise GameRunningError("幻兽帕鲁正在运行，无法修改模组")

    def _prepare_dirs(self) -> dict[str, Path]:
        dirs = get_mod_directories(self.game_root)
        for path in (
            self.game_root,
            dirs["tilde_mods"],
            dirs["logic_mods"],
            dirs["ue4ss_mods"],
            self.data_dir,
        ):
            validate_no_reparse_ancestors(path)
        ensure_mod_folders(self.game_root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "staging").mkdir(parents=True, exist_ok=True)
        for path in (dirs["tilde_mods"], dirs["logic_mods"], dirs["ue4ss_mods"], self.data_dir):
            if not check_directory_writable(path):
                raise PermissionError(f"目录不可写：{path}")
        return dirs

    @staticmethod
    def _kind(value: str | ModKind | None, fallback: ModKind) -> ModKind:
        if value in (None, "", "auto"):
            return fallback
        kind = ModKind(value)
        if kind not in (ModKind.PAK, ModKind.LOGICPAK, ModKind.UE4SS):
            raise ValueError("仅支持 pak、logicpak 或 ue4ss")
        return kind

    @staticmethod
    def _target(dirs: dict[str, Path], kind: ModKind) -> Path:
        if kind is ModKind.UE4SS:
            return dirs["ue4ss_mods"]
        return dirs["logic_mods"] if kind is ModKind.LOGICPAK else dirs["tilde_mods"]

    @staticmethod
    def _serialize_manifest(manifest: ModManifest, audit) -> dict[str, object]:
        value = asdict(manifest)
        value["kind"] = manifest.kind.value
        value["install_root"] = str(manifest.install_root)
        value["manifest_files"] = [asdict(item) for item in manifest.files]
        value["mod_type"] = manifest.kind.value
        value["status"] = audit.status.value
        value["enabled"] = manifest.enabled
        value["install_path"] = str(
            manifest.install_root / manifest.files[0].relative_path
            if len(manifest.files) == 1
            else manifest.install_root
        )
        value["files"] = [item.relative_path for item in manifest.files]
        value["size_bytes"] = sum(item.size for item in manifest.files)
        value["notes"] = "由 Palworld Mod Manager 管理"
        if manifest.ue4ss_enabled_txt is not None:
            value["ue4ss_enabled_txt"] = asdict(manifest.ue4ss_enabled_txt)
        value["audit"] = {
            "manifest_id": audit.manifest_id,
            "status": audit.status.value,
        }
        return value

    def _listed(self, manifest: ModManifest) -> dict[str, object]:
        audit = self._audit(manifest)
        displayed = manifest
        if manifest.kind is ModKind.UE4SS and audit.status in (AuditStatus.ENABLED, AuditStatus.DISABLED):
            displayed = replace(manifest, enabled=audit.status is AuditStatus.ENABLED)
        return self._serialize_manifest(displayed, audit)

    @staticmethod
    def _mods_entry(line: str) -> tuple[str, str] | None:
        match = re.match(r"^(?P<prefix>\s*)(?P<name>[^:#;]+?)(?P<separator>\s*:\s*)(?P<value>[01])(?P<tail>\s*(?:[#;].*)?)$", line)
        if not match:
            return None
        return match.group("name").strip(), match.group("value")

    @classmethod
    def _updated_mods_text(cls, text: str, name: str, enabled: bool | None) -> str:
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        wanted = name.casefold()
        found = False
        appended = False
        output: list[str] = []
        for line in lines:
            entry = cls._mods_entry(line)
            if entry is None or entry[0].casefold() != wanted:
                output.append(line)
                continue
            if found or enabled is None:
                continue
            output.append(re.sub(r"(\s*:\s*)[01]", rf"\g<1>{int(enabled)}", line, count=1))
            found = True
        if enabled is not None and not found:
            output.append(f"{name} : {int(enabled)}")
            appended = True
        result = newline.join(output)
        if output and (text.endswith(("\n", "\r")) or appended):
            result += newline
        return result

    def _write_mods_txt(self, path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(text.encode("utf-8"))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _mods_enabled(cls, path: Path, name: str) -> bool | None:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            entry = cls._mods_entry(line)
            if entry is not None and entry[0].casefold() == name.casefold():
                return entry[1] == "1"
        return None

    def _audit(self, manifest: ModManifest) -> ManifestAudit:
        audit = self.store.audit(manifest)
        if manifest.kind is not ModKind.UE4SS or audit.status is not AuditStatus.ENABLED:
            return audit
        state = self._mods_enabled(manifest.install_root.parent / "mods.txt", manifest.install_root.name)
        if state is None:
            return ManifestAudit(manifest.id, AuditStatus.MISSING)
        return ManifestAudit(
            manifest.id,
            AuditStatus.ENABLED if state else AuditStatus.DISABLED,
        )

    def _install_ue4ss(
        self,
        source_path: Path,
        inspected,
        dirs: dict[str, Path],
        transaction: Path,
        display_name: str | None,
        nexus_id: int | None,
        decision: str,
    ) -> dict[str, object]:
        source_root = inspected.content_root
        folder_name = _safe_label(source_root.name or inspected.display_name)
        target_root = dirs["ue4ss_mods"] / folder_name
        existing = next(
            (child for child in dirs["ue4ss_mods"].iterdir() if child.name.casefold() == folder_name.casefold()),
            None,
        )
        if existing is not None:
            raise ModConflictError({"files": [str(existing)], "choices": ["cancel"]})
        files = [path for path in source_root.rglob("*") if path.is_file() and not _is_reparse(path)]
        enabled_source = next((path for path in files if path.relative_to(source_root).as_posix().casefold() == "enabled.txt"), None)
        payload = [path for path in files if path != enabled_source]
        if not any(path.relative_to(source_root).as_posix().casefold() == "scripts/main.lua" for path in payload):
            raise ValueError("UE4SS 模组必须包含 Scripts/main.lua")
        mods_txt = dirs["ue4ss_mods"] / "mods.txt"
        old_config = mods_txt.read_bytes() if mods_txt.is_file() else None
        created: ModManifest | None = None
        metadata_root: Path | None = None
        try:
            target_root.mkdir(parents=True)
            installed: list[Path] = []
            for incoming in payload:
                destination = target_root / incoming.relative_to(source_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(incoming, destination)
                installed.append(destination)
            created = self.store.create(
                name=display_name or folder_name,
                kind=ModKind.UE4SS,
                install_root=target_root,
                files=installed,
                source_name=source_path.name,
                nexus_id=nexus_id,
            )
            if enabled_source is not None:
                metadata_root = self.data_dir / "disabled" / created.id
                metadata = metadata_root / "metadata" / "enabled.txt"
                metadata.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(enabled_source, metadata)
                created = replace(
                    created,
                    ue4ss_enabled_txt=ManifestFile("metadata/enabled.txt", metadata.stat().st_size, _sha256(metadata)),
                )
                self.store.save(created)
            current = old_config.decode("utf-8", errors="ignore") if old_config is not None else ""
            self._write_mods_txt(mods_txt, self._updated_mods_text(current, folder_name, True))
            return self._listed(created)
        except BaseException:
            if created is not None:
                self.store.delete(created.id)
            shutil.rmtree(target_root, ignore_errors=True)
            if metadata_root is not None:
                shutil.rmtree(metadata_root, ignore_errors=True)
            if old_config is None:
                mods_txt.unlink(missing_ok=True)
            else:
                mods_txt.parent.mkdir(parents=True, exist_ok=True)
                mods_txt.write_bytes(old_config)
            raise

    def _direct_group(self, source: Path) -> tuple[tuple[Path, ...], ...]:
        if source.suffix.casefold() != ".pak" or not source.is_file():
            raise ValueError("仅支持 .zip 或 .pak 文件")
        files = [source]
        for suffix in (".utoc", ".ucas"):
            sidecar = source.with_suffix(suffix)
            if sidecar.is_file() and not _is_reparse(sidecar):
                files.append(sidecar)
        return (tuple(files),)

    @staticmethod
    def _owned_by(manifests: Iterable[ModManifest], path: Path) -> ModManifest | None:
        for manifest in manifests:
            if manifest.install_root == path.parent and any(
                item.relative_path.casefold() == path.name.casefold()
                for item in manifest.files
            ):
                return manifest
        return None

    @_locked_write
    def install(
        self,
        source: str | os.PathLike[str],
        preferred_kind: str | ModKind | None = None,
        display_name: str | None = None,
        nexus_id: int | None = None,
        decision: str = "cancel",
    ) -> dict[str, object]:
        self._assert_stopped()
        dirs = self._prepare_dirs()
        source_path = Path(source)
        if not source_path.is_file() or _is_reparse(source_path):
            raise FileNotFoundError(f"模组文件不存在或不安全：{source_path}")
        if decision not in {"cancel", "replace", "keep_both"}:
            raise ValueError("decision 必须是 replace、keep_both 或 cancel")

        transaction = self.data_dir / "staging" / uuid.uuid4().hex
        transaction.mkdir(parents=True)
        published: list[Path] = []
        temporary_files: list[Path] = []
        backups: list[tuple[Path, Path]] = []
        old_manifests: dict[str, ModManifest] = {}
        merge_owner: ModManifest | None = None
        merge_manifest_bytes: bytes | None = None
        created: ModManifest | None = None
        try:
            if source_path.suffix.casefold() == ".zip":
                extracted = transaction / "extracted"
                inspected = inspect_and_extract(source_path, extracted)
                kind = self._kind(preferred_kind, inspected.kind)
                if kind is ModKind.UE4SS:
                    if inspected.kind is not ModKind.UE4SS:
                        raise ValueError("所选压缩包不是标准 UE4SS Scripts/main.lua 模组")
                    return self._install_ue4ss(
                        source_path, inspected, dirs, transaction,
                        display_name, nexus_id, decision,
                    )
                if inspected.kind not in (ModKind.PAK, ModKind.LOGICPAK):
                    raise ValueError("此服务仅支持 PAK、LogicMods 与 UE4SS")
                groups = inspected.groups
                name = display_name or inspected.display_name
            else:
                kind = self._kind(preferred_kind, ModKind.PAK)
                groups = self._direct_group(source_path)
                name = display_name or source_path.stem
            target = self._target(dirs, kind)
            payload_bytes = sum(path.stat().st_size for group in groups for path in group)
            if shutil.disk_usage(target)[2] < payload_bytes or shutil.disk_usage(self.data_dir)[2] < payload_bytes:
                raise OSError("磁盘空间不足，无法安装模组")

            planned = [(path, target / path.name) for group in groups for path in group]
            manifests = self.store.list()
            overlaps = [
                (incoming, destination)
                for incoming, destination in planned
                if destination.is_file()
            ]
            overlap_owners = [
                self._owned_by(manifests, destination)
                for _, destination in overlaps
            ]
            owner_ids = {owner.id for owner in overlap_owners if owner is not None}
            if len(owner_ids) > 1:
                raise ModConflictError({
                    "files": [str(destination) for _, destination in overlaps],
                    "choices": ["cancel"],
                })
            identical_overlaps = overlaps and all(
                _sha256(incoming) == _sha256(destination)
                for incoming, destination in overlaps
            )
            has_unmanaged_overlap = any(owner is None for owner in overlap_owners)
            if identical_overlaps:
                if len(owner_ids) == 1 and not has_unmanaged_overlap:
                    owner = next(owner for owner in overlap_owners if owner is not None)
                    if len(overlaps) == len(planned):
                        return self._listed(owner)
                    stems = {destination.stem.casefold() for _, destination in planned}
                    additions = [incoming for incoming, destination in planned if not destination.exists()]
                    if (
                        len(stems) != 1
                        or any(path.suffix.casefold() not in {".utoc", ".ucas"} for path in additions)
                    ):
                        raise ModConflictError({
                            "files": [str(destination) for _, destination in overlaps],
                            "choices": ["cancel"],
                        })
                    merge_owner = owner
                    merge_path = self.store.manifests_dir / f"{owner.id}.json"
                    merge_manifest_bytes = merge_path.read_bytes()
            conflicts = [
                (incoming, destination)
                for incoming, destination in planned
                if destination.exists() and _sha256(incoming) != _sha256(destination)
            ]
            if has_unmanaged_overlap:
                # Replacing a mixed group must republish every overlap so no file is shared.
                conflicts = overlaps
            if conflicts:
                details = {
                    "files": [str(destination) for _, destination in conflicts],
                    "choices": ["replace", "keep_both", "cancel"],
                }
                if decision == "cancel":
                    raise ModConflictError(details)
                if decision == "keep_both":
                    label = _safe_label(name)
                    renamed: list[tuple[Path, Path]] = []
                    for group in groups:
                        stem = group[0].stem
                        for incoming in group:
                            destination = target / f"{stem} ({label}){incoming.suffix.lower()}"
                            if destination.exists():
                                raise ModConflictError({**details, "files": [str(destination)]})
                            renamed.append((incoming, destination))
                    planned = renamed
                else:
                    owners = {
                        owner.id: owner
                        for _, destination in conflicts
                        if (owner := self._owned_by(manifests, destination)) is not None
                    }
                    old_manifests = owners
                    backup_root = transaction / "backup"
                    for owner in owners.values():
                        for item in owner.files:
                            live = owner.install_root / item.relative_path
                            if live.is_file():
                                backup = backup_root / owner.id / item.relative_path
                                backup.parent.mkdir(parents=True, exist_ok=True)
                                os.replace(live, backup)
                                backups.append((backup, live))
                    owned_paths = {original.resolve() for _, original in backups}
                    for _, destination in conflicts:
                        if destination.is_file() and destination.resolve() not in owned_paths:
                            backup = backup_root / "unmanaged" / destination.name
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(destination, backup)
                            backups.append((backup, destination))

            for incoming, destination in planned:
                if destination.exists():
                    # An identical existing file is reused rather than overwritten.
                    continue
                temporary = destination.with_name(f".{destination.name}.{transaction.name}.tmp")
                temporary_files.append(temporary)
                shutil.copy2(incoming, temporary)
                os.replace(temporary, destination)
                temporary_files.remove(temporary)
                published.append(destination)

            installed_files = [destination for _, destination in planned]
            if merge_owner is not None:
                known = {item.relative_path.casefold() for item in merge_owner.files}
                added_records = [
                    ManifestFile(path.name, path.stat().st_size, _sha256(path))
                    for path in published
                    if path.name.casefold() not in known
                ]
                merged = replace(
                    merge_owner,
                    files=tuple(sorted((*merge_owner.files, *added_records), key=lambda item: item.relative_path)),
                )
                self.store.save(merged)
                return self._listed(merged)
            created = self.store.create(
                name=_safe_label(name),
                kind=kind,
                install_root=target,
                files=installed_files,
                source_name=source_path.name,
                nexus_id=nexus_id,
            )
            for old_id in old_manifests:
                self.store.delete(old_id)
            return self._listed(created)
        except BaseException:
            if created is not None:
                self.store.delete(created.id)
            for path in temporary_files:
                path.unlink(missing_ok=True)
            for path in reversed(published):
                path.unlink(missing_ok=True)
            for backup, original in reversed(backups):
                original.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    os.replace(backup, original)
            for manifest in old_manifests.values():
                if not (self.store.manifests_dir / f"{manifest.id}.json").exists():
                    self.store.save(manifest)
            if merge_owner is not None and merge_manifest_bytes is not None:
                merge_path = self.store.manifests_dir / f"{merge_owner.id}.json"
                rollback_path = merge_path.with_name(f".{merge_path.name}.rollback.tmp")
                rollback_path.write_bytes(merge_manifest_bytes)
                os.replace(rollback_path, merge_path)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    @_locked_write
    def set_enabled(self, manifest_id: str, enabled: bool) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        audit = self._audit(manifest)
        if manifest.kind is ModKind.UE4SS:
            desired_status = AuditStatus.ENABLED if enabled else AuditStatus.DISABLED
            if manifest.enabled is enabled and audit.status is desired_status:
                return self._listed(manifest)
            if audit.status in (AuditStatus.MISSING, AuditStatus.MODIFIED, AuditStatus.CONFLICT):
                raise RuntimeError(f"模组文件状态异常：{audit.status.value}")
            mods_txt = manifest.install_root.parent / "mods.txt"
            old_config = mods_txt.read_bytes() if mods_txt.is_file() else None
            manifest_path = self.store.manifests_dir / f"{manifest.id}.json"
            manifest_bytes = manifest_path.read_bytes()
            changed = replace(manifest, enabled=enabled)
            try:
                current = old_config.decode("utf-8", errors="ignore") if old_config is not None else ""
                self._write_mods_txt(
                    mods_txt,
                    self._updated_mods_text(current, manifest.install_root.name, enabled),
                )
                self.store.save(changed)
                return self._listed(changed)
            except BaseException:
                if old_config is None:
                    mods_txt.unlink(missing_ok=True)
                else:
                    mods_txt.write_bytes(old_config)
                rollback = manifest_path.with_name(f".{manifest_path.name}.rollback.tmp")
                rollback.write_bytes(manifest_bytes)
                os.replace(rollback, manifest_path)
                raise
        if enabled and audit.status is AuditStatus.ENABLED:
            return self._listed(manifest)
        if not enabled and audit.status is AuditStatus.DISABLED:
            return self._listed(manifest)
        if audit.status is AuditStatus.CONFLICT and enabled:
            conflicts = [
                manifest.install_root / item.relative_path
                for item in manifest.files
                if (manifest.install_root / item.relative_path).exists()
            ]
            raise ModConflictError({
                "files": [str(path) for path in conflicts],
                "choices": ["cancel"],
            })
        if audit.status in (AuditStatus.MISSING, AuditStatus.CONFLICT):
            raise RuntimeError(f"模组文件状态异常：{audit.status.value}")

        disabled_root = self.data_dir / "disabled" / manifest.id
        transaction = self.data_dir / "staging" / uuid.uuid4().hex
        transaction.mkdir(parents=True)
        moves: list[tuple[Path, Path]] = []
        changed = replace(manifest, enabled=enabled)
        manifest_path = self.store.manifests_dir / f"{manifest.id}.json"
        manifest_bytes = manifest_path.read_bytes()
        result: dict[str, object]
        try:
            if not enabled:
                holding = transaction / "group"
                for item in manifest.files:
                    source = manifest.install_root / item.relative_path
                    destination = holding / item.relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    moves.append((destination, source))
                disabled_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(holding, disabled_root)
                moves = [(disabled_root / item.relative_path, manifest.install_root / item.relative_path) for item in manifest.files]
            else:
                conflicts = [
                    manifest.install_root / item.relative_path
                    for item in manifest.files
                    if (manifest.install_root / item.relative_path).exists()
                ]
                if conflicts:
                    raise ModConflictError({
                        "files": [str(path) for path in conflicts],
                        "choices": ["cancel"],
                    })
                for item in manifest.files:
                    source = disabled_root / item.relative_path
                    destination = manifest.install_root / item.relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    moves.append((destination, source))
            self.store.save(changed)
            result = self._listed(changed)
        except BaseException:
            for current, original in reversed(moves):
                original.parent.mkdir(parents=True, exist_ok=True)
                if current.exists():
                    os.replace(current, original)
            rollback_manifest = manifest_path.with_name(f".{manifest_path.name}.rollback.tmp")
            rollback_manifest.parent.mkdir(parents=True, exist_ok=True)
            rollback_manifest.write_bytes(manifest_bytes)
            os.replace(rollback_manifest, manifest_path)
            if manifest.enabled:
                self._remove_empty_parents(disabled_root, self.data_dir / "disabled")
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        if enabled:
            shutil.rmtree(disabled_root, ignore_errors=True)
        self._remove_empty_parents(disabled_root, self.data_dir / "disabled")
        return result

    @staticmethod
    def _remove_empty_parents(path: Path, stop: Path) -> None:
        current = path if path.is_dir() else path.parent
        while current != stop and current.is_relative_to(stop):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @_locked_write
    def delete(self, manifest_id: str, force_modified: bool = False) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        audit = self._audit(manifest)
        if audit.status in (AuditStatus.MODIFIED, AuditStatus.CONFLICT) and not force_modified:
            raise RuntimeError("模组文件已修改，需 force_modified=True 才能删除")
        transaction = self.data_dir / "staging" / uuid.uuid4().hex
        transaction.mkdir(parents=True)
        moved: list[tuple[Path, Path]] = []
        manifest_path = self.store.manifests_dir / f"{manifest.id}.json"
        manifest_bytes = manifest_path.read_bytes()
        mods_txt = manifest.install_root.parent / "mods.txt" if manifest.kind is ModKind.UE4SS else None
        old_config = mods_txt.read_bytes() if mods_txt is not None and mods_txt.is_file() else None
        try:
            disabled_root = self.data_dir / "disabled" / manifest.id
            for root in (manifest.install_root, disabled_root):
                for item in manifest.files:
                    source = root / item.relative_path
                    if source.is_file() and not _is_reparse(source):
                        backup = transaction / str(len(moved)) / item.relative_path
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, backup)
                        moved.append((backup, source))
            if manifest.kind is ModKind.UE4SS:
                metadata = disabled_root / "metadata" / "enabled.txt"
                if metadata.is_file():
                    backup = transaction / "metadata" / "enabled.txt"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(metadata, backup)
                    moved.append((backup, metadata))
                current = old_config.decode("utf-8", errors="ignore") if old_config is not None else ""
                self._write_mods_txt(
                    mods_txt,
                    self._updated_mods_text(current, manifest.install_root.name, None),
                )
            self.store.delete(manifest.id)
        except BaseException:
            if mods_txt is not None:
                if old_config is None:
                    mods_txt.unlink(missing_ok=True)
                else:
                    mods_txt.write_bytes(old_config)
            for backup, original in reversed(moved):
                original.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    os.replace(backup, original)
            if not manifest_path.exists():
                rollback = manifest_path.with_name(f".{manifest_path.name}.rollback.tmp")
                rollback.parent.mkdir(parents=True, exist_ok=True)
                rollback.write_bytes(manifest_bytes)
                os.replace(rollback, manifest_path)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        for _, original in moved:
            stop = manifest.install_root if original.is_relative_to(manifest.install_root) else self.data_dir / "disabled"
            self._remove_empty_parents(original, stop)
        if manifest.kind is ModKind.UE4SS:
            shutil.rmtree(manifest.install_root, ignore_errors=True)
            shutil.rmtree(self.data_dir / "disabled" / manifest.id, ignore_errors=True)
        return {"ok": True, "deleted": manifest.id}

    @_locked_write
    def rescan(self) -> list[dict[str, object]]:
        self._assert_stopped()
        self._prepare_dirs()
        dirs = get_mod_directories(self.game_root)
        tracked = {
            (str(manifest.install_root.resolve()).casefold(), item.relative_path.casefold())
            for manifest in self.store.list()
            for item in manifest.files
        }
        for root, kind in ((dirs["tilde_mods"], ModKind.PAK), (dirs["logic_mods"], ModKind.LOGICPAK)):
            if _is_reparse(root):
                continue
            for pak in sorted(root.glob("*.pak"), key=lambda path: path.name.casefold()):
                if _is_reparse(pak) or not pak.is_file():
                    continue
                key = (str(root.resolve()).casefold(), pak.name.casefold())
                if key in tracked:
                    continue
                group = [pak]
                for suffix in (".utoc", ".ucas"):
                    sidecar = pak.with_suffix(suffix)
                    if sidecar.is_file() and not _is_reparse(sidecar):
                        group.append(sidecar)
                manifest = self.store.create(pak.stem, kind, root, group, source_name="rescan")
                tracked.update((str(root.resolve()).casefold(), item.relative_path.casefold()) for item in manifest.files)

        ue4ss_root = dirs["ue4ss_mods"]
        tracked_roots = {
            str(manifest.install_root.resolve()).casefold()
            for manifest in self.store.list()
            if manifest.kind is ModKind.UE4SS
        }
        if not _is_reparse(ue4ss_root):
            for candidate in sorted(ue4ss_root.iterdir(), key=lambda path: path.name.casefold()):
                if (
                    not candidate.is_dir()
                    or _is_reparse(candidate)
                    or is_ue4ss_framework_mod(candidate.name)
                    or not (candidate / "Scripts" / "main.lua").is_file()
                    or str(candidate.resolve()).casefold() in tracked_roots
                ):
                    continue
                files = [path for path in candidate.rglob("*") if path.is_file() and not _is_reparse(path)]
                enabled_txt = next((path for path in files if path.relative_to(candidate).as_posix().casefold() == "enabled.txt"), None)
                payload = [path for path in files if path != enabled_txt]
                enabled = self._mods_enabled(ue4ss_root / "mods.txt", candidate.name)
                manifest = self.store.create(
                    candidate.name, ModKind.UE4SS, candidate, payload,
                    source_name="rescan", enabled=enabled is not False,
                )
                if enabled_txt is not None:
                    metadata = self.data_dir / "disabled" / manifest.id / "metadata" / "enabled.txt"
                    try:
                        metadata.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(enabled_txt, metadata)
                        manifest = replace(
                            manifest,
                            ue4ss_enabled_txt=ManifestFile(
                                "metadata/enabled.txt", metadata.stat().st_size, _sha256(metadata)
                            ),
                        )
                        self.store.save(manifest)
                    except BaseException:
                        if metadata.exists():
                            enabled_txt.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(metadata, enabled_txt)
                        self.store.delete(manifest.id)
                        shutil.rmtree(metadata.parent.parent, ignore_errors=True)
                        raise
                tracked_roots.add(str(candidate.resolve()).casefold())
        return self.list_mods()

    def list_mods(self) -> list[dict[str, object]]:
        return [self._listed(manifest) for manifest in self.store.list()]
