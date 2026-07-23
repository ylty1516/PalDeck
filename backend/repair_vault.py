"""Content-addressed, verified recovery copies for managed Mod files."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from .domain import ManifestFile
from .manifest_store import validate_no_reparse_ancestors

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


class RepairVault:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def object_path(self, digest: str) -> Path:
        normalized = str(digest).casefold()
        if _SHA256.fullmatch(normalized) is None:
            raise ValueError("repair object digest must be SHA-256")
        return self.root / "objects" / normalized[:2] / normalized

    @staticmethod
    def _matches(path: Path, expected: ManifestFile) -> bool:
        try:
            safe = validate_no_reparse_ancestors(path)
            return (
                safe.is_file()
                and not _is_reparse(safe)
                and safe.stat().st_size == expected.size
                and _sha256(safe) == expected.sha256
            )
        except (OSError, ValueError):
            return False

    def available(self, expected: ManifestFile) -> bool:
        return self._matches(self.object_path(expected.sha256), expected)

    def capture(self, source: Path, expected: ManifestFile) -> bool:
        source = validate_no_reparse_ancestors(source)
        if not self._matches(source, expected):
            raise ValueError("repair source does not match the manifest")
        destination = self.object_path(expected.sha256)
        if destination.exists():
            if not self._matches(destination, expected):
                raise ValueError("repair vault object is damaged")
            return False
        validate_no_reparse_ancestors(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if not self._matches(temporary, expected):
                raise ValueError("repair vault copy verification failed")
            os.replace(temporary, destination)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self, expected: ManifestFile, destination: Path) -> None:
        source = self.object_path(expected.sha256)
        if not self._matches(source, expected):
            raise FileNotFoundError("verified repair source is unavailable")
        destination = validate_no_reparse_ancestors(destination)
        if os.path.lexists(destination):
            raise FileExistsError(f"repair destination exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        validate_no_reparse_ancestors(destination.parent)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if not self._matches(temporary, expected):
                raise ValueError("restored repair object failed verification")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
