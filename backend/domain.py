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
