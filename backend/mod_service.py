"""Transactional management for PAK and LogicMods file groups."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Iterable

from .archive_utils import inspect_and_extract
from .domain import (
    AuditStatus,
    ManifestAudit,
    ManifestFile,
    ManifestFileAudit,
    ModKind,
    ModManifest,
)
from .game_detector import (
    ensure_mod_folders,
    get_mod_directories,
    get_palschema_mod_roots,
    get_ue4ss_mod_roots,
    is_ue4ss_framework_mod,
    is_ue4ss_mod_folder,
)
from .game_lock import game_write_lock
from .ignored_mod_store import IgnoredIdentity, IgnoredModStore
from .manifest_store import ManifestStore, _relative_path_key, validate_no_reparse_ancestors
from .mod_value_service import ModValueConflict, ModValueService
from .process_utils import check_directory_writable, is_palworld_running
from .repair_vault import RepairVault
from .trash_service import TrashService
from .ue4ss_config import enabled_state, parse_entry, remove_entry, update_entry

_SIDECARS = (".pak", ".utoc", ".ucas")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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


class ModifiedFilesError(RuntimeError):
    def __init__(self, files: Iterable[Path | str]):
        self.details = {"files": [str(path) for path in files]}
        super().__init__("模组文件已修改")


class NotExternalModError(RuntimeError):
    """A PalDeck-installed mod cannot be removed from management only."""


class RepairConfirmationRequired(RuntimeError):
    def __init__(self, plan: dict[str, object]):
        self.details = plan
        super().__init__("修复会替换或隔离已修改的文件，需要确认")


class RepairPlanStale(RuntimeError):
    def __init__(self, plan: dict[str, object]):
        self.details = plan
        super().__init__("Mod 状态已变化，请重新检查后再修复")



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_cross_device_error(error: OSError) -> bool:
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17


def _move_verified(
    source: Path, destination: Path, expected_sha256: str | None = None
) -> None:
    """Move one regular file, falling back to a verified copy across volumes."""
    source = validate_no_reparse_ancestors(source)
    destination = validate_no_reparse_ancestors(destination)
    if _is_reparse(source) or not source.is_file():
        raise ValueError(f"移动源文件不存在或不安全：{source}")
    if os.path.lexists(destination):
        raise FileExistsError(f"移动目标已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    validate_no_reparse_ancestors(destination.parent)
    try:
        os.replace(source, destination)
        return
    except OSError as error:
        if not _is_cross_device_error(error):
            raise

    digest = expected_sha256 or _sha256(source)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        if _sha256(temporary) != digest:
            raise ValueError("跨盘符移动校验失败")
        os.replace(temporary, destination)
        try:
            source.unlink()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)


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
            known_roots=(
                dirs["tilde_mods"], dirs["logic_mods"],
                *get_ue4ss_mod_roots(self.game_root),
                *get_palschema_mod_roots(self.game_root),
            ),
        )
        self.game_running = game_running
        self.trash_service = TrashService(
            self.game_root, self.data_dir, game_running=self.game_running,
        )
        self.ignored_store = IgnoredModStore(self.data_dir / "ignored-mods-v1.json")
        self.repair_vault = RepairVault(self.data_dir / "repair-vault-v1")
        self.value_service = ModValueService()
        self._migrate_legacy_once()

    def _transaction_lock(self, timeout: float = 10.0):
        return game_write_lock(self.data_dir, timeout=timeout)

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
                    _move_verified(source, disabled)
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
                        _move_verified(disabled, source)
                raise

    def _assert_stopped(self) -> None:
        if self.game_running():
            raise GameRunningError("幻兽帕鲁正在运行，无法修改模组")

    def _prepare_dirs(self) -> dict[str, Path]:
        dirs = get_mod_directories(self.game_root)
        ue4ss_roots = get_ue4ss_mod_roots(self.game_root)
        palschema_roots = get_palschema_mod_roots(self.game_root)
        for path in (
            self.game_root,
            dirs["tilde_mods"],
            dirs["logic_mods"],
            *ue4ss_roots,
            *palschema_roots,
            self.data_dir,
        ):
            validate_no_reparse_ancestors(path)
        ensure_mod_folders(self.game_root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "staging").mkdir(parents=True, exist_ok=True)
        writable_roots = [dirs["tilde_mods"], dirs["logic_mods"], self.data_dir]
        writable_roots.extend(path for path in ue4ss_roots if path.is_dir())
        writable_roots.extend(path for path in palschema_roots if path.is_dir())
        for path in writable_roots:
            if not check_directory_writable(path):
                raise PermissionError(f"目录不可写：{path}")
        return dirs

    @staticmethod
    def _kind(value: str | ModKind | None, fallback: ModKind) -> ModKind:
        if value in (None, "", "auto"):
            return fallback
        kind = ModKind(value)
        if kind not in (
            ModKind.PAK, ModKind.LOGICPAK, ModKind.UE4SS, ModKind.PALSCHEMA,
        ):
            raise ValueError("仅支持 pak、logicpak、ue4ss 或 palschema")
        return kind

    @staticmethod
    def _target(dirs: dict[str, Path], kind: ModKind) -> Path:
        if kind is ModKind.UE4SS:
            return dirs["ue4ss_mods"]
        if kind is ModKind.PALSCHEMA:
            return dirs["palschema_mods"]
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
        value["externally_discovered"] = manifest.source_name == "rescan"
        if manifest.ue4ss_enabled_txt is not None:
            value["ue4ss_enabled_txt"] = asdict(manifest.ue4ss_enabled_txt)
        value["audit"] = {
            "manifest_id": audit.manifest_id,
            "status": audit.status.value,
        }
        value["file_health"] = [asdict(item) for item in audit.files]
        value["audit_issues"] = list(audit.issues)
        return value

    def _listed(self, manifest: ModManifest) -> dict[str, object]:
        audit = self._audit(manifest)
        displayed = manifest
        if manifest.kind is ModKind.UE4SS and audit.status in (AuditStatus.ENABLED, AuditStatus.DISABLED):
            displayed = replace(manifest, enabled=audit.status is AuditStatus.ENABLED)
        value = self._serialize_manifest(displayed, audit)
        capability = self.value_service.inspect_manifest(manifest)
        value["adjustable_values"] = capability is not None
        value["adjustable_value_count"] = len(capability.fields) if capability else 0
        return value

    @staticmethod
    def _mods_entry(line: str) -> tuple[str, str] | None:
        return parse_entry(line)

    @staticmethod
    def _updated_mods_bytes(data: bytes, name: str, enabled: bool | None) -> bytes:
        return remove_entry(data, name) if enabled is None else update_entry(data, name, enabled)

    def _write_mods_txt(self, path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _mods_enabled(path: Path, name: str) -> bool | None:
        return enabled_state(path.read_bytes(), name) if path.is_file() else None

    def _audit(self, manifest: ModManifest) -> ManifestAudit:
        try:
            audit = self.store.audit(manifest)
        except ValueError:
            return ManifestAudit(
                manifest.id,
                AuditStatus.CONFLICT,
                (),
                ("unsafe_or_unreadable_path",),
            )
        if manifest.kind is not ModKind.UE4SS or audit.status is not AuditStatus.ENABLED:
            return audit
        state = self._mods_enabled(manifest.install_root.parent / "mods.txt", manifest.install_root.name)
        if state is None:
            return ManifestAudit(
                manifest.id,
                AuditStatus.MISSING,
                audit.files,
                (*audit.issues, "ue4ss_mods_txt_missing"),
            )
        return ManifestAudit(
            manifest.id,
            AuditStatus.ENABLED if state else AuditStatus.DISABLED,
            audit.files,
            audit.issues,
        )

    @staticmethod
    def _expected_from_audit(item: ManifestFileAudit) -> ManifestFile:
        return ManifestFile(
            item.relative_path, item.expected_size, item.expected_sha256
        )

    def _audit_paths(
        self, manifest: ModManifest, item: ManifestFileAudit
    ) -> dict[str, Path]:
        return {
            "live": manifest.install_root / item.relative_path,
            "disabled": self.data_dir / "disabled" / manifest.id / item.relative_path,
        }

    def _capture_repair_sources(self, manifest: ModManifest) -> dict[str, object]:
        audit = self.store.audit(manifest)
        captured = 0
        unavailable = 0
        for item in audit.files:
            expected = self._expected_from_audit(item)
            paths = self._audit_paths(manifest, item)
            source = next(
                (
                    paths[location]
                    for location in ("live", "disabled")
                    if getattr(item, location).state == "healthy"
                ),
                None,
            )
            if source is None:
                unavailable += 1
                continue
            try:
                captured += int(self.repair_vault.capture(source, expected))
            except (OSError, ValueError):
                unavailable += 1
        return {
            "captured": captured,
            "unavailable": unavailable,
            "file_count": len(audit.files),
        }

    def _checkpoint_manifest(self, manifest: ModManifest) -> bool:
        try:
            self.store.checkpoint(manifest)
            return True
        except (OSError, ValueError):
            return False

    def _finalize_install(self, manifest: ModManifest) -> dict[str, object]:
        snapshot = self._capture_repair_sources(manifest)
        snapshot["manifest_checkpoint"] = self._checkpoint_manifest(manifest)
        result = self._listed(manifest)
        result["repair_snapshot"] = snapshot
        return result

    def _build_repair_plan(self, manifest: ModManifest) -> dict[str, object]:
        audit = self._audit(manifest)
        actions: list[dict[str, object]] = []
        blocked: list[dict[str, object]] = []
        for item in audit.files:
            expected = self._expected_from_audit(item)
            desired = (
                "disabled"
                if item.role == "ue4ss_metadata" or not manifest.enabled
                else "live"
            )
            other = "live" if desired == "disabled" else "disabled"
            desired_state = getattr(item, desired).state
            other_state = getattr(item, other).state
            vault_available = self.repair_vault.available(expected)
            common = {
                "relative_path": item.relative_path,
                "role": item.role,
                "target": desired,
                "source": other,
                "desired_state": desired_state,
                "other_state": other_state,
                "vault_available": vault_available,
            }

            if desired_state == "healthy":
                if other_state == "missing":
                    continue
                if other_state == "conflict":
                    blocked.append({**common, "reason": "path_conflict"})
                else:
                    actions.append({
                        **common,
                        "kind": "quarantine_other",
                        "requires_confirmation": False,
                    })
                continue

            if desired_state == "missing":
                if other_state == "healthy":
                    actions.append({
                        **common,
                        "kind": "move_verified",
                        "requires_confirmation": False,
                    })
                elif other_state == "conflict":
                    blocked.append({**common, "reason": "path_conflict"})
                elif vault_available and other_state == "missing":
                    actions.append({
                        **common,
                        "kind": "restore_from_vault",
                        "source": "vault",
                        "requires_confirmation": False,
                    })
                elif vault_available:
                    actions.append({
                        **common,
                        "kind": "quarantine_other_and_restore",
                        "requires_confirmation": True,
                    })
                else:
                    blocked.append({**common, "reason": "trusted_source_missing"})
                continue

            if desired_state == "modified":
                if other_state == "conflict":
                    blocked.append({**common, "reason": "path_conflict"})
                elif other_state == "healthy":
                    actions.append({
                        **common,
                        "kind": "quarantine_desired_and_move",
                        "requires_confirmation": True,
                    })
                elif vault_available:
                    actions.append({
                        **common,
                        "kind": (
                            "quarantine_desired_and_restore"
                            if other_state == "missing"
                            else "quarantine_both_and_restore"
                        ),
                        "requires_confirmation": True,
                    })
                else:
                    blocked.append({**common, "reason": "trusted_source_missing"})
                continue

            blocked.append({**common, "reason": "path_conflict"})

        if "ue4ss_mods_txt_missing" in audit.issues:
            actions.append({
                "kind": "restore_ue4ss_entry",
                "relative_path": "mods.txt",
                "role": "ue4ss_config",
                "target": "live",
                "source": "manifest",
                "desired_state": "missing",
                "other_state": "missing",
                "vault_available": False,
                "requires_confirmation": False,
            })
        if "unsafe_or_unreadable_path" in audit.issues:
            blocked.append({
                "relative_path": "",
                "role": "manifest",
                "target": "unknown",
                "source": "unknown",
                "desired_state": "conflict",
                "other_state": "conflict",
                "vault_available": False,
                "reason": "unsafe_or_unreadable_path",
            })

        revision_payload = {
            "manifest": ManifestStore._to_dict(manifest),
            "audit": asdict(audit),
            "actions": actions,
            "blocked": blocked,
        }
        revision = "sha256:" + hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        safe_actions = sum(
            action["requires_confirmation"] is False for action in actions
        )
        confirmation_actions = len(actions) - safe_actions
        return {
            "manifest_id": manifest.id,
            "name": manifest.name,
            "status": audit.status.value,
            "revision": revision,
            "actions": actions,
            "blocked": blocked,
            "safe_actions": safe_actions,
            "confirmation_actions": confirmation_actions,
            "repairable": bool(actions),
            "complete_possible": not blocked,
            "issues": list(audit.issues),
        }

    def repair_plan(self, manifest_id: str) -> dict[str, object]:
        with self._transaction_lock():
            return self._build_repair_plan(self.store.get(manifest_id))

    def _execute_repair_plan(
        self,
        manifest: ModManifest,
        plan: dict[str, object],
        *,
        confirm_replace: bool,
        safe_only: bool = False,
    ) -> dict[str, object]:
        actions = [
            action
            for action in plan["actions"]
            if not safe_only or action["requires_confirmation"] is False
        ]
        if (
            not safe_only
            and any(action["requires_confirmation"] is True for action in actions)
            and not confirm_replace
        ):
            raise RepairConfirmationRequired(plan)
        if not actions:
            result = self._listed(manifest)
            return {
                "ok": True,
                "manifest_id": manifest.id,
                "executed": [],
                "executed_count": 0,
                "remaining_blocked": plan["blocked"],
                "complete": result["status"] in {"enabled", "disabled"},
                "quarantine_path": None,
                "mod": result,
            }

        transaction_id = uuid.uuid4().hex
        quarantine_root = (
            self.data_dir / "repair-quarantine" / "files" / transaction_id
        )
        audit = self.store.audit(manifest)
        expected_by_key = {
            (item.role, item.relative_path.casefold()): self._expected_from_audit(item)
            for item in audit.files
        }
        moved: list[tuple[Path, Path]] = []
        created: list[Path] = []
        config_backups: dict[Path, bytes | None] = {}

        def item_paths(action: dict[str, object]) -> tuple[ManifestFile, dict[str, Path]]:
            key = (str(action["role"]), str(action["relative_path"]).casefold())
            expected = expected_by_key[key]
            detail = next(
                item
                for item in audit.files
                if (item.role, item.relative_path.casefold()) == key
            )
            return expected, self._audit_paths(manifest, detail)

        def move(source: Path, destination: Path, expected: ManifestFile) -> None:
            _move_verified(source, destination, expected.sha256)
            moved.append((destination, source))

        def quarantine(
            source: Path, location: str, expected: ManifestFile
        ) -> Path:
            destination = (
                quarantine_root
                / manifest.id
                / location
                / expected.relative_path
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _move_verified(source, destination)
            moved.append((destination, source))
            return destination

        def restore(expected: ManifestFile, destination: Path) -> None:
            self.repair_vault.restore(expected, destination)
            created.append(destination)

        executed: list[dict[str, object]] = []
        try:
            for action in actions:
                kind = str(action["kind"])
                if kind == "restore_ue4ss_entry":
                    mods_txt = manifest.install_root.parent / "mods.txt"
                    if mods_txt not in config_backups:
                        config_backups[mods_txt] = (
                            mods_txt.read_bytes() if mods_txt.is_file() else None
                        )
                    current = config_backups[mods_txt] or b""
                    self._write_mods_txt(
                        mods_txt,
                        self._updated_mods_bytes(
                            current, manifest.install_root.name, manifest.enabled
                        ),
                    )
                    executed.append(action)
                    continue

                expected, paths = item_paths(action)
                target = str(action["target"])
                source = str(action["source"])
                target_path = paths[target]
                source_path = paths[source] if source in paths else None
                if kind == "move_verified":
                    move(source_path, target_path, expected)
                elif kind == "restore_from_vault":
                    restore(expected, target_path)
                elif kind == "quarantine_other":
                    quarantine(source_path, source, expected)
                elif kind == "quarantine_desired_and_move":
                    quarantine(target_path, target, expected)
                    move(source_path, target_path, expected)
                elif kind == "quarantine_desired_and_restore":
                    quarantine(target_path, target, expected)
                    restore(expected, target_path)
                elif kind == "quarantine_other_and_restore":
                    quarantine(source_path, source, expected)
                    restore(expected, target_path)
                elif kind == "quarantine_both_and_restore":
                    quarantine(target_path, target, expected)
                    quarantine(source_path, source, expected)
                    restore(expected, target_path)
                else:
                    raise ValueError("unknown repair action")
                executed.append(action)
        except BaseException:
            for path, before in config_backups.items():
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    self._write_mods_txt(path, before)
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for current, original in reversed(moved):
                original.parent.mkdir(parents=True, exist_ok=True)
                if current.exists() and not os.path.lexists(original):
                    _move_verified(current, original)
            shutil.rmtree(quarantine_root, ignore_errors=True)
            raise

        result = self._listed(manifest)
        return {
            "ok": True,
            "manifest_id": manifest.id,
            "executed": executed,
            "executed_count": len(executed),
            "remaining_blocked": plan["blocked"],
            "complete": result["status"] in {"enabled", "disabled"},
            "quarantine_path": (
                str(quarantine_root) if quarantine_root.is_dir() else None
            ),
            "mod": result,
        }

    @_locked_write
    def repair_mod(
        self,
        manifest_id: str,
        revision: str,
        *,
        confirm_replace: bool = False,
    ) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        plan = self._build_repair_plan(manifest)
        if revision != plan["revision"]:
            raise RepairPlanStale(plan)
        return self._execute_repair_plan(
            manifest, plan, confirm_replace=confirm_replace
        )

    def health_report(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        healthy = abnormal = safe_actions = confirmation_actions = blocked = 0
        for manifest in self.store.list():
            plan = self._build_repair_plan(manifest)
            if plan["status"] in {"enabled", "disabled"}:
                healthy += 1
            else:
                abnormal += 1
            safe_actions += int(plan["safe_actions"])
            confirmation_actions += int(plan["confirmation_actions"])
            blocked += len(plan["blocked"])
            items.append({
                "id": manifest.id,
                "name": manifest.name,
                "kind": manifest.kind.value,
                "status": plan["status"],
                "safe_actions": plan["safe_actions"],
                "confirmation_actions": plan["confirmation_actions"],
                "blocked": len(plan["blocked"]),
                "repairable": plan["repairable"],
            })
        invalid = self.store.invalid_records()
        return {
            "status": "healthy" if not abnormal and not invalid else "degraded",
            "summary": {
                "managed_mods": len(items),
                "healthy_mods": healthy,
                "abnormal_mods": abnormal,
                "invalid_manifests": len(invalid),
                "safe_actions": safe_actions,
                "confirmation_actions": confirmation_actions,
                "blocked_files": blocked,
            },
            "mods": items,
            "invalid_manifests": invalid,
        }

    @_locked_write
    def repair_all_safe(self) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        restored_manifests: list[str] = []
        for item in self.store.invalid_records():
            if item["backup_available"] is not True:
                continue
            try:
                if self.store.restore_invalid_from_backup(str(item["id"])):
                    restored_manifests.append(str(item["id"]))
            except (OSError, TypeError, ValueError):
                continue

        captured = 0
        repaired: list[dict[str, object]] = []
        failed: list[str] = []
        for manifest in self.store.list():
            try:
                captured += int(self._capture_repair_sources(manifest)["captured"])
            except (OSError, ValueError):
                pass
            self._checkpoint_manifest(manifest)
            plan = self._build_repair_plan(manifest)
            if not plan["safe_actions"]:
                continue
            try:
                result = self._execute_repair_plan(
                    manifest,
                    plan,
                    confirm_replace=False,
                    safe_only=True,
                )
                repaired.append({
                    "id": manifest.id,
                    "executed_count": result["executed_count"],
                    "complete": result["complete"],
                })
            except (OSError, RuntimeError, ValueError):
                failed.append(manifest.id)
        return {
            "ok": not failed,
            "restored_manifests": restored_manifests,
            "captured_repair_objects": captured,
            "repaired": repaired,
            "failed": failed,
            "report": self.health_report(),
        }

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
        entry_points = {
            path.relative_to(source_root).as_posix().casefold() for path in payload
        }
        if not {"scripts/main.lua", "dlls/main.dll"} & entry_points:
            raise ValueError("UE4SS 模组必须包含 Scripts/main.lua 或 dlls/main.dll")
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
            current = old_config if old_config is not None else b""
            self._write_mods_txt(mods_txt, self._updated_mods_bytes(current, folder_name, True))
            return self._finalize_install(created)
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

    def _palschema_target_root(self) -> Path:
        for root in get_palschema_mod_roots(self.game_root):
            framework = root.parent
            marker = framework / "dlls" / "main.dll"
            if framework.is_dir() and marker.is_file() and not _is_reparse(marker):
                return root
        raise ValueError("未检测到 PalSchema，请先安装并启用 PalSchema 框架")

    def _install_palschema(
        self,
        source_path: Path,
        inspected,
        display_name: str | None,
        nexus_id: int | None,
    ) -> dict[str, object]:
        source_root = inspected.content_root
        folder_name = _safe_label(display_name or inspected.display_name)
        palschema_root = self._palschema_target_root()
        validate_no_reparse_ancestors(palschema_root)
        existing = next(
            (
                child for child in palschema_root.iterdir()
                if child.name.casefold() == folder_name.casefold()
            ),
            None,
        ) if palschema_root.is_dir() else None
        if existing is not None:
            raise ModConflictError({
                "files": [str(existing)],
                "choices": ["cancel"],
            })
        files = [
            path for path in source_root.rglob("*")
            if path.is_file() and not _is_reparse(path)
        ]
        if not files:
            raise ValueError("PalSchema 模组不包含可安装文件")
        payload_bytes = sum(path.stat().st_size for path in files)
        if (
            shutil.disk_usage(palschema_root.parent)[2] < payload_bytes
            or shutil.disk_usage(self.data_dir)[2] < payload_bytes
        ):
            raise OSError("磁盘空间不足，无法安装模组")

        target_root = palschema_root / folder_name
        created: ModManifest | None = None
        try:
            target_root.mkdir(parents=True)
            installed: list[Path] = []
            for incoming in files:
                destination = target_root / incoming.relative_to(source_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(incoming, destination)
                installed.append(destination)
            created = self.store.create(
                name=display_name or inspected.display_name,
                kind=ModKind.PALSCHEMA,
                install_root=target_root,
                files=installed,
                source_name=source_path.name,
                nexus_id=nexus_id,
            )
            return self._finalize_install(created)
        except BaseException:
            if created is not None:
                self.store.delete(created.id)
            shutil.rmtree(target_root, ignore_errors=True)
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
                if kind is ModKind.PALSCHEMA:
                    if inspected.kind is not ModKind.PALSCHEMA:
                        raise ValueError("所选压缩包不是 PalSchema 内容模组")
                    return self._install_palschema(
                        source_path, inspected, display_name, nexus_id,
                    )
                if inspected.kind not in (ModKind.PAK, ModKind.LOGICPAK):
                    raise ValueError("此服务仅支持 PAK、LogicMods、UE4SS 与 PalSchema")
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
                        return self._finalize_install(owner)
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
                                _move_verified(live, backup)
                                backups.append((backup, live))
                    owned_paths = {original.resolve() for _, original in backups}
                    for _, destination in conflicts:
                        if destination.is_file() and destination.resolve() not in owned_paths:
                            backup = backup_root / "unmanaged" / destination.name
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            _move_verified(destination, backup)
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
                return self._finalize_install(merged)
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
            return self._finalize_install(created)
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
                    _move_verified(backup, original)
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
                current = old_config if old_config is not None else b""
                self._write_mods_txt(
                    mods_txt,
                    self._updated_mods_bytes(current, manifest.install_root.name, enabled),
                )
                self.store.save(changed)
                result = self._listed(changed)
                self._checkpoint_manifest(changed)
                return result
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
                    _move_verified(source, destination, item.sha256)
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
                    _move_verified(source, destination, item.sha256)
                    moves.append((destination, source))
            self.store.save(changed)
            result = self._listed(changed)
            self._checkpoint_manifest(changed)
        except BaseException:
            for current, original in reversed(moves):
                original.parent.mkdir(parents=True, exist_ok=True)
                if current.exists():
                    _move_verified(current, original)
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

    def _modified_paths(self, manifest: ModManifest, status: AuditStatus) -> list[Path]:
        disabled_root = self.data_dir / "disabled" / manifest.id
        changed: list[Path] = []
        existing: list[Path] = []
        for expected in manifest.files:
            candidates = (
                manifest.install_root / expected.relative_path,
                disabled_root / expected.relative_path,
            )
            present = [path for path in candidates if path.is_file() and not _is_reparse(path)]
            existing.extend(present)
            if status is AuditStatus.CONFLICT and len(present) > 1:
                changed.extend(present)
                continue
            for path in present:
                if path.stat().st_size != expected.size or _sha256(path) != expected.sha256:
                    changed.append(path)
        if manifest.ue4ss_enabled_txt is not None:
            expected = manifest.ue4ss_enabled_txt
            metadata = disabled_root / expected.relative_path
            if metadata.is_file() and not _is_reparse(metadata):
                existing.append(metadata)
                if metadata.stat().st_size != expected.size or _sha256(metadata) != expected.sha256:
                    changed.append(metadata)
        selected = changed or existing
        return sorted(set(selected), key=lambda path: str(path).casefold())

    @_locked_write
    def delete(self, manifest_id: str, force_modified: bool = False) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        audit = self._audit(manifest)
        if audit.status in (AuditStatus.MODIFIED, AuditStatus.CONFLICT) and not force_modified:
            raise ModifiedFilesError(self._modified_paths(manifest, audit.status))
        record = self.trash_service.recycle_local_mod(manifest, audit, self.store)
        return {
            "ok": True,
            "trash_id": record.id,
            "expires_at": record.expires_at,
            "files_moved": len(record.files),
        }

    @_locked_write
    def restore_trash(self, trash_id: str) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.trash_service.restore_local_mod(trash_id, self.store)
        return self._finalize_install(manifest)

    def list_trash(self) -> dict[str, object]:
        return self.trash_service.list_records()

    @_locked_write
    def purge_trash(self, trash_id: str) -> dict[str, object]:
        return self.trash_service.purge(trash_id)

    @_locked_write
    def purge_expired_trash(self) -> dict[str, object]:
        return self.trash_service.purge_expired()

    def _ignored_identity(self, manifest: ModManifest) -> IgnoredIdentity:
        if manifest.kind is ModKind.PAK:
            key = next(
                (item.relative_path for item in manifest.files if Path(item.relative_path).suffix.casefold() == ".pak"),
                manifest.files[0].relative_path,
            )
            return IgnoredIdentity("pak", "tilde_mods", Path(key).name)
        if manifest.kind is ModKind.LOGICPAK:
            key = next(
                (item.relative_path for item in manifest.files if Path(item.relative_path).suffix.casefold() == ".pak"),
                manifest.files[0].relative_path,
            )
            return IgnoredIdentity("logicpak", "logic_mods", Path(key).name)
        if manifest.kind is ModKind.PALSCHEMA:
            parent = Path(os.path.abspath(manifest.install_root.parent))
            roots = self.trash_service.original_roots
            if parent == roots["palschema_classic"]:
                root = "palschema_classic"
            elif parent == roots["palschema_nested"]:
                root = "palschema_nested"
            else:
                raise ValueError("PalSchema manifest is outside supported roots")
            return IgnoredIdentity("palschema", root, manifest.install_root.name)
        parent = Path(os.path.abspath(manifest.install_root.parent))
        roots = self.trash_service.original_roots
        if parent == roots["ue4ss_classic"]:
            root = "ue4ss_classic"
        elif parent == roots["ue4ss_nested"]:
            root = "ue4ss_nested"
        else:
            raise ValueError("UE4SS manifest is outside supported roots")
        return IgnoredIdentity("ue4ss", root, manifest.install_root.name)

    def _is_ignored(self, identity: IgnoredIdentity) -> bool:
        return self.ignored_store.contains(
            self.trash_service.game_fingerprint, identity
        )

    @staticmethod
    def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
        validate_no_reparse_ancestors(source)
        validate_no_reparse_ancestors(destination)
        if _is_reparse(source) or not source.is_file():
            raise ValueError("UE4SS enabled metadata is missing or unsafe")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if _sha256(temporary) != expected_sha256:
                raise ValueError("UE4SS enabled metadata checksum mismatch")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @_locked_write
    def unmanage(self, manifest_id: str) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        if manifest.source_name != "rescan":
            raise NotExternalModError("只有外部发现模组可以取消管理")
        audit = self._audit(manifest)
        if audit.status in (AuditStatus.MISSING, AuditStatus.CONFLICT):
            raise RuntimeError(f"模组文件状态异常：{audit.status.value}")
        identity = self._ignored_identity(manifest)
        fingerprint = self.trash_service.game_fingerprint
        metadata_source: Path | None = None
        marker_destination: Path | None = None
        restored_marker = False
        self.ignored_store.add(fingerprint, identity)
        try:
            if manifest.kind is ModKind.UE4SS and manifest.ue4ss_enabled_txt is not None:
                metadata_source = (
                    self.data_dir
                    / "disabled"
                    / manifest.id
                    / manifest.ue4ss_enabled_txt.relative_path
                )
                marker_destination = manifest.install_root / "enabled.txt"
                if os.path.lexists(marker_destination):
                    raise ModConflictError(
                        {"files": [str(marker_destination)], "choices": ["cancel"]}
                    )
                self._copy_verified(
                    metadata_source,
                    marker_destination,
                    manifest.ue4ss_enabled_txt.sha256,
                )
                restored_marker = True
                metadata_source.unlink()
            self.store.delete(manifest.id)
        except BaseException:
            try:
                self.store.get(manifest.id)
            except KeyError:
                self.store.save(manifest)
            if restored_marker and metadata_source is not None and marker_destination is not None:
                self._copy_verified(
                    marker_destination,
                    metadata_source,
                    manifest.ue4ss_enabled_txt.sha256,
                )
                marker_destination.unlink(missing_ok=True)
            self.ignored_store.remove(fingerprint, identity)
            raise
        if metadata_source is not None:
            self._remove_empty_parents(metadata_source, self.data_dir / "disabled")
        return {"ok": True, "unmanaged": manifest.id}

    def ignored_summary(self) -> dict[str, object]:
        entries = self.ignored_store.list(self.trash_service.game_fingerprint)
        return {
            "count": len(entries),
            "items": [
                {"kind": item.kind, "root": item.root, "key": item.key}
                for item in entries
            ],
        }

    @_locked_write
    def reset_ignored_and_rescan(self) -> list[dict[str, object]]:
        self._assert_stopped()
        self._prepare_dirs()
        fingerprint = self.trash_service.game_fingerprint
        previous = self.ignored_store.list(fingerprint)
        existing_ids = {manifest.id for manifest in self.store.list()}
        self.ignored_store.reset(fingerprint)
        try:
            return self._rescan_locked()
        except BaseException:
            for manifest in self.store.list():
                if manifest.id not in existing_ids and manifest.source_name == "rescan":
                    self.store.delete(manifest.id)
            self.ignored_store.replace(fingerprint, previous)
            raise

    def _discovery_marker(self) -> Path:
        identity = (
            b"discovery-v2\0"
            + str(self.game_root.resolve(strict=False)).casefold().encode("utf-8")
        )
        return self.data_dir / "discovery" / f"{hashlib.sha256(identity).hexdigest()}.done"

    def discover_existing_once(self) -> list[dict[str, object]]:
        """Adopt pre-existing mods once per game path, deferring while the game runs."""
        marker = self._discovery_marker()
        if marker.is_file():
            return self.list_mods()
        try:
            with self._transaction_lock():
                if marker.is_file():
                    return self.list_mods()
                result = self._rescan_locked()
                marker.parent.mkdir(parents=True, exist_ok=True)
                temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_text("1\n", encoding="ascii")
                    os.replace(temporary, marker)
                finally:
                    temporary.unlink(missing_ok=True)
                return result
        except (GameRunningError, TimeoutError):
            # An aborted or overlapping UI refresh can leave the first discovery
            # request finishing in the server. Return the last consistent snapshot
            # instead of surfacing a generic 500 while that request owns the lock.
            return self.list_mods()

    @_locked_write
    def rescan(self) -> list[dict[str, object]]:
        return self._rescan_locked()

    def _rescan_locked(self) -> list[dict[str, object]]:
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
            paks = (path for path in root.iterdir() if path.suffix.casefold() == ".pak")
            for pak in sorted(paks, key=lambda path: path.name.casefold()):
                if _is_reparse(pak) or not pak.is_file():
                    continue
                key = (str(root.resolve()).casefold(), pak.name.casefold())
                root_key = "logic_mods" if kind is ModKind.LOGICPAK else "tilde_mods"
                ignored = IgnoredIdentity(kind.value, root_key, pak.name)
                if key in tracked or self._is_ignored(ignored):
                    continue
                group = [pak]
                for suffix in (".utoc", ".ucas"):
                    sidecar = pak.with_suffix(suffix)
                    if sidecar.is_file() and not _is_reparse(sidecar):
                        group.append(sidecar)
                manifest = self.store.create(pak.stem, kind, root, group, source_name="rescan")
                tracked.update((str(root.resolve()).casefold(), item.relative_path.casefold()) for item in manifest.files)

        tracked_roots = {
            str(manifest.install_root.resolve()).casefold()
            for manifest in self.store.list()
            if manifest.kind is ModKind.UE4SS
        }
        for ue4ss_root in get_ue4ss_mod_roots(self.game_root):
            if not ue4ss_root.is_dir() or _is_reparse(ue4ss_root):
                continue
            for candidate in sorted(ue4ss_root.iterdir(), key=lambda path: path.name.casefold()):
                if (
                    not candidate.is_dir()
                    or _is_reparse(candidate)
                    or is_ue4ss_framework_mod(candidate.name)
                    or not is_ue4ss_mod_folder(candidate)
                    or str(candidate.resolve()).casefold() in tracked_roots
                ):
                    continue
                root_key = (
                    "ue4ss_nested"
                    if Path(os.path.abspath(ue4ss_root)) == self.trash_service.original_roots["ue4ss_nested"]
                    else "ue4ss_classic"
                )
                if self._is_ignored(IgnoredIdentity("ue4ss", root_key, candidate.name)):
                    continue
                files = [
                    path for path in candidate.rglob("*")
                    if path.is_file()
                    and not _is_reparse(path)
                    and not (
                        candidate.name.casefold() == "palschema"
                        and path.relative_to(candidate).parts
                        and path.relative_to(candidate).parts[0].casefold() == "mods"
                    )
                ]
                enabled_txt = next((path for path in files if path.relative_to(candidate).as_posix().casefold() == "enabled.txt"), None)
                payload = [path for path in files if path != enabled_txt]
                mods_txt = ue4ss_root / "mods.txt"
                enabled = self._mods_enabled(mods_txt, candidate.name)
                old_config = mods_txt.read_bytes() if mods_txt.is_file() else None
                manifest: ModManifest | None = None
                metadata: Path | None = None
                try:
                    if enabled is None:
                        enabled = enabled_txt is not None
                        current = old_config if old_config is not None else b""
                        self._write_mods_txt(
                            mods_txt,
                            self._updated_mods_bytes(current, candidate.name, enabled),
                        )
                    manifest = self.store.create(
                        candidate.name, ModKind.UE4SS, candidate, payload,
                        source_name="rescan", enabled=enabled,
                    )
                    if enabled_txt is not None:
                        metadata = self.data_dir / "disabled" / manifest.id / "metadata" / "enabled.txt"
                        metadata.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_verified(enabled_txt, metadata, _sha256(enabled_txt))
                        enabled_txt.unlink()
                        manifest = replace(
                            manifest,
                            ue4ss_enabled_txt=ManifestFile(
                                "metadata/enabled.txt", metadata.stat().st_size, _sha256(metadata)
                            ),
                        )
                        self.store.save(manifest)
                except BaseException:
                    if metadata is not None and metadata.exists():
                        enabled_txt.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_verified(metadata, enabled_txt, _sha256(metadata))
                        metadata.unlink()
                    if manifest is not None:
                        self.store.delete(manifest.id)
                    if metadata is not None:
                        shutil.rmtree(metadata.parent.parent, ignore_errors=True)
                    if old_config is None:
                        mods_txt.unlink(missing_ok=True)
                    else:
                        mods_txt.write_bytes(old_config)
                    raise
                tracked_roots.add(str(candidate.resolve()).casefold())

        tracked_palschema_roots = {
            str(manifest.install_root.resolve()).casefold()
            for manifest in self.store.list()
            if manifest.kind is ModKind.PALSCHEMA
        }
        for palschema_root in get_palschema_mod_roots(self.game_root):
            if not palschema_root.is_dir() or _is_reparse(palschema_root):
                continue
            root_key = (
                "palschema_nested"
                if Path(os.path.abspath(palschema_root))
                == self.trash_service.original_roots["palschema_nested"]
                else "palschema_classic"
            )
            for candidate in sorted(
                palschema_root.iterdir(), key=lambda path: path.name.casefold()
            ):
                resolved_key = str(candidate.resolve()).casefold()
                if (
                    not candidate.is_dir()
                    or _is_reparse(candidate)
                    or resolved_key in tracked_palschema_roots
                    or self._is_ignored(
                        IgnoredIdentity("palschema", root_key, candidate.name)
                    )
                ):
                    continue
                files = [
                    path for path in candidate.rglob("*")
                    if path.is_file() and not _is_reparse(path)
                ]
                if not files:
                    continue
                self.store.create(
                    candidate.name,
                    ModKind.PALSCHEMA,
                    candidate,
                    files,
                    source_name="rescan",
                    enabled=True,
                )
                tracked_palschema_roots.add(resolved_key)
        return self.list_mods()

    def get_mod_values(self, manifest_id: str) -> dict[str, object]:
        with self._transaction_lock():
            manifest = self.store.get(manifest_id)
            return self.value_service.read_values(manifest)

    def _assert_value_edit_files_safe(
        self, manifest: ModManifest, config_relative_path: str
    ) -> None:
        config_key = _relative_path_key(config_relative_path)
        conflicts: list[str] = []
        for item in manifest.files:
            if _relative_path_key(item.relative_path) == config_key:
                continue
            path = validate_no_reparse_ancestors(
                manifest.install_root / item.relative_path
            )
            if (
                not path.is_file()
                or _is_reparse(path)
                or path.stat().st_size != item.size
                or _sha256(path) != item.sha256
            ):
                conflicts.append(item.relative_path)
        if conflicts:
            raise ModValueConflict(
                "Mod 的非配置文件存在异常，已拒绝调整数值",
                {"files": conflicts[:20], "file_count": len(conflicts)},
            )

    @_locked_write
    def update_mod_values(
        self,
        manifest_id: str,
        values: dict[str, object],
        revision: str,
    ) -> dict[str, object]:
        self._assert_stopped()
        self._prepare_dirs()
        manifest = self.store.get(manifest_id)
        capability = self.value_service.inspect_manifest(manifest)
        if capability is None:
            return self.value_service.read_values(manifest)
        self._assert_value_edit_files_safe(
            manifest, capability.config_relative_path
        )
        manifest_path = self.store.manifests_dir / f"{manifest.id}.json"
        manifest_before = manifest_path.read_bytes()
        try:
            changed, result = self.value_service.update_values(
                manifest, values, revision, self.store.save
            )
            self._capture_repair_sources(changed)
            self._checkpoint_manifest(changed)
            return result
        except BaseException:
            try:
                if manifest_path.read_bytes() != manifest_before:
                    rollback = manifest_path.with_name(
                        f".{manifest_path.name}.{uuid.uuid4().hex}.rollback.tmp"
                    )
                    rollback.write_bytes(manifest_before)
                    os.replace(rollback, manifest_path)
            except OSError as rollback_error:
                raise ModValueConflict(
                    "Mod 清单回滚失败",
                    {"rollback_error": type(rollback_error).__name__},
                ) from rollback_error
            raise

    def folder_for(self, mod_id: str | None = None) -> Path:
        """Return only a configured mod root or a managed manifest's install root."""
        directories = get_mod_directories(self.game_root)
        allowed_roots = tuple(
            validate_no_reparse_ancestors(path).resolve(strict=False)
            for path in (
                directories["tilde_mods"], directories["logic_mods"],
                *get_ue4ss_mod_roots(self.game_root),
            )
        )
        candidate = (
            self.store.get(mod_id).install_root
            if mod_id
            else directories["tilde_mods"]
        )
        resolved = validate_no_reparse_ancestors(candidate).resolve(strict=False)
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
            raise PermissionError("模组目录超出受管范围")
        return resolved

    def list_mods(self) -> list[dict[str, object]]:
        return [self._listed(manifest) for manifest in self.store.list()]
