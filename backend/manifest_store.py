"""Per-mod manifests with safe filesystem validation and legacy migration."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
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
        files = tuple(ManifestFile(**item) for item in value["files"])
        enabled_value = value.get("ue4ss_enabled_txt")
        enabled_txt = ManifestFile(**enabled_value) if enabled_value is not None else None
        return ModManifest(
            id=uuid.UUID(str(value["id"])).hex,
            name=str(value["name"]),
            kind=ModKind(value["kind"]),
            install_root=Path(value["install_root"]),
            source_name=str(value.get("source_name", "")),
            nexus_id=value.get("nexus_id"),
            installed_at=str(value["installed_at"]),
            enabled=bool(value.get("enabled", True)),
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
        if value is None:
            raise ValueError(f"invalid manifest: {manifest_id}")
        return self._from_dict(value)

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
        return sorted(manifests, key=lambda item: item.id)

    def delete(self, manifest_id: str) -> None:
        try:
            path = self._path(manifest_id)
        except ValueError:
            return
        path.unlink(missing_ok=True)

    def audit(
        self, manifest_id: str, disabled_root: str | os.PathLike[str] | None = None
    ) -> ManifestAudit:
        manifest = self.get(manifest_id)
        live_root = manifest.install_root
        alternate_root = Path(disabled_root).resolve() if disabled_root is not None else None
        live_count = disabled_count = 0
        changed = False
        for expected in manifest.files:
            live = live_root / Path(expected.relative_path)
            disabled = alternate_root / Path(expected.relative_path) if alternate_root else None
            live_exists = live.is_file() and not _is_reparse(live)
            disabled_exists = bool(disabled and disabled.is_file() and not _is_reparse(disabled))
            if live_exists:
                live_count += 1
                changed |= live.stat().st_size != expected.size or _sha256(live) != expected.sha256
            if disabled_exists and disabled is not None:
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
