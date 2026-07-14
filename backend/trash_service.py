"""Same-volume payload primitives for PalDeck's recoverable trash."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path, PureWindowsPath
from typing import Callable

from .game_detector import get_mod_directories
from .manifest_store import (
    _is_reparse,
    _relative_path_key,
    validate_no_reparse_ancestors,
)
from .process_utils import is_palworld_running
from .trash_store import TrashFile, TrashStore


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
