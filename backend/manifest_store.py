"""Per-mod manifests with safe filesystem validation and legacy migration."""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import stat
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .domain import AuditStatus, ManifestAudit, ManifestFile, ModKind, ModManifest
from .storage import JsonStore

_REPARSE_POINT = 0x400


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_path_key(value: Any) -> str:
    if type(value) is not str or not value:
        raise ValueError("relative_path must be a non-empty string")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise ValueError("relative_path must be purely relative")
    if posix_path.is_absolute() or any(part in ("", ".", "..") for part in posix_path.parts):
        raise ValueError("relative_path must be purely relative and contain no '..'")
    return ntpath.normcase(ntpath.normpath(value.replace("/", "\\")))


def _manifest_file(value: Any) -> ManifestFile:
    if not isinstance(value, dict):
        raise ValueError("file entry must be an object")
    relative_path = value.get("relative_path")
    _relative_path_key(relative_path)
    size = value.get("size")
    if type(size) is not int or size < 0:
        raise ValueError("file size must be a non-negative integer")
    sha256 = value.get("sha256")
    if type(sha256) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        raise ValueError("file sha256 must be exactly 64 hexadecimal characters")
    return ManifestFile(relative_path=relative_path, size=size, sha256=sha256.lower())


def _installed_datetime(value: Any) -> datetime:
    if type(value) is not str:
        raise ValueError("installed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("installed_at must be a valid ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("installed_at must include a timezone")
    return parsed


class ManifestStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.manifests_dir = self.root / "manifests"

    def _path(self, manifest_id: str) -> Path:
        try:
            normalized = uuid.UUID(manifest_id).hex
        except (ValueError, AttributeError) as exc:
            raise ValueError("manifest id must be a UUID") from exc
        return self.manifests_dir / f"{normalized}.json"

    @staticmethod
    def _record(path: Path, relative_path: str) -> ManifestFile:
        return ManifestFile(relative_path, path.stat().st_size, _sha256(path))

    @staticmethod
    def _validated_file(path: Path, root: Path) -> tuple[Path, str]:
        candidate = Path(path)
        # Check every existing lexical component before resolve follows it.
        current = candidate
        while True:
            if _is_reparse(current):
                raise ValueError(f"symlink/reparse point is not allowed: {candidate}")
            if current == root or current.parent == current:
                break
            current = current.parent
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"file does not exist: {candidate}") from exc
        if not _inside(resolved, root):
            raise ValueError(f"file is outside install_root: {candidate}")
        if not resolved.is_file() or _is_reparse(resolved):
            raise ValueError(f"not a regular file: {candidate}")
        relative = resolved.relative_to(root).as_posix()
        return resolved, relative

    def create(
        self,
        *,
        name: str,
        kind: ModKind,
        install_root: str | os.PathLike[str],
        files: Iterable[str | os.PathLike[str]],
        source_name: str = "",
        nexus_id: int | None = None,
        installed_at: str | None = None,
        enabled: bool = True,
        ue4ss_enabled_txt: str | os.PathLike[str] | None = None,
    ) -> ModManifest:
        root_path = Path(install_root)
        if _is_reparse(root_path):
            raise ValueError("install_root cannot be a symlink/reparse point")
        try:
            resolved_root = root_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("install_root does not exist") from exc
        if not resolved_root.is_dir():
            raise ValueError("install_root must be a directory")

        records: list[ManifestFile] = []
        seen: set[str] = set()
        path_by_relative: dict[str, Path] = {}
        for supplied in files:
            resolved, relative = self._validated_file(Path(supplied), resolved_root)
            normalized = relative.casefold() if os.name == "nt" else relative
            if normalized in seen:
                raise ValueError(f"duplicate normalized path: {relative}")
            seen.add(normalized)
            path_by_relative[relative] = resolved
            records.append(self._record(resolved, relative))
        if not records:
            raise ValueError("at least one file is required")
        records.sort(key=lambda item: item.relative_path)

        enabled_record = None
        if ue4ss_enabled_txt is not None:
            resolved, relative = self._validated_file(Path(ue4ss_enabled_txt), resolved_root)
            enabled_record = self._record(resolved, relative)

        manifest = ModManifest(
            id=uuid.uuid4().hex,
            name=name,
            kind=ModKind(kind),
            install_root=resolved_root,
            source_name=source_name,
            nexus_id=nexus_id,
            installed_at=installed_at or datetime.now(timezone.utc).isoformat(),
            enabled=bool(enabled),
            files=tuple(records),
            ue4ss_enabled_txt=enabled_record,
        )
        self.save(manifest)
        return manifest

    @staticmethod
    def _to_dict(manifest: ModManifest) -> dict[str, Any]:
        value = asdict(manifest)
        value["kind"] = manifest.kind.value
        value["install_root"] = str(manifest.install_root)
        return value

    @staticmethod
    def _from_dict(value: Any) -> ModManifest:
        if not isinstance(value, dict):
            raise ValueError("manifest must be an object")
        required = {
            "id", "name", "kind", "install_root", "source_name", "nexus_id",
            "installed_at", "enabled", "files", "ue4ss_enabled_txt",
        }
        if not required.issubset(value):
            raise ValueError("manifest is missing required fields")
        if type(value["id"]) is not str:
            raise ValueError("id must be a UUID string")
        try:
            manifest_id = uuid.UUID(value["id"]).hex
        except ValueError as exc:
            raise ValueError("id must be a valid UUID") from exc
        if type(value["name"]) is not str:
            raise ValueError("name must be a string")
        if type(value["kind"]) is not str:
            raise ValueError("kind must be a string")
        try:
            kind = ModKind(value["kind"])
        except ValueError as exc:
            raise ValueError("kind must be a valid ModKind") from exc
        if type(value["install_root"]) is not str:
            raise ValueError("install_root must be a string")
        if type(value["source_name"]) is not str:
            raise ValueError("source_name must be a string")
        nexus_id = value["nexus_id"]
        if nexus_id is not None and type(nexus_id) is not int:
            raise ValueError("nexus_id must be an integer or null")
        _installed_datetime(value["installed_at"])
        if type(value["enabled"]) is not bool:
            raise ValueError("enabled must be a boolean")
        if not isinstance(value["files"], list) or not value["files"]:
            raise ValueError("files must be a non-empty list")
        files = tuple(_manifest_file(item) for item in value["files"])
        keys = [_relative_path_key(item.relative_path) for item in files]
        if len(keys) != len(set(keys)):
            raise ValueError("files contain duplicate Windows-normalized paths")
        enabled_value = value["ue4ss_enabled_txt"]
        enabled_txt = _manifest_file(enabled_value) if enabled_value is not None else None
        return ModManifest(
            id=manifest_id,
            name=value["name"],
            kind=kind,
            install_root=Path(value["install_root"]),
            source_name=value["source_name"],
            nexus_id=nexus_id,
            installed_at=value["installed_at"],
            enabled=value["enabled"],
            files=files,
            ue4ss_enabled_txt=enabled_txt,
        )

    def save(self, manifest: ModManifest) -> None:
        JsonStore(self._path(manifest.id)).write(self._to_dict(manifest))

    def get(self, manifest_id: str) -> ModManifest:
        try:
            path = self._path(manifest_id)
        except ValueError as exc:
            raise KeyError(manifest_id) from exc
        if not path.is_file():
            raise KeyError(manifest_id)
        value = JsonStore(path).read(None)
        try:
            if value is None:
                raise ValueError("invalid JSON")
            return self._from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid manifest: {manifest_id}: {exc}") from exc

    def list(self) -> list[ModManifest]:
        if not self.manifests_dir.is_dir():
            return []
        manifests: list[ModManifest] = []
        for path in sorted(self.manifests_dir.glob("*.json"), key=lambda item: item.name):
            try:
                value = JsonStore(path).read(None)
                if value is not None:
                    manifests.append(self._from_dict(value))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(manifests, key=lambda item: (_installed_datetime(item.installed_at), item.id))

    def delete(self, manifest_id: str) -> None:
        try:
            path = self._path(manifest_id)
        except ValueError:
            return
        path.unlink(missing_ok=True)

    @staticmethod
    def _audit_path(root: Path, relative_path: str) -> Path:
        _relative_path_key(relative_path)
        resolved_root = root.resolve(strict=False)
        if _is_reparse(root):
            raise ValueError(f"symlink/reparse point is not allowed: {root}")
        candidate = resolved_root / Path(relative_path)
        current = candidate
        while current != resolved_root:
            if _is_reparse(current):
                raise ValueError(f"symlink/reparse point is not allowed: {candidate}")
            if current.parent == current:
                raise ValueError("relative_path resolves outside its root")
            current = current.parent
        resolved = candidate.resolve(strict=False)
        if not _inside(resolved, resolved_root):
            raise ValueError("relative_path resolves outside its root")
        return resolved

    def audit(
        self, manifest_id: str, disabled_root: str | os.PathLike[str] | None = None
    ) -> ManifestAudit:
        manifest = self.get(manifest_id)
        live_root = manifest.install_root
        alternate_root = (
            Path(disabled_root) if disabled_root is not None else live_root.parent / "disabled"
        ) / manifest.id
        live_count = disabled_count = 0
        changed = False
        for expected in manifest.files:
            live = self._audit_path(live_root, expected.relative_path)
            disabled = self._audit_path(alternate_root, expected.relative_path)
            live_exists = live.is_file()
            disabled_exists = disabled.is_file()
            if live_exists:
                live_count += 1
                changed |= live.stat().st_size != expected.size or _sha256(live) != expected.sha256
            if disabled_exists:
                disabled_count += 1
                changed |= disabled.stat().st_size != expected.size or _sha256(disabled) != expected.sha256
        if live_count and disabled_count:
            status = AuditStatus.CONFLICT
        elif changed:
            status = AuditStatus.MODIFIED
        elif live_count == len(manifest.files):
            status = AuditStatus.ENABLED
        elif disabled_count == len(manifest.files):
            status = AuditStatus.DISABLED
        else:
            status = AuditStatus.MISSING
        return ManifestAudit(manifest.id, status)

    def migrate_legacy_registry(
        self,
        legacy_path: str | os.PathLike[str],
        known_roots: Iterable[str | os.PathLike[str]],
    ) -> list[ModManifest]:
        raw = JsonStore(legacy_path).read([])
        if not isinstance(raw, list):
            return []
        roots: list[Path] = []
        for root in known_roots:
            try:
                resolved = Path(root).resolve(strict=True)
                if resolved.is_dir() and not _is_reparse(Path(root)):
                    roots.append(resolved)
            except OSError:
                continue
        migrated: list[ModManifest] = []
        for item in raw:
            try:
                if not isinstance(item, dict):
                    continue
                legacy_id = uuid.UUID(str(item["id"])).hex
                try:
                    self.get(legacy_id)
                    continue
                except KeyError:
                    pass
                raw_install_path = Path(item["install_path"])
                if _is_reparse(raw_install_path):
                    continue
                install_path = raw_install_path.resolve(strict=True)
                if not any(_inside(install_path, root) for root in roots):
                    continue
                install_root = install_path if install_path.is_dir() else install_path.parent
                if install_path.is_file():
                    files = [install_path]
                else:
                    names = item.get("files")
                    if not isinstance(names, list) or not names:
                        continue
                    files = [install_root / str(name) for name in names]
                created = self.create(
                    name=str(item["name"]),
                    kind=ModKind(item["mod_type"]),
                    install_root=install_root,
                    files=files,
                    source_name=str(item.get("source_name", "")),
                    nexus_id=item.get("nexus_id"),
                    installed_at=str(item.get("installed_at") or datetime.now(timezone.utc).isoformat()),
                    enabled=bool(item.get("enabled", True)),
                    ue4ss_enabled_txt=(install_root / "enabled.txt")
                    if item.get("mod_type") == ModKind.UE4SS.value
                    and (install_root / "enabled.txt").is_file()
                    else None,
                )
                # Preserve valid legacy UUID and remove the temporary create id.
                self.delete(created.id)
                created = replace(created, id=legacy_id)
                self.save(created)
                migrated.append(created)
            except (KeyError, OSError, TypeError, ValueError):
                continue
        return migrated
