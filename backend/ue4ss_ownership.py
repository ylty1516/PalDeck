"""Strict ownership and integrity audit for installed UE4SS framework files."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Literal

from .manifest_store import _is_reparse, _relative_path_key, validate_no_reparse_ancestors
from .storage import JsonStore

_SOURCES = {"bundled", "upstream", "local_zip"}
_TOP_FIELDS = {"schema_version", "games"}
_RECORD_FIELDS = {
    "schema_version",
    "game_fingerprint",
    "source",
    "asset_name",
    "asset_sha256",
    "installed_at",
    "files",
    "framework_mods",
}
_FILE_FIELDS = {"relative_path", "size", "sha256", "mutable"}


class OwnershipStoreInvalid(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("game fingerprint is invalid")
    return value.casefold()


def _timestamp(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("installed_at must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("installed_at is invalid") from exc
    if parsed.utcoffset() is None:
        raise ValueError("installed_at must include a timezone")
    return value


@dataclass(frozen=True)
class OwnedFrameworkFile:
    relative_path: str
    size: int
    sha256: str
    mutable: bool

    def __post_init__(self) -> None:
        normalized = _relative_path_key(self.relative_path)
        if type(self.size) is not int or self.size < 0:
            raise ValueError("owned file size is invalid")
        if type(self.sha256) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256) is None:
            raise ValueError("owned file sha256 is invalid")
        if type(self.mutable) is not bool:
            raise ValueError("owned file mutable must be a bool")
        object.__setattr__(self, "relative_path", PureWindowsPath(self.relative_path).as_posix())
        object.__setattr__(self, "sha256", self.sha256.casefold())

    @classmethod
    def from_path(
        cls, win64: Path, path: Path, *, mutable: bool
    ) -> "OwnedFrameworkFile":
        trusted = validate_no_reparse_ancestors(win64).resolve(strict=True)
        candidate = validate_no_reparse_ancestors(path).resolve(strict=True)
        if not candidate.is_relative_to(trusted) or not candidate.is_file() or _is_reparse(candidate):
            raise ValueError("owned framework file is outside Win64 or unsafe")
        relative = candidate.relative_to(trusted).as_posix()
        return cls(relative, candidate.stat().st_size, _sha256(candidate), mutable)


@dataclass(frozen=True)
class OwnershipRecord:
    schema_version: int
    game_fingerprint: str
    source: Literal["bundled", "upstream", "local_zip"]
    asset_name: str
    asset_sha256: str
    installed_at: str
    files: tuple[OwnedFrameworkFile, ...]
    framework_mods: tuple[str, ...]

    @classmethod
    def create(
        cls,
        game_fingerprint: str,
        source: str,
        asset_name: str,
        asset_sha256: str,
        files: Iterable[OwnedFrameworkFile],
        framework_mods: Iterable[str],
        *,
        installed_at: str | None = None,
    ) -> "OwnershipRecord":
        return cls(
            schema_version=1,
            game_fingerprint=game_fingerprint,
            source=source,
            asset_name=asset_name,
            asset_sha256=asset_sha256,
            installed_at=installed_at or datetime.now(timezone.utc).isoformat(),
            files=tuple(files),
            framework_mods=tuple(framework_mods),
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ownership schema is invalid")
        object.__setattr__(self, "game_fingerprint", _fingerprint(self.game_fingerprint))
        if type(self.source) is not str or self.source not in _SOURCES:
            raise ValueError("ownership source is invalid")
        if (
            type(self.asset_name) is not str
            or not self.asset_name
            or Path(self.asset_name).name != self.asset_name
        ):
            raise ValueError("ownership asset_name is invalid")
        if type(self.asset_sha256) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", self.asset_sha256) is None:
            raise ValueError("ownership asset sha256 is invalid")
        object.__setattr__(self, "asset_sha256", self.asset_sha256.casefold())
        object.__setattr__(self, "installed_at", _timestamp(self.installed_at))
        if not self.files or not all(isinstance(item, OwnedFrameworkFile) for item in self.files):
            raise ValueError("ownership files are invalid")
        keys = [_relative_path_key(item.relative_path) for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("ownership files contain duplicates")
        mods: list[str] = []
        seen: set[str] = set()
        for name in self.framework_mods:
            if type(name) is not str or len(PureWindowsPath(name).parts) != 1:
                raise ValueError("framework mod name is invalid")
            normalized = _relative_path_key(name)
            if normalized in seen:
                raise ValueError("framework mod names contain duplicates")
            seen.add(normalized)
            mods.append(name.strip())
        object.__setattr__(self, "framework_mods", tuple(mods))


@dataclass(frozen=True)
class OwnershipAudit:
    integrity: Literal["healthy", "missing", "modified", "conflict"]
    missing: tuple[str, ...]
    modified: tuple[str, ...]
    conflicts: tuple[str, ...]


class OwnershipStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._json = JsonStore(self.path)

    @staticmethod
    def _file(value: Any) -> OwnedFrameworkFile:
        if not isinstance(value, dict) or set(value) != _FILE_FIELDS:
            raise OwnershipStoreInvalid("ownership file fields are invalid")
        try:
            return OwnedFrameworkFile(**value)
        except (TypeError, ValueError) as exc:
            raise OwnershipStoreInvalid("ownership file is invalid") from exc

    @classmethod
    def _record(cls, value: Any) -> OwnershipRecord:
        if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
            raise OwnershipStoreInvalid("ownership record fields are invalid")
        if not isinstance(value["files"], list) or not isinstance(value["framework_mods"], list):
            raise OwnershipStoreInvalid("ownership record lists are invalid")
        try:
            return OwnershipRecord(
                schema_version=value["schema_version"],
                game_fingerprint=value["game_fingerprint"],
                source=value["source"],
                asset_name=value["asset_name"],
                asset_sha256=value["asset_sha256"],
                installed_at=value["installed_at"],
                files=tuple(cls._file(item) for item in value["files"]),
                framework_mods=tuple(value["framework_mods"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnershipStoreInvalid("ownership record is invalid") from exc

    @staticmethod
    def _to_record(record: OwnershipRecord) -> dict[str, Any]:
        value = asdict(record)
        value["files"] = [asdict(item) for item in record.files]
        value["framework_mods"] = list(record.framework_mods)
        return value

    def _read_locked(self) -> dict[str, OwnershipRecord]:
        if not self.path.exists():
            return {}
        try:
            validate_no_reparse_ancestors(self.path)
            if _is_reparse(self.path) or not self.path.is_file():
                raise OwnershipStoreInvalid("ownership store path is unsafe")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != _TOP_FIELDS:
                raise OwnershipStoreInvalid("ownership store fields are invalid")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
                raise OwnershipStoreInvalid("ownership store schema is invalid")
            if not isinstance(raw["games"], dict):
                raise OwnershipStoreInvalid("ownership games must be an object")
            records: dict[str, OwnershipRecord] = {}
            for key, value in raw["games"].items():
                fingerprint = _fingerprint(key)
                record = self._record(value)
                if record.game_fingerprint != fingerprint:
                    raise OwnershipStoreInvalid("ownership game key does not match record")
                records[fingerprint] = record
            return records
        except OwnershipStoreInvalid:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OwnershipStoreInvalid("ownership store is invalid") from exc

    def _write_locked(self, records: dict[str, OwnershipRecord]) -> None:
        value = {
            "schema_version": 1,
            "games": {
                key: self._to_record(record) for key, record in sorted(records.items())
            },
        }
        validate_no_reparse_ancestors(self.path)
        self._json._write_unlocked(value)

    def save(self, record: OwnershipRecord) -> None:
        validated = self._record(self._to_record(record))
        with self._json.locked():
            records = self._read_locked()
            records[validated.game_fingerprint] = validated
            self._write_locked(records)

    def get(self, game_fingerprint: str) -> OwnershipRecord:
        fingerprint = _fingerprint(game_fingerprint)
        with self._json.locked():
            records = self._read_locked()
            try:
                return records[fingerprint]
            except KeyError as exc:
                raise KeyError(fingerprint) from exc

    def list(self) -> tuple[OwnershipRecord, ...]:
        with self._json.locked():
            records = self._read_locked()
            return tuple(records[key] for key in sorted(records))

    def delete(self, game_fingerprint: str) -> None:
        fingerprint = _fingerprint(game_fingerprint)
        with self._json.locked():
            records = self._read_locked()
            records.pop(fingerprint, None)
            self._write_locked(records)

    def audit(self, record: OwnershipRecord, win64: Path) -> OwnershipAudit:
        trusted = validate_no_reparse_ancestors(win64).resolve(strict=True)
        missing: list[str] = []
        modified: list[str] = []
        conflicts: list[str] = []
        for item in record.files:
            relative = Path(*PureWindowsPath(item.relative_path).parts)
            candidate = validate_no_reparse_ancestors(trusted / relative)
            if not candidate.is_relative_to(trusted):
                raise ValueError("ownership path escaped Win64")
            if os.path.lexists(candidate) and (_is_reparse(candidate) or not candidate.is_file()):
                conflicts.append(item.relative_path)
                continue
            if not candidate.is_file():
                missing.append(item.relative_path)
                continue
            if not item.mutable and (
                candidate.stat().st_size != item.size or _sha256(candidate) != item.sha256
            ):
                modified.append(item.relative_path)
        integrity: Literal["healthy", "missing", "modified", "conflict"]
        if conflicts:
            integrity = "conflict"
        elif modified:
            integrity = "modified"
        elif missing:
            integrity = "missing"
        else:
            integrity = "healthy"
        return OwnershipAudit(
            integrity,
            tuple(sorted(missing, key=str.casefold)),
            tuple(sorted(modified, key=str.casefold)),
            tuple(sorted(conflicts, key=str.casefold)),
        )
