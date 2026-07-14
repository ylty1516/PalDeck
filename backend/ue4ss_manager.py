"""PalDeck-owned UE4SS framework lifecycle management."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

from . import ue4ss_installer
from .game_lock import game_write_lock
from .process_utils import is_palworld_running
from .trash_service import TrashService
from .trash_store import TrashRecord
from .ue4ss_ownership import (
    OwnedFrameworkFile,
    OwnershipRecord,
    OwnershipStore,
    OwnershipStoreInvalid,
)
from .ue4ss_provider import ASSET_NAME, Ue4ssProvider


class Ue4ssLifecycleError(RuntimeError):
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
                "status": "conflict",
                "ownership": "unknown",
                "integrity": "conflict",
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
                "status": "external" if external else "absent",
                "ownership": "external" if external else "none",
                "integrity": "unknown" if external else "absent",
                "source": None,
                "owned_files": 0,
                "installer": installer_state,
            }
        try:
            audit = self.ownership.audit(record, self.win64)
        except (OSError, ValueError) as exc:
            return {
                "status": "conflict",
                "ownership": "PalDeck",
                "integrity": "conflict",
                "source": record.source,
                "owned_files": len(record.files),
                "error": str(exc),
                "installer": installer_state,
            }
        return {
            "status": "managed",
            "ownership": "PalDeck",
            "integrity": audit.integrity,
            "source": record.source,
            "asset_name": record.asset_name,
            "asset_sha256": record.asset_sha256,
            "installed_at": record.installed_at,
            "owned_files": len(record.files),
            "missing": list(audit.missing),
            "modified": list(audit.modified),
            "conflicts": list(audit.conflicts),
            "installer": installer_state,
        }

    def _assert_stopped(self) -> None:
        if self.game_running():
            raise Ue4ssLifecycleError("幻兽帕鲁正在运行，无法修改 UE4SS")

    def _install_payload(
        self,
        payload: bytes,
        *,
        source: str,
        asset_name: str,
        asset_sha256: str,
        repair: bool,
    ) -> dict[str, object]:
        self._assert_stopped()
        with game_write_lock(self.data_dir):
            result = ue4ss_installer.install_from_bytes(
                self.game_root,
                payload,
                confirm_replace=repair,
                require_palworld_layout=True,
                preserve_mutable=repair,
            )
            files = tuple(OwnedFrameworkFile(**item) for item in result["installed_files"])
            record = OwnershipRecord.create(
                self.game_fingerprint,
                source,
                asset_name,
                asset_sha256,
                files,
                result["framework_mods"],
            )
            self.ownership.save(record)
        return {**result, "ownership": "PalDeck", "integrity": "healthy"}

    def install_bundled(self) -> dict[str, object]:
        current = self.state()
        if current["status"] == "external":
            raise Ue4ssLifecycleError("检测到外部 UE4SS，PalDeck 不会接管或覆盖")
        if current["status"] == "managed":
            raise Ue4ssLifecycleError("UE4SS 已由 PalDeck 管理，请使用修复")
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
            repair=False,
        )

    def repair(self) -> dict[str, object]:
        state = self.state()
        if state["status"] != "managed":
            if state["status"] == "external":
                raise Ue4ssLifecycleError("外部 UE4SS 不可由 PalDeck 修复")
            raise Ue4ssLifecycleError("没有可修复的 PalDeck UE4SS")
        if state["integrity"] == "conflict":
            raise Ue4ssLifecycleError("UE4SS 路径存在冲突，已拒绝修复")
        if state["integrity"] == "healthy":
            return {"ok": True, "unchanged": True, "state": state}
        record = self._record()
        assert record is not None
        if record.source != "bundled":
            raise Ue4ssLifecycleError("当前 UE4SS 来源暂不支持自动修复")
        bundled = self.provider.bundled_status()
        if not bundled.get("available"):
            raise Ue4ssLifecycleError(str(bundled.get("error") or "内置 UE4SS 不可用"))
        asset = bundled["asset"]
        if asset.sha256.casefold() != record.asset_sha256:
            raise Ue4ssLifecycleError("内置 UE4SS 与原所有权摘要不一致，拒绝修复")
        return self._install_payload(
            self.provider.bundled_archive(),
            source=record.source,
            asset_name=record.asset_name,
            asset_sha256=record.asset_sha256,
            repair=True,
        )

    def uninstall(self, *, now: datetime | None = None) -> dict[str, object]:
        state = self.state()
        if state["status"] == "external":
            raise Ue4ssLifecycleError("外部 UE4SS 不可由 PalDeck 卸载")
        if state["status"] != "managed":
            raise Ue4ssLifecycleError("没有可卸载的 PalDeck UE4SS")
        if state["integrity"] != "healthy":
            raise Ue4ssLifecycleError("UE4SS 完整性异常，请先修复再安全卸载")
        record = self._record()
        assert record is not None
        self._assert_stopped()
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise ValueError("trash timestamp must include a timezone")
        trash_id = uuid.uuid4().hex
        moved = []
        trash_record = None
        with game_write_lock(self.data_dir):
            try:
                for item in record.files:
                    if item.mutable:
                        continue
                    trash_file = self.trash.move_to_payload(
                        trash_id, "framework_win64", item.relative_path
                    )
                    moved.append(trash_file)
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
                    ue4ss_state=None,
                    framework_ownership=OwnershipStore._to_record(record),
                )
                self.trash.store.save(trash_record)
                self.ownership.delete(self.game_fingerprint)
            except BaseException:
                if trash_record is not None:
                    self.trash.store.delete(trash_id)
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
        if record.framework_ownership is None:
            raise Ue4ssLifecycleError("UE4SS 回收记录缺少所有权清单")
        ownership = OwnershipStore._record(record.framework_ownership)
        if self._record() is not None:
            raise Ue4ssLifecycleError("当前游戏已存在 PalDeck UE4SS 所有权记录")
        restored = []
        with game_write_lock(self.data_dir):
            try:
                for item in record.files:
                    destination, payload = self.trash.restore_file(trash_id, item)
                    restored.append((destination, payload))
                self.ownership.save(ownership)
                self.trash.store.delete(trash_id)
            except BaseException:
                for destination, payload in reversed(restored):
                    try:
                        payload.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists() and not os.path.lexists(payload):
                            os.replace(destination, payload)
                    except BaseException:
                        pass
                raise
        return {"ok": True, "restored_files": len(restored), "state": self.state()}
