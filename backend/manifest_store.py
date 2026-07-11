"""Per-mod manifests with safe filesystem validation and legacy migration."""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import stat
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

from .domain import AuditStatus, ManifestAudit, ManifestFile, ModKind, ModManifest
from .storage import JsonStore

_REPARSE_POINT = 0x400
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_DEVICES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


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
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise ValueError("relative_path must be purely relative")
    components = windows_path.parts
    if not components or any(part in ("", ".", "..") for part in components):
        raise ValueError("relative_path must be purely relative and contain no '..'")
    for component in components:
        if _WINDOWS_FORBIDDEN.search(component):
            raise ValueError("relative_path contains a Windows-invalid character or ADS")
        if component.endswith((".", " ")):
            raise ValueError("relative_path component cannot end in a dot or space")
        if component.split(".", 1)[0].casefold() in _WINDOWS_DEVICES:
            raise ValueError("relative_path contains a reserved Windows device name")
    normalized = ntpath.normpath(value.replace("/", "\\"))
    return "\\".join(part.casefold() for part in PureWindowsPath(normalized).parts)


def validate_no_reparse_ancestors(path: str | os.PathLike[str]) -> Path:
    """Return an absolute path after rejecting every existing reparse ancestor."""
    absolute = Path(os.path.abspath(path))
    ancestors: list[Path] = []
    current = absolute
    while True:
        ancestors.append(current)
        if current.parent == current:
            break
        current = current.parent
    for ancestor in reversed(ancestors):
        if _is_reparse(ancestor):
            raise ValueError(f"symlink/reparse point is not allowed: {ancestor}")
    return absolute


# Internal compatibility name retained for existing callers.
_reject_reparse_ancestors = validate_no_reparse_ancestors


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
    def __init__(
        self,
        root: str | os.PathLike[str],
        known_roots: Iterable[str | os.PathLike[str]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.manifests_dir = self.root
        self.known_roots = tuple(Path(path) for path in (known_roots or ()))

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
        candidate = _reject_reparse_ancestors(Path(path))
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"file does not exist: {candidate}") from exc
        if not _inside(resolved, root):
            raise ValueError(f"file is outside install_root: {candidate}")
        if not resolved.is_file() or _is_reparse(resolved):
            raise ValueError(f"not a regular file: {candidate}")
        relative = resolved.relative_to(root).as_posix()
        _relative_path_key(relative)
        return resolved, relative

    def _build_manifest(
        self,
        name: str,
        kind: ModKind,
        install_root: str | os.PathLike[str],
        files: Iterable[str | os.PathLike[str]],
        *,
        manifest_id: str,
        source_name: str = "",
        nexus_id: int | None = None,
        installed_at: str | None = None,
        enabled: bool = True,
        ue4ss_enabled_txt: str | os.PathLike[str] | None = None,
    ) -> ModManifest:
        root_path = _reject_reparse_ancestors(Path(install_root))
        try:
            resolved_root = root_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("install_root does not exist") from exc
        if not resolved_root.is_dir():
            raise ValueError("install_root must be a directory")

        records: list[ManifestFile] = []
        seen: set[str] = set()
        for supplied in files:
            resolved, relative = self._validated_file(Path(supplied), resolved_root)
            normalized = _relative_path_key(relative)
            if normalized in seen:
                raise ValueError(f"duplicate normalized path: {relative}")
            seen.add(normalized)
            records.append(self._record(resolved, relative))
        if not records:
            raise ValueError("at least one file is required")
        records.sort(key=lambda item: item.relative_path)

        enabled_record = None
        if ue4ss_enabled_txt is not None:
            resolved, relative = self._validated_file(Path(ue4ss_enabled_txt), resolved_root)
            enabled_record = self._record(resolved, relative)

        return ModManifest(
            id=uuid.UUID(manifest_id).hex,
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

    def create(
        self,
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
        manifest = self._build_manifest(
            name,
            kind,
            install_root,
            files,
            manifest_id=uuid.uuid4().hex,
            source_name=source_name,
            nexus_id=nexus_id,
            installed_at=installed_at,
            enabled=enabled,
            ue4ss_enabled_txt=ue4ss_enabled_txt,
        )
        self.save(manifest)
        return manifest

    @staticmethod
    def _to_dict(manifest: ModManifest) -> dict[str, Any]:
        value = asdict(manifest)
        if isinstance(manifest.kind, ModKind):
            value["kind"] = manifest.kind.value
        if isinstance(manifest.install_root, Path):
            value["install_root"] = str(manifest.install_root)
        if type(manifest.files) is tuple:
            value["files"] = list(value["files"])
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
        manifest_id = getattr(manifest, "id", "unknown")
        try:
            validated = self._from_dict(self._to_dict(manifest))
            for item in validated.files:
                self._audit_path(validated.install_root, item.relative_path)
            if validated.ue4ss_enabled_txt is not None:
                self._audit_path(
                    validated.install_root, validated.ue4ss_enabled_txt.relative_path
                )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid manifest: {manifest_id}: {exc}") from exc
        JsonStore(self._path(validated.id)).write(self._to_dict(validated))

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
            manifest = self._from_dict(value)
            requested_id = uuid.UUID(manifest_id).hex
            if manifest.id != requested_id:
                raise ValueError("JSON id does not match requested id")
            return manifest
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
                    manifest = self._from_dict(value)
                    if uuid.UUID(path.stem).hex != manifest.id:
                        raise ValueError("JSON id does not match manifest filename")
                    manifests.append(manifest)
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
        lexical_root = _reject_reparse_ancestors(root)
        resolved_root = lexical_root.resolve(strict=False)
        candidate = _reject_reparse_ancestors(resolved_root / Path(relative_path))
        resolved = candidate.resolve(strict=False)
        if not _inside(resolved, resolved_root):
            raise ValueError("relative_path resolves outside its root")
        return resolved

    def audit(self, manifest_or_id: ModManifest | str) -> ManifestAudit:
        manifest = (
            manifest_or_id
            if isinstance(manifest_or_id, ModManifest)
            else self.get(manifest_or_id)
        )
        live_root = manifest.install_root
        alternate_root = self.root.parent / "disabled" / manifest.id
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
        known_roots: Iterable[str | os.PathLike[str]] | None = None,
    ) -> list[ModManifest]:
        raw = JsonStore(legacy_path).read([])
        if not isinstance(raw, list):
            return []
        configured_roots = self.known_roots if known_roots is None else tuple(Path(root) for root in known_roots)
        if not configured_roots:
            return []
        roots: list[Path] = []
        for root in configured_roots:
            try:
                lexical_root = _reject_reparse_ancestors(Path(root))
                resolved = lexical_root.resolve(strict=True)
                if resolved.is_dir():
                    roots.append(resolved)
            except (OSError, ValueError):
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
                raw_install_path = _reject_reparse_ancestors(Path(item["install_path"]))
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
                manifest = self._build_manifest(
                    str(item["name"]),
                    ModKind(item["mod_type"]),
                    install_root,
                    files,
                    manifest_id=legacy_id,
                    source_name=str(item.get("source_name", "")),
                    nexus_id=item.get("nexus_id"),
                    installed_at=str(item.get("installed_at") or datetime.now(timezone.utc).isoformat()),
                    enabled=bool(item.get("enabled", True)),
                    ue4ss_enabled_txt=(install_root / "enabled.txt")
                    if item.get("mod_type") == ModKind.UE4SS.value
                    and (install_root / "enabled.txt").is_file()
                    else None,
                )
                self.save(manifest)
                migrated.append(manifest)
            except (KeyError, OSError, TypeError, ValueError):
                continue
        return migrated
