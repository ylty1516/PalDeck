"""Domain models shared by archive inspection and mod installation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ModKind(StrEnum):
    PAK = "pak"
    LOGICPAK = "logicpak"
    UE4SS = "ue4ss"


@dataclass(frozen=True)
class ManifestFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModManifest:
    id: str
    name: str
    kind: ModKind
    install_root: Path
    source_name: str
    nexus_id: int | None
    installed_at: str
    enabled: bool
    files: tuple[ManifestFile, ...]
    ue4ss_enabled_txt: ManifestFile | None = None


class AuditStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    MODIFIED = "modified"
    MISSING = "missing"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ManifestAudit:
    manifest_id: str
    status: AuditStatus


@dataclass(frozen=True)
class ArchivePolicy:
    max_files: int = 5000
    max_single_bytes: int = 2 * 1024**3
    max_total_bytes: int = 8 * 1024**3


@dataclass(frozen=True)
class InspectedMod:
    kind: ModKind
    display_name: str
    content_root: Path
    groups: tuple[tuple[Path, ...], ...]
