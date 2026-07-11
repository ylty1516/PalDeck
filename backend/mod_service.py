"""Transactional management for PAK and LogicMods file groups."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Iterable

from .archive_utils import inspect_and_extract
from .domain import AuditStatus, ManifestFile, ModKind, ModManifest
from .game_detector import ensure_mod_folders, get_mod_directories
from .manifest_store import ManifestStore
from .process_utils import check_directory_writable, is_palworld_running

_SIDECARS = (".pak", ".utoc", ".ucas")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
        self.store = ManifestStore(self.data_dir / "manifests")
        self.game_running = game_running

    def _assert_stopped(self) -> None:
        if self.game_running():
            raise GameRunningError("幻兽帕鲁正在运行，无法修改模组")

    def _prepare_dirs(self) -> dict[str, Path]:
        ensure_mod_folders(self.game_root)
        dirs = get_mod_directories(self.game_root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "staging").mkdir(parents=True, exist_ok=True)
        for path in (dirs["tilde_mods"], dirs["logic_mods"], self.data_dir):
            if not check_directory_writable(path):
                raise PermissionError(f"目录不可写：{path}")
        return dirs

    @staticmethod
    def _kind(value: str | ModKind | None, fallback: ModKind) -> ModKind:
        if value in (None, "", "auto"):
            return fallback
        kind = ModKind(value)
        if kind not in (ModKind.PAK, ModKind.LOGICPAK):
            raise ValueError("仅支持 pak 或 logicpak")
        return kind

    @staticmethod
    def _target(dirs: dict[str, Path], kind: ModKind) -> Path:
        return dirs["logic_mods"] if kind is ModKind.LOGICPAK else dirs["tilde_mods"]

    @staticmethod
    def _serialize_manifest(manifest: ModManifest, audit) -> dict[str, object]:
        value = asdict(manifest)
        value["kind"] = manifest.kind.value
        value["install_root"] = str(manifest.install_root)
        value["files"] = [asdict(item) for item in manifest.files]
        if manifest.ue4ss_enabled_txt is not None:
            value["ue4ss_enabled_txt"] = asdict(manifest.ue4ss_enabled_txt)
        value["audit"] = {
            "manifest_id": audit.manifest_id,
            "status": audit.status.value,
        }
        return value

    def _listed(self, manifest: ModManifest) -> dict[str, object]:
        return self._serialize_manifest(manifest, self.store.audit(manifest))

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
        manifest_before = {path.name for path in self.store.manifests_dir.glob("*.json")} if self.store.manifests_dir.is_dir() else set()
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
                if inspected.kind not in (ModKind.PAK, ModKind.LOGICPAK):
                    raise ValueError("此服务仅支持 PAK 与 LogicMods")
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
            if identical_overlaps:
                if owner_ids and any(owner is None for owner in overlap_owners):
                    raise ModConflictError({
                        "files": [str(destination) for _, destination in overlaps],
                        "choices": ["cancel"],
                    })
                if len(owner_ids) == 1:
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
                    if len(owners) == 0 or any(
                        self._owned_by(manifests, destination) is None
                        for _, destination in conflicts
                    ):
                        raise ModConflictError(details)
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
            if self.store.manifests_dir.is_dir():
                for path in self.store.manifests_dir.glob("*.json"):
                    if path.name not in manifest_before:
                        path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    def set_enabled(self, manifest_id: str, enabled: bool) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        audit = self.store.audit(manifest)
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

    def delete(self, manifest_id: str, force_modified: bool = False) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        audit = self.store.audit(manifest)
        if audit.status in (AuditStatus.MODIFIED, AuditStatus.CONFLICT) and not force_modified:
            raise RuntimeError("模组文件已修改，需 force_modified=True 才能删除")
        transaction = self.data_dir / "staging" / uuid.uuid4().hex
        transaction.mkdir(parents=True)
        moved: list[tuple[Path, Path]] = []
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
            self.store.delete(manifest.id)
        except BaseException:
            for backup, original in reversed(moved):
                original.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    os.replace(backup, original)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        for _, original in moved:
            stop = manifest.install_root if original.is_relative_to(manifest.install_root) else self.data_dir / "disabled"
            self._remove_empty_parents(original, stop)
        return {"ok": True, "deleted": manifest.id}

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
        return self.list_mods()

    def list_mods(self) -> list[dict[str, object]]:
        return [self._listed(manifest) for manifest in self.store.list()]
