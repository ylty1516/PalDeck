"""PalDeck-owned UE4SS framework lifecycle management."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path, PureWindowsPath
from typing import Callable

from . import ue4ss_installer
from .archive_utils import extract_archive_safely
from .game_detector import is_ue4ss_framework_mod
from .game_lock import game_write_lock
from .manifest_store import validate_no_reparse_ancestors
from .process_utils import is_palworld_running
from .trash_service import TrashService
from .trash_store import TrashRecord
from .ue4ss_ownership import (
    OwnedFrameworkFile,
    OwnershipRecord,
    OwnershipStore,
    OwnershipStoreInvalid,
)
from .ue4ss_config import enabled_state, remove_entries, update_entry
from .ue4ss_provider import ASSET_NAME, Ue4ssProvider


class Ue4ssLifecycleError(RuntimeError):
    def __init__(self, message: str, details: dict[str, object] | None = None):
        self.details = details or {}
        super().__init__(message)


class Ue4ssRepairConflict(Ue4ssLifecycleError):
    pass


class Ue4ssModifiedFiles(Ue4ssLifecycleError):
    pass


class Ue4ssFrameworkManager:
    def __init__(
        self,
        game_root: str | os.PathLike[str],
        data_dir: str | os.PathLike[str],
        *,
        provider: Ue4ssProvider | None = None,
        game_running: Callable[[], bool] = is_palworld_running,
    ) -> None:
        self.game_root = Path(os.path.abspath(game_root))
        self.data_dir = Path(os.path.abspath(data_dir))
        self.win64 = self.game_root / "Pal" / "Binaries" / "Win64"
        self.provider = provider or Ue4ssProvider()
        self.game_running = game_running
        self.trash = TrashService(
            self.game_root, self.data_dir, game_running=game_running
        )
        self.ownership = OwnershipStore(self.data_dir / "ue4ss-framework-v1.json")

    @property
    def game_fingerprint(self) -> str:
        return self.trash.game_fingerprint

    def _record(self) -> OwnershipRecord | None:
        try:
            return self.ownership.get(self.game_fingerprint)
        except KeyError:
            return None

    def state(self) -> dict[str, object]:
        installer_state = ue4ss_installer.status(self.game_root)
        try:
            record = self._record()
        except OwnershipStoreInvalid as exc:
            return {
                "installed": bool(installer_state.get("installed")),
                "status": "conflict",
                "ownership": "unknown",
                "managed": False,
                "integrity": "conflict",
                "owned_files": 0,
                "missing_files": 0,
                "modified_files": 0,
                "repair_available": False,
                "uninstall_available": False,
                "error": str(exc),
                "installer": installer_state,
            }
        if record is None:
            markers = installer_state.get("markers", {})
            external = isinstance(markers, dict) and any(
                markers.get(key) is True
                for key in ("dwmapi", "xinput1_3", "ue4ss_dll_root", "ue4ss_dll_sub")
            )
            return {
                "installed": external,
                "status": "external" if external else "absent",
                "ownership": "external" if external else "none",
                "managed": False,
                "integrity": "unmanaged" if external else "not_installed",
                "source": None,
                "owned_files": 0,
                "missing_files": 0,
                "modified_files": 0,
                "repair_available": external,
                "uninstall_available": external,
                "installer": installer_state,
            }
        try:
            audit = self.ownership.audit(record, self.win64)
        except (OSError, ValueError) as exc:
            return {
                "installed": bool(installer_state.get("installed")),
                "status": "conflict",
                "ownership": "PalDeck",
                "managed": True,
                "integrity": "conflict",
                "source": record.source,
                "owned_files": len(record.files),
                "missing_files": 0,
                "modified_files": 0,
                "repair_available": False,
                "uninstall_available": False,
                "error": str(exc),
                "installer": installer_state,
            }
        abnormal = len(audit.missing) + len(audit.modified) + len(audit.conflicts)
        return {
            "installed": bool(installer_state.get("installed")),
            "status": "managed",
            "ownership": "PalDeck",
            "managed": True,
            "integrity": audit.integrity,
            "source": record.source,
            "asset_name": record.asset_name,
            "asset_sha256": record.asset_sha256,
            "installed_at": record.installed_at,
            "owned_files": len(record.files),
            "missing_files": len(audit.missing),
            "modified_files": len(audit.modified),
            "conflict_files": len(audit.conflicts),
            "abnormal_files": abnormal,
            "missing": list(audit.missing),
            "modified": list(audit.modified),
            "conflicts": list(audit.conflicts),
            "repair_available": True,
            "uninstall_available": audit.integrity != "conflict",
            "installer": installer_state,
        }

    def _assert_stopped(self) -> None:
        if self.game_running():
            raise ue4ss_installer.Ue4ssGameRunningError(
                "幻兽帕鲁正在运行，无法修改 UE4SS"
            )

    def _snapshot_install_targets(
        self, payload: bytes
    ) -> tuple[Path, list[tuple[Path, Path | None]]]:
        transaction = Path(
            tempfile.mkdtemp(prefix=".paldeck-ue4ss-manager-", dir=self.win64)
        )
        extract = transaction / "extract"
        extract_archive_safely(
            BytesIO(payload),
            extract,
            policy=ue4ss_installer.UE4SS_ARCHIVE_POLICY,
        )
        package = ue4ss_installer._find_package_root(extract)
        snapshots: list[tuple[Path, Path | None]] = []
        for index, source in enumerate(
            path for path in package.rglob("*") if path.is_file()
        ):
            destination = validate_no_reparse_ancestors(
                self.win64 / source.relative_to(package)
            )
            backup: Path | None = None
            if destination.is_file():
                backup = transaction / "backups" / str(index) / destination.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            snapshots.append((destination, backup))
        return transaction, snapshots

    def _restore_install_targets(
        self,
        snapshots: list[tuple[Path, Path | None]],
        ownership_before: bytes | None,
    ) -> None:
        for destination, backup in reversed(snapshots):
            if backup is None:
                if destination.is_file():
                    destination.unlink()
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file():
                destination.unlink()
            os.replace(backup, destination)
        if ownership_before is None:
            self.ownership.path.unlink(missing_ok=True)
        else:
            self.trash._write_atomic(self.ownership.path, ownership_before)
        parents = {
            parent
            for destination, _backup in snapshots
            for parent in destination.parents
            if parent != self.win64 and parent.is_relative_to(self.win64)
        }
        for directory in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _install_payload(
        self,
        payload: bytes,
        *,
        source: str,
        asset_name: str,
        asset_sha256: str,
        repair: bool,
        require_palworld_layout: bool = True,
    ) -> dict[str, object]:
        self._assert_stopped()
        with game_write_lock(self.data_dir):
            transaction, snapshots = self._snapshot_install_targets(payload)
            ownership_before = (
                self.ownership.path.read_bytes()
                if self.ownership.path.is_file()
                else None
            )
            try:
                result = ue4ss_installer.install_from_bytes(
                    self.game_root,
                    payload,
                    confirm_replace=repair,
                    require_palworld_layout=require_palworld_layout,
                    preserve_mutable=repair,
                )
                files = tuple(
                    OwnedFrameworkFile(**item) for item in result["installed_files"]
                )
                record = OwnershipRecord.create(
                    self.game_fingerprint,
                    source,
                    asset_name,
                    asset_sha256,
                    files,
                    result["framework_mods"],
                )
                self.ownership.save(record)
            except BaseException:
                self._restore_install_targets(snapshots, ownership_before)
                raise
            finally:
                shutil.rmtree(transaction, ignore_errors=True)
        return {**result, "ownership": "PalDeck", "integrity": "healthy"}

    def _replacement_requested(self, confirm_replace: bool) -> bool:
        current = self.state()
        occupied = current["status"] in {"external", "managed", "conflict"}
        if occupied and not confirm_replace:
            markers = current.get("installer", {}).get("markers", {})
            raise ue4ss_installer.Ue4ssConflictError(markers)
        return occupied

    def install_bundled(self, *, confirm_replace: bool = False) -> dict[str, object]:
        replacing = self._replacement_requested(confirm_replace)
        status = self.provider.bundled_status()
        if not status.get("available"):
            raise Ue4ssLifecycleError(str(status.get("error") or "内置 UE4SS 不可用"))
        asset = status["asset"]
        payload = self.provider.bundled_archive()
        return self._install_payload(
            payload,
            source="bundled",
            asset_name=asset.name,
            asset_sha256=asset.sha256,
            repair=replacing,
        )

    def install_local_zip(
        self,
        zip_path: str | os.PathLike[str],
        *,
        confirm_replace: bool = False,
    ) -> dict[str, object]:
        replacing = self._replacement_requested(confirm_replace)
        archive = Path(zip_path)
        if not archive.is_file() or archive.suffix.casefold() != ".zip":
            raise ValueError("UE4SS 安装仅支持本地 .zip")
        if archive.stat().st_size > ue4ss_installer.UE4SS_ARCHIVE_POLICY.max_total_bytes:
            raise ValueError("UE4SS ZIP 超过安全大小限制")
        payload = archive.read_bytes()
        return self._install_payload(
            payload,
            source="local_zip",
            asset_name=archive.name,
            asset_sha256=hashlib.sha256(payload).hexdigest(),
            repair=replacing,
            require_palworld_layout=False,
        )

    def install_upstream(
        self,
        asset,
        zip_path: str | os.PathLike[str],
        *,
        confirm_replace: bool = False,
    ) -> dict[str, object]:
        replacing = self._replacement_requested(confirm_replace)
        archive = Path(zip_path)
        payload = archive.read_bytes()
        return self._install_payload(
            payload,
            source="upstream",
            asset_name=str(asset.name),
            asset_sha256=str(asset.sha256),
            repair=replacing,
            require_palworld_layout=True,
        )

    def _bundled_asset_payload(self):
        bundled = self.provider.bundled_status()
        if not bundled.get("available"):
            raise Ue4ssLifecycleError(str(bundled.get("error") or "内置 UE4SS 不可用"))
        return bundled["asset"], self.provider.bundled_archive()

    def _known_unmanaged_record(self) -> OwnershipRecord:
        asset, payload = self._bundled_asset_payload()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="paldeck-ue4ss-audit-", dir=self.data_dir))
        try:
            extract = temporary / "extract"
            extract_archive_safely(
                BytesIO(payload),
                extract,
                policy=ue4ss_installer.UE4SS_ARCHIVE_POLICY,
            )
            package = ue4ss_installer._find_package_root(extract)
            files: list[OwnedFrameworkFile] = []
            framework_mods: set[str] = set()
            for source in package.rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(package)
                destination = self.win64 / relative
                parts = relative.parts
                for index, part in enumerate(parts[:-1]):
                    if part.casefold() == "mods" and index + 1 < len(parts):
                        name = parts[index + 1]
                        if is_ue4ss_framework_mod(name):
                            framework_mods.add(name)
                        break
                if not os.path.lexists(destination):
                    continue
                expected = OwnedFrameworkFile.from_path(
                    package,
                    source,
                    mutable=relative.as_posix().casefold()
                    in ue4ss_installer.MUTABLE_FRAMEWORK_FILES,
                )
                files.append(expected)
            if not files:
                raise Ue4ssLifecycleError("未找到可安全识别的 UE4SS 固定文件")
            return OwnershipRecord.create(
                self.game_fingerprint,
                "bundled",
                asset.name,
                asset.sha256,
                files,
                framework_mods,
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def repair(self, *, confirm_replace: bool = False) -> dict[str, object]:
        state = self.state()
        if state["status"] == "absent":
            raise Ue4ssLifecycleError("没有可修复的 UE4SS")
        if state["integrity"] == "healthy":
            return {"ok": True, "unchanged": True, "state": state}
        if state["integrity"] in {"unmanaged", "modified", "conflict"} and not confirm_replace:
            details = {
                "integrity": state["integrity"],
                "missing": list(state.get("missing", []))[:20],
                "modified": list(state.get("modified", []))[:20],
                "conflicts": list(state.get("conflicts", []))[:20],
            }
            raise Ue4ssRepairConflict("UE4SS 文件需要确认后修复", details)
        asset, payload = self._bundled_asset_payload()
        return self._install_payload(
            payload,
            source="bundled",
            asset_name=asset.name,
            asset_sha256=asset.sha256,
            repair=True,
        )

    def _framework_configs(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("classic", self.win64 / "Mods" / "mods.txt"),
            ("nested", self.win64 / "ue4ss" / "Mods" / "mods.txt"),
        )

    def _remove_framework_entries(
        self, framework_mods: tuple[str, ...]
    ) -> tuple[dict[str, object] | None, list[tuple[Path, bytes]]]:
        configs: list[dict[str, object]] = []
        snapshots: list[tuple[Path, bytes]] = []
        for root, path in self._framework_configs():
            if not path.is_file():
                continue
            original = path.read_bytes()
            entries = {
                name: state
                for name in framework_mods
                if (state := enabled_state(original, name)) is not None
            }
            updated = remove_entries(original, framework_mods)
            if updated != original:
                snapshots.append((path, original))
                self.trash._write_atomic(path, updated)
            if entries:
                configs.append({"root": root, "entries": entries})
        return ({"configs": configs} if configs else None), snapshots

    def _restore_config_snapshots(
        self, snapshots: list[tuple[Path, bytes | None]]
    ) -> None:
        for path, data in reversed(snapshots):
            if data is None:
                path.unlink(missing_ok=True)
            else:
                self.trash._write_atomic(path, data)

    def _merge_framework_entries(
        self, state: dict[str, object] | None
    ) -> list[tuple[Path, bytes | None]]:
        if state is None:
            return []
        configs = state.get("configs")
        if not isinstance(configs, list):
            raise ValueError("UE4SS 回收配置状态无效")
        paths = dict(self._framework_configs())
        snapshots: list[tuple[Path, bytes | None]] = []
        for config in configs:
            if not isinstance(config, dict) or set(config) != {"root", "entries"}:
                raise ValueError("UE4SS 回收配置项无效")
            path = paths.get(config["root"])
            entries = config["entries"]
            if path is None or not isinstance(entries, dict):
                raise ValueError("UE4SS 回收配置根无效")
            original = path.read_bytes() if path.is_file() else None
            updated = original or b""
            for name, value in entries.items():
                if type(name) is not str or type(value) is not bool:
                    raise ValueError("UE4SS 回收配置条目无效")
                if enabled_state(updated, name) is None:
                    updated = update_entry(updated, name, value)
            if updated != (original or b""):
                snapshots.append((path, original))
                self.trash._write_atomic(path, updated)
        return snapshots

    def uninstall(
        self,
        *,
        confirm_modified: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        state = self.state()
        if state["status"] == "absent":
            raise Ue4ssLifecycleError("没有可卸载的 UE4SS")
        managed_record = self._record()
        record = managed_record or self._known_unmanaged_record()
        audit = self.ownership.audit(record, self.win64)
        if audit.integrity == "conflict":
            raise Ue4ssLifecycleError(
                "UE4SS 路径存在目录或重解析点冲突，已拒绝卸载",
                {"files": list(audit.conflicts)[:20]},
            )
        if audit.modified and not confirm_modified:
            raise Ue4ssModifiedFiles(
                "UE4SS 受管文件已修改，确认后可移入回收站",
                {"files": list(audit.modified)[:20], "file_count": len(audit.modified)},
            )
        self._assert_stopped()
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise ValueError("trash timestamp must include a timezone")
        trash_id = uuid.uuid4().hex
        moved = []
        config_snapshots: list[tuple[Path, bytes]] = []
        trash_record = None
        with game_write_lock(self.data_dir):
            try:
                for item in record.files:
                    if item.mutable:
                        continue
                    source = self.trash.resolve_original(
                        "framework_win64", item.relative_path
                    )
                    if not source.is_file():
                        continue
                    trash_file = self.trash.move_to_payload(
                        trash_id, "framework_win64", item.relative_path
                    )
                    moved.append(trash_file)
                if not moved:
                    raise Ue4ssLifecycleError("没有可回收的 UE4SS 核心文件")
                ue4ss_state, config_snapshots = self._remove_framework_entries(
                    record.framework_mods
                )
                trash_record = TrashRecord(
                    id=trash_id,
                    schema_version=1,
                    entry_type="ue4ss_framework",
                    name="UE4SS Framework",
                    created_at=current.isoformat(),
                    expires_at=(current + timedelta(days=30)).isoformat(),
                    game_fingerprint=self.game_fingerprint,
                    files=tuple(moved),
                    manifest=None,
                    ue4ss_state=ue4ss_state,
                    framework_ownership=(
                        OwnershipStore._to_record(managed_record)
                        if managed_record is not None
                        else None
                    ),
                )
                self.trash.store.save(trash_record)
                if managed_record is not None:
                    self.ownership.delete(self.game_fingerprint)
            except BaseException:
                if trash_record is not None:
                    self.trash.store.delete(trash_id)
                self._restore_config_snapshots(config_snapshots)
                for item in reversed(moved):
                    try:
                        self.trash.restore_file(trash_id, item)
                    except BaseException:
                        pass
                raise
        self._remove_empty_owned_directories(record)
        return {"ok": True, "trash_id": trash_id, "expires_at": trash_record.expires_at}

    def _remove_empty_owned_directories(self, record: OwnershipRecord) -> None:
        parents = set()
        for item in record.files:
            if item.mutable:
                continue
            path = self.win64 / Path(*PureWindowsPath(item.relative_path).parts)
            parents.update(parent for parent in path.parents if parent != self.win64 and parent.is_relative_to(self.win64))
        for directory in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    def restore(self, trash_id: str) -> dict[str, object]:
        self._assert_stopped()
        record = self.trash.store.get(trash_id)
        if record.entry_type != "ue4ss_framework" or record.game_fingerprint != self.game_fingerprint:
            raise Ue4ssLifecycleError("回收记录不属于当前游戏的 UE4SS")
        ownership = (
            OwnershipStore._record(record.framework_ownership)
            if record.framework_ownership is not None
            else None
        )
        if ownership is not None and self._record() is not None:
            raise Ue4ssLifecycleError("当前游戏已存在 PalDeck UE4SS 所有权记录")
        restored = []
        config_snapshots: list[tuple[Path, bytes | None]] = []
        with game_write_lock(self.data_dir):
            try:
                for item in record.files:
                    destination, payload = self.trash.restore_file(trash_id, item)
                    restored.append((destination, payload))
                config_snapshots = self._merge_framework_entries(record.ue4ss_state)
                if ownership is not None:
                    self.ownership.save(ownership)
                self.trash.store.delete(trash_id)
            except BaseException:
                self._restore_config_snapshots(config_snapshots)
                for destination, payload in reversed(restored):
                    try:
                        payload.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists() and not os.path.lexists(payload):
                            os.replace(destination, payload)
                    except BaseException:
                        pass
                raise
        return {"ok": True, "restored_files": len(restored), "state": self.state()}
