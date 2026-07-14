"""Strict versioned records for PalDeck's recoverable trash."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .manifest_store import _relative_path_key

_SCHEMA_VERSION = 1
_ENTRY_TYPES = {"local_mod", "ue4ss_framework"}
_ORIGINAL_ROOTS = {
    "tilde_mods",
    "logic_mods",
    "ue4ss_classic",
    "ue4ss_nested",
    "disabled",
    "framework_win64",
}
_PAYLOAD_ROOTS = {"game", "data"}
_RECORD_FIELDS = {
    "id",
    "schema_version",
    "entry_type",
    "name",
    "created_at",
    "expires_at",
    "game_fingerprint",
    "files",
    "manifest",
    "ue4ss_state",
    "framework_ownership",
}
_FILE_FIELDS = {
    "original_root",
    "relative_path",
    "payload_root",
    "payload_path",
    "size",
    "sha256",
}


@dataclass(frozen=True)
class TrashFile:
    original_root: Literal[
        "tilde_mods",
        "logic_mods",
        "ue4ss_classic",
        "ue4ss_nested",
        "disabled",
        "framework_win64",
    ]
    relative_path: str
    payload_root: Literal["game", "data"]
    payload_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class TrashRecord:
    id: str
    schema_version: int
    entry_type: Literal["local_mod", "ue4ss_framework"]
    name: str
    created_at: str
    expires_at: str
    game_fingerprint: str
    files: tuple[TrashFile, ...]
    manifest: dict[str, Any] | None
    ue4ss_state: dict[str, Any] | None
    framework_ownership: dict[str, Any] | None


class TrashStore:
    def __init__(self, records_dir: str | os.PathLike[str]) -> None:
        self.records_dir = Path(records_dir)

    @staticmethod
    def _id(value: Any) -> str:
        if type(value) is not str:
            raise ValueError("trash id must be a UUID string")
        try:
            return uuid.UUID(value).hex
        except (ValueError, AttributeError) as exc:
            raise ValueError("trash id must be a valid UUID") from exc

    def _path(self, trash_id: str) -> Path:
        return self.records_dir / f"{self._id(trash_id)}.json"

    @staticmethod
    def _datetime(value: Any, name: str) -> datetime:
        if type(value) is not str:
            raise ValueError(f"{name} must be an ISO-8601 string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
        if parsed.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return parsed

    @staticmethod
    def _file(value: Any) -> TrashFile:
        if not isinstance(value, dict) or set(value) != _FILE_FIELDS:
            raise ValueError("trash file fields are invalid")
        original_root = value["original_root"]
        payload_root = value["payload_root"]
        if type(original_root) is not str or original_root not in _ORIGINAL_ROOTS:
            raise ValueError("trash original_root is invalid")
        if type(payload_root) is not str or payload_root not in _PAYLOAD_ROOTS:
            raise ValueError("trash payload_root is invalid")
        relative_path = value["relative_path"]
        payload_path = value["payload_path"]
        _relative_path_key(relative_path)
        _relative_path_key(payload_path)
        size = value["size"]
        if type(size) is not int or size < 0:
            raise ValueError("trash file size is invalid")
        sha256 = value["sha256"]
        if type(sha256) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise ValueError("trash file sha256 is invalid")
        return TrashFile(
            original_root=original_root,
            relative_path=relative_path,
            payload_root=payload_root,
            payload_path=payload_path,
            size=size,
            sha256=sha256.casefold(),
        )

    @classmethod
    def from_dict(cls, value: Any) -> TrashRecord:
        if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
            raise ValueError("trash record fields are invalid")
        trash_id = cls._id(value["id"])
        if type(value["schema_version"]) is not int or value["schema_version"] != _SCHEMA_VERSION:
            raise ValueError("trash schema version is invalid")
        entry_type = value["entry_type"]
        if type(entry_type) is not str or entry_type not in _ENTRY_TYPES:
            raise ValueError("trash entry_type is invalid")
        name = value["name"]
        if type(name) is not str or not name.strip() or len(name) > 240:
            raise ValueError("trash name is invalid")
        created_at = cls._datetime(value["created_at"], "created_at")
        expires_at = cls._datetime(value["expires_at"], "expires_at")
        if expires_at <= created_at:
            raise ValueError("trash expires_at must be after created_at")
        fingerprint = value["game_fingerprint"]
        if type(fingerprint) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
            raise ValueError("trash game_fingerprint is invalid")
        raw_files = value["files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError("trash files must be a non-empty list")
        files = tuple(cls._file(item) for item in raw_files)
        identities = [
            (item.original_root, _relative_path_key(item.relative_path)) for item in files
        ]
        payloads = [
            (item.payload_root, _relative_path_key(item.payload_path)) for item in files
        ]
        if len(set(identities)) != len(identities) or len(set(payloads)) != len(payloads):
            raise ValueError("trash files contain duplicate normalized paths")
        manifest = value["manifest"]
        ue4ss_state = value["ue4ss_state"]
        framework_ownership = value["framework_ownership"]
        if manifest is not None and not isinstance(manifest, dict):
            raise ValueError("trash manifest must be an object or null")
        if ue4ss_state is not None and not isinstance(ue4ss_state, dict):
            raise ValueError("trash ue4ss_state must be an object or null")
        if framework_ownership is not None and not isinstance(framework_ownership, dict):
            raise ValueError("trash framework_ownership must be an object or null")
        if entry_type == "local_mod" and manifest is None:
            raise ValueError("local_mod trash requires a manifest")
        if entry_type == "ue4ss_framework" and manifest is not None:
            raise ValueError("framework trash cannot contain a mod manifest")
        if entry_type == "local_mod" and framework_ownership is not None:
            raise ValueError("local_mod trash cannot contain framework ownership")
        return TrashRecord(
            id=trash_id,
            schema_version=_SCHEMA_VERSION,
            entry_type=entry_type,
            name=name.strip(),
            created_at=value["created_at"],
            expires_at=value["expires_at"],
            game_fingerprint=fingerprint.casefold(),
            files=files,
            manifest=manifest.copy() if manifest is not None else None,
            ue4ss_state=ue4ss_state.copy() if ue4ss_state is not None else None,
            framework_ownership=(
                framework_ownership.copy() if framework_ownership is not None else None
            ),
        )

    @staticmethod
    def to_dict(record: TrashRecord) -> dict[str, Any]:
        value = asdict(record)
        value["files"] = [asdict(item) for item in record.files]
        return value

    def save(self, record: TrashRecord) -> None:
        validated = self.from_dict(self.to_dict(record))
        path = self._path(validated.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.to_dict(validated), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, trash_id: str) -> TrashRecord:
        path = self._path(trash_id)
        if not path.is_file():
            raise KeyError(self._id(trash_id))
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return self.from_dict(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("trash record is invalid") from exc

    def list(self) -> tuple[list[TrashRecord], list[str]]:
        valid: list[TrashRecord] = []
        invalid: list[str] = []
        if not self.records_dir.is_dir():
            return valid, invalid
        for path in sorted(self.records_dir.glob("*.json"), key=lambda item: item.name.casefold()):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                valid.append(self.from_dict(value))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                invalid.append(path.stem)
        valid.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        invalid.sort(key=str.casefold)
        return valid, invalid

    def delete(self, trash_id: str) -> None:
        self._path(trash_id).unlink(missing_ok=True)
