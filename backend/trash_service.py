"""Same-volume payload primitives for PalDeck's recoverable trash."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path, PureWindowsPath
from typing import Callable

from .domain import AuditStatus, ManifestAudit, ModKind, ModManifest
from .game_detector import get_mod_directories
from .manifest_store import (
    ManifestStore,
    _is_reparse,
    _relative_path_key,
    validate_no_reparse_ancestors,
)
from .process_utils import is_palworld_running
from .trash_store import TrashFile, TrashRecord, TrashStore
from .ue4ss_config import remove_entry, update_entry


class TrashPayloadError(RuntimeError):
    def __init__(self, message: str, details: dict[str, object] | None = None):
        self.details = details or {}
        super().__init__(message)


class TrashPayloadMissing(TrashPayloadError):
    pass


class TrashPayloadModified(TrashPayloadError):
    pass


class TrashPayloadConflict(TrashPayloadError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_from_relative(value: str) -> Path:
    _relative_path_key(value)
    return Path(*PureWindowsPath(value).parts)


class TrashService:
    def __init__(
        self,
        game_root: str | os.PathLike[str],
        data_dir: str | os.PathLike[str],
        *,
        game_running: Callable[[], bool] = is_palworld_running,
        store: TrashStore | None = None,
    ) -> None:
        self.game_root = Path(os.path.abspath(game_root))
        self.data_dir = Path(os.path.abspath(data_dir))
        self.game_running = game_running
        self.store = store or TrashStore(self.data_dir / "trash" / "records")
        dirs = get_mod_directories(self.game_root)
        win64 = dirs["win64"]
        self.original_roots: dict[str, Path] = {
            "tilde_mods": Path(os.path.abspath(dirs["tilde_mods"])),
            "logic_mods": Path(os.path.abspath(dirs["logic_mods"])),
            "ue4ss_classic": Path(os.path.abspath(win64 / "Mods")),
            "ue4ss_nested": Path(os.path.abspath(win64 / "ue4ss" / "Mods")),
            "disabled": Path(os.path.abspath(self.data_dir / "disabled")),
            "framework_win64": Path(os.path.abspath(win64)),
        }

    @staticmethod
    def _trash_id(value: str) -> str:
        try:
            return uuid.UUID(value).hex
        except (ValueError, AttributeError) as exc:
            raise ValueError("trash id must be a UUID") from exc

    @property
    def game_payload_root(self) -> Path:
        return self.game_root / ".paldeck" / "trash"

    @property
    def data_payload_root(self) -> Path:
        return self.data_dir / "trash" / "payload"

    def payload_root_for(self, source: Path, trash_id: str) -> Path:
        normalized_id = self._trash_id(trash_id)
        candidate = Path(os.path.abspath(source))
        if candidate == self.data_dir or candidate.is_relative_to(self.data_dir):
            return self.data_payload_root / normalized_id
        if candidate == self.game_root or candidate.is_relative_to(self.game_root):
            return self.game_payload_root / normalized_id
        raise ValueError("trash source is outside trusted roots")

    def resolve_original(self, original_root: str, relative_path: str) -> Path:
        root = self.original_roots.get(original_root)
        if root is None:
            raise ValueError("trash original root is invalid")
        relative = _path_from_relative(relative_path)
        candidate = validate_no_reparse_ancestors(root / relative)
        trusted = validate_no_reparse_ancestors(root)
        if not candidate.is_relative_to(trusted):
            raise ValueError("trash original path escaped its root")
        return candidate

    def _payload_base(self, payload_root: str, trash_id: str) -> Path:
        normalized_id = self._trash_id(trash_id)
        if payload_root == "game":
            root = self.game_payload_root / normalized_id
        elif payload_root == "data":
            root = self.data_payload_root / normalized_id
        else:
            raise ValueError("trash payload root is invalid")
        return validate_no_reparse_ancestors(root)

    def resolve_payload(self, trash_id: str, item: TrashFile) -> Path:
        base = self._payload_base(item.payload_root, trash_id)
        candidate = validate_no_reparse_ancestors(
            base / _path_from_relative(item.payload_path)
        )
        if not candidate.is_relative_to(base):
            raise ValueError("trash payload path escaped its root")
        return candidate

    def _same_volume(self, source: Path, destination_parent: Path) -> bool:
        try:
            return source.stat().st_dev == destination_parent.stat().st_dev
        except OSError:
            source_drive = os.path.splitdrive(str(source))[0].casefold()
            target_drive = os.path.splitdrive(str(destination_parent))[0].casefold()
            return bool(source_drive and source_drive == target_drive)

    @staticmethod
    def _ensure_parent(path: Path) -> None:
        validate_no_reparse_ancestors(path)
        path.mkdir(parents=True, exist_ok=True)
        validate_no_reparse_ancestors(path)

    def move_to_payload(
        self,
        trash_id: str,
        original_root: str,
        relative_path: str,
    ) -> TrashFile:
        source = self.resolve_original(original_root, relative_path)
        if not source.is_file():
            raise TrashPayloadMissing("trash source file is missing", {"files": [str(source)]})
        if _is_reparse(source):
            raise ValueError(f"reparse source is not allowed: {source}")
        metadata = source.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"trash source is not a regular file: {source}")
        digest = _sha256(source)
        base = self.payload_root_for(source, trash_id)
        payload_root = "data" if base.is_relative_to(self.data_payload_root) else "game"
        normalized_relative = PureWindowsPath(relative_path).as_posix()
        payload_path = f"{original_root}/{normalized_relative}"
        provisional = TrashFile(
            original_root=original_root,
            relative_path=normalized_relative,
            payload_root=payload_root,
            payload_path=payload_path,
            size=metadata.st_size,
            sha256=digest,
        )
        destination = self.resolve_payload(trash_id, provisional)
        if os.path.lexists(destination):
            raise TrashPayloadConflict(
                "trash payload already exists", {"files": [str(destination)]}
            )
        self._ensure_parent(destination.parent)
        if not self._same_volume(source, destination.parent):
            raise OSError("trash payload must be on the same volume as its source")
        os.replace(source, destination)
        try:
            self.verify_payload(trash_id, provisional)
        except BaseException:
            source.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not os.path.lexists(source):
                os.replace(destination, source)
            raise
        return provisional

    def verify_payload(self, trash_id: str, item: TrashFile) -> Path:
        payload = self.resolve_payload(trash_id, item)
        if not payload.is_file() or _is_reparse(payload):
            raise TrashPayloadMissing(
                "trash payload is missing", {"files": [str(payload)]}
            )
        metadata = payload.stat()
        if metadata.st_size != item.size or _sha256(payload) != item.sha256:
            raise TrashPayloadModified(
                "trash payload was modified", {"files": [str(payload)]}
            )
        return payload

    def restore_file(self, trash_id: str, item: TrashFile) -> tuple[Path, Path]:
        payload = self.verify_payload(trash_id, item)
        destination = self.resolve_original(item.original_root, item.relative_path)
        if os.path.lexists(destination):
            raise TrashPayloadConflict(
                "trash restore target already exists", {"files": [str(destination)]}
            )
        self._ensure_parent(destination.parent)
        if not self._same_volume(payload, destination.parent):
            raise OSError("trash restore target must be on the same volume as its payload")
        os.replace(payload, destination)
        return destination, payload

    @property
    def game_fingerprint(self) -> str:
        identity = str(self.game_root.resolve(strict=False)).casefold().encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        validate_no_reparse_ancestors(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _live_identity(self, manifest: ModManifest, relative_path: str) -> tuple[str, str]:
        if manifest.kind is ModKind.PAK:
            root_key = "tilde_mods"
            relative = relative_path
        elif manifest.kind is ModKind.LOGICPAK:
            root_key = "logic_mods"
            relative = relative_path
        else:
            parent = Path(os.path.abspath(manifest.install_root.parent))
            if parent == self.original_roots["ue4ss_classic"]:
                root_key = "ue4ss_classic"
            elif parent == self.original_roots["ue4ss_nested"]:
                root_key = "ue4ss_nested"
            else:
                raise ValueError("UE4SS manifest is outside supported roots")
            relative = f"{manifest.install_root.name}/{relative_path}"
        expected_root = self.original_roots[root_key]
        actual_root = (
            Path(os.path.abspath(manifest.install_root))
            if manifest.kind is not ModKind.UE4SS
            else Path(os.path.abspath(manifest.install_root.parent))
        )
        if actual_root != expected_root:
            raise ValueError("manifest install root does not match its kind")
        _relative_path_key(relative)
        return root_key, PureWindowsPath(relative).as_posix()

    def _manifest_source_identities(self, manifest: ModManifest) -> list[tuple[str, str]]:
        identities: list[tuple[str, str]] = []
        for item in manifest.files:
            identities.append(self._live_identity(manifest, item.relative_path))
            identities.append(("disabled", f"{manifest.id}/{item.relative_path}"))
        if manifest.ue4ss_enabled_txt is not None:
            identities.append(
                (
                    "disabled",
                    f"{manifest.id}/{manifest.ue4ss_enabled_txt.relative_path}",
                )
            )
        return identities

    def recycle_local_mod(
        self,
        manifest: ModManifest,
        audit: ManifestAudit,
        manifest_store: ManifestStore,
        *,
        now: datetime | None = None,
    ) -> TrashRecord:
        if audit.status is AuditStatus.MISSING:
            raise TrashPayloadMissing("managed mod files are missing")
        trash_id = uuid.uuid4().hex
        moves: list[tuple[Path, Path]] = []
        files: list[TrashFile] = []
        mods_txt = (
            manifest.install_root.parent / "mods.txt"
            if manifest.kind is ModKind.UE4SS
            else None
        )
        old_config = mods_txt.read_bytes() if mods_txt is not None and mods_txt.is_file() else None
        current_time = now or datetime.now(timezone.utc)
        if current_time.utcoffset() is None:
            raise ValueError("trash timestamp must include a timezone")
        record: TrashRecord | None = None
        try:
            for original_root, relative_path in self._manifest_source_identities(manifest):
                source = self.resolve_original(original_root, relative_path)
                if not source.is_file() or _is_reparse(source):
                    continue
                item = self.move_to_payload(trash_id, original_root, relative_path)
                files.append(item)
                moves.append((self.resolve_payload(trash_id, item), source))
            if not files:
                raise TrashPayloadMissing("managed mod has no recoverable files")
            if mods_txt is not None:
                self._write_atomic(
                    mods_txt,
                    remove_entry(old_config if old_config is not None else b"", manifest.install_root.name),
                )
            record = TrashRecord(
                id=trash_id,
                schema_version=1,
                entry_type="local_mod",
                name=manifest.name,
                created_at=current_time.isoformat(),
                expires_at=(current_time + timedelta(days=30)).isoformat(),
                game_fingerprint=self.game_fingerprint,
                files=tuple(files),
                manifest=ManifestStore._to_dict(manifest),
                ue4ss_state=(
                    {"name": manifest.install_root.name, "enabled": manifest.enabled}
                    if manifest.kind is ModKind.UE4SS
                    else None
                ),
                framework_ownership=None,
            )
            self.store.save(record)
            manifest_store.delete(manifest.id)
        except BaseException:
            if record is not None:
                self.store.delete(record.id)
            if mods_txt is not None:
                if old_config is None:
                    mods_txt.unlink(missing_ok=True)
                else:
                    self._write_atomic(mods_txt, old_config)
            try:
                manifest_store.get(manifest.id)
            except KeyError:
                manifest_store.save(manifest)
            self.rollback_moves(moves)
            raise
        for _payload, original in moves:
            if original.is_relative_to(self.data_dir / "disabled"):
                stop = self.data_dir / "disabled"
            elif manifest.kind is ModKind.UE4SS:
                stop = manifest.install_root.parent
            else:
                stop = manifest.install_root
            self._remove_empty_parents(original, stop)
        return record

    @staticmethod
    def _remove_empty_parents(path: Path, stop: Path) -> None:
        current = path.parent
        while current != stop and current.is_relative_to(stop):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _validated_local_record(
        self, trash_id: str, manifest_store: ManifestStore
    ) -> tuple[TrashRecord, ModManifest]:
        record = self.store.get(trash_id)
        if record.entry_type != "local_mod" or record.game_fingerprint != self.game_fingerprint:
            raise ValueError("trash record does not belong to this game")
        if record.manifest is None:
            raise ValueError("local trash record has no manifest")
        manifest = ManifestStore._from_dict(record.manifest)
        if manifest.id != record.manifest.get("id"):
            raise ValueError("trash manifest id is inconsistent")
        allowed = {
            (root, _relative_path_key(relative))
            for root, relative in self._manifest_source_identities(manifest)
        }
        supplied = {
            (item.original_root, _relative_path_key(item.relative_path))
            for item in record.files
        }
        if not supplied or not supplied.issubset(allowed):
            raise ValueError("trash record files do not match its manifest")
        for expected in manifest.files:
            live = self._live_identity(manifest, expected.relative_path)
            disabled = ("disabled", f"{manifest.id}/{expected.relative_path}")
            normalized_options = {
                (root, _relative_path_key(relative)) for root, relative in (live, disabled)
            }
            if supplied.isdisjoint(normalized_options):
                raise ValueError("trash record omits a managed manifest file")
        if manifest.ue4ss_enabled_txt is not None:
            metadata_identity = (
                "disabled",
                _relative_path_key(
                    f"{manifest.id}/{manifest.ue4ss_enabled_txt.relative_path}"
                ),
            )
            if metadata_identity not in supplied:
                raise ValueError("trash record omits UE4SS enabled metadata")
        try:
            manifest_store.get(manifest.id)
        except KeyError:
            pass
        else:
            raise TrashPayloadConflict("manifest is already active")
        active_paths: set[str] = set()
        for active in manifest_store.list():
            for root, relative in self._manifest_source_identities(active):
                active_paths.add(os.path.normcase(str(self.resolve_original(root, relative))))
        conflicts = [
            str(self.resolve_original(item.original_root, item.relative_path))
            for item in record.files
            if os.path.normcase(
                str(self.resolve_original(item.original_root, item.relative_path))
            ) in active_paths
        ]
        if conflicts:
            raise TrashPayloadConflict("trash files overlap active manifests", {"files": conflicts})
        return record, manifest

    def restore_local_mod(
        self, trash_id: str, manifest_store: ManifestStore
    ) -> ModManifest:
        record, manifest = self._validated_local_record(trash_id, manifest_store)
        for item in record.files:
            self.verify_payload(record.id, item)
            destination = self.resolve_original(item.original_root, item.relative_path)
            if os.path.lexists(destination):
                raise TrashPayloadConflict(
                    "trash restore target already exists", {"files": [str(destination)]}
                )
        mods_txt = (
            manifest.install_root.parent / "mods.txt"
            if manifest.kind is ModKind.UE4SS
            else None
        )
        old_config = mods_txt.read_bytes() if mods_txt is not None and mods_txt.is_file() else None
        moves: list[tuple[Path, Path]] = []
        saved_manifest = False
        try:
            for item in record.files:
                destination, payload = self.restore_file(record.id, item)
                moves.append((destination, payload))
            if mods_txt is not None:
                state = record.ue4ss_state or {}
                name = state.get("name")
                enabled = state.get("enabled")
                if type(name) is not str or type(enabled) is not bool:
                    raise ValueError("trash UE4SS state is invalid")
                self._write_atomic(
                    mods_txt,
                    update_entry(old_config if old_config is not None else b"", name, enabled),
                )
            manifest_store.save(manifest)
            saved_manifest = True
            self.store.delete(record.id)
        except BaseException:
            if saved_manifest:
                manifest_store.delete(manifest.id)
            if mods_txt is not None:
                if old_config is None:
                    mods_txt.unlink(missing_ok=True)
                else:
                    self._write_atomic(mods_txt, old_config)
            self.rollback_moves(moves)
            raise
        self._remove_empty_payload_roots(record)
        return manifest

    def _remove_empty_payload_roots(self, record: TrashRecord) -> None:
        for payload_root in {item.payload_root for item in record.files}:
            root = self._payload_base(payload_root, record.id)
            for item in sorted(
                (entry for entry in record.files if entry.payload_root == payload_root),
                key=lambda entry: len(PureWindowsPath(entry.payload_path).parts),
                reverse=True,
            ):
                self._remove_empty_parents(
                    self.resolve_payload(record.id, item), root.parent
                )

    def list_records(self, *, now: datetime | None = None) -> dict[str, object]:
        current_time = now or datetime.now(timezone.utc)
        records, invalid = self.store.list()
        items: list[dict[str, object]] = []
        for record in records:
            if record.game_fingerprint != self.game_fingerprint:
                continue
            expires = datetime.fromisoformat(record.expires_at)
            items.append(
                {
                    "id": record.id,
                    "entry_type": record.entry_type,
                    "name": record.name,
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                    "days_remaining": max(0, ceil((expires - current_time).total_seconds() / 86400)),
                    "file_count": len(record.files),
                    "valid": True,
                }
            )
        return {"items": items, "invalid_records": invalid}

    def purge(self, trash_id: str) -> dict[str, object]:
        record = self.store.get(trash_id)
        if record.game_fingerprint != self.game_fingerprint:
            raise ValueError("trash record does not belong to this game")
        for item in record.files:
            payload = self.resolve_payload(record.id, item)
            if not payload.exists():
                continue
            self.verify_payload(record.id, item)
        deleted = 0
        for item in record.files:
            payload = self.resolve_payload(record.id, item)
            if payload.is_file() and not _is_reparse(payload):
                payload.unlink()
                deleted += 1
        self.store.delete(record.id)
        self._remove_empty_payload_roots(record)
        return {"ok": True, "purged": record.id, "files_deleted": deleted}

    def purge_expired(self, *, now: datetime | None = None) -> dict[str, object]:
        current_time = now or datetime.now(timezone.utc)
        records, _invalid = self.store.list()
        purged: list[str] = []
        failed: list[str] = []
        for record in records:
            if record.game_fingerprint != self.game_fingerprint:
                continue
            if datetime.fromisoformat(record.expires_at) > current_time:
                continue
            try:
                self.purge(record.id)
                purged.append(record.id)
            except (OSError, ValueError, TrashPayloadError):
                failed.append(record.id)
        return {"purged": purged, "failed": failed}

    def rollback_moves(self, moves: list[tuple[Path, Path]]) -> None:
        for current, original in reversed(moves):
            if not current.exists():
                continue
            if os.path.lexists(original):
                raise TrashPayloadConflict(
                    "trash rollback target already exists", {"files": [str(original)]}
                )
            self._ensure_parent(original.parent)
            if not self._same_volume(current, original.parent):
                raise OSError("trash rollback must remain on the same volume")
            os.replace(current, original)
