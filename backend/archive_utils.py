"""Safely inspect and extract supported mod ZIP archives."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from backend.domain import ArchivePolicy, InspectedMod, ModKind

_CHUNK_SIZE = 1024 * 1024
_SIDECAR_SUFFIXES = (".pak", ".utoc", ".ucas")
_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ARCHIVE_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, UnicodeError, zlib.error)


class _TargetError(ValueError):
    """A destination or staging path was unsafe or unusable."""


def _validate_windows_component(component: str, archive_name: str) -> None:
    if _INVALID_WINDOWS_NAME.search(component):
        raise ValueError(f"ZIP 路径包含 Windows 非法字符：{archive_name!r}")
    if component.endswith((".", " ")):
        raise ValueError(f"ZIP 路径组件以点或空格结尾，Windows 无法安全创建：{archive_name!r}")
    device_name = component.split(".", 1)[0].upper()
    if device_name in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"ZIP 路径使用了 Windows 保留设备名：{archive_name!r}")


def _safe_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename
    if not name or "\x00" in name:
        raise ValueError("ZIP 中存在空路径或异常路径，请重新打包后再试")

    windows_path = PureWindowsPath(name)
    if name.startswith(("/", "\\")) or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"ZIP 条目使用了绝对路径或盘符：{name!r}")

    normalized = name.replace("\\", "/")
    is_directory = normalized.endswith("/")
    parts = normalized.split("/")
    if is_directory:
        parts.pop()
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"ZIP 中存在空路径、异常路径或路径穿越：{name!r}")
    for component in parts:
        _validate_windows_component(component, name)
    return tuple(parts)


def _reject_special_file(info: zipfile.ZipInfo) -> None:
    if info.create_system != 3:
        return
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"ZIP 包含符号链接或其他特殊文件：{info.filename!r}")


def _validate_entries(
    infos: list[zipfile.ZipInfo], root: Path, policy: ArchivePolicy
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]]:
    if (
        policy.max_files < 0 or policy.max_single_bytes < 0
        or policy.max_total_bytes < 0 or policy.max_compression_ratio < 0
    ):
        raise ValueError("解包限制不能为负数")

    entries = 0
    total = 0
    canonical_paths: set[tuple[str, ...]] = set()
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]] = []
    for info in infos:
        parts = _safe_parts(info)
        entries += 1
        if entries > policy.max_files:
            raise ValueError(f"ZIP 文件数/条目数超过限制（最多 {policy.max_files} 个文件或目录）")
        canonical = tuple(part.rstrip(". ").casefold() for part in parts)
        if canonical in canonical_paths:
            raise ValueError(f"ZIP 包含 Windows 规范化后发生碰撞的路径：{info.filename!r}")
        canonical_paths.add(canonical)

        _reject_special_file(info)
        if info.flag_bits & 0x1:
            raise ValueError(f"ZIP 包含加密条目，暂不支持解包：{info.filename!r}")

        target = root.joinpath(*parts)
        if not target.is_relative_to(root):
            raise ValueError(f"ZIP 条目路径逃逸目标目录：{info.filename!r}")

        if not info.is_dir():
            if info.file_size and info.file_size / max(info.compress_size, 1) > policy.max_compression_ratio:
                raise ValueError(
                    f"ZIP 条目压缩比超过限制（最多 {policy.max_compression_ratio}:1）"
                )
            if info.file_size > policy.max_single_bytes:
                raise ValueError(
                    f"ZIP 单个文件声明大小超过限制（最多 {policy.max_single_bytes} 字节）"
                )
            total += info.file_size
            if total > policy.max_total_bytes:
                raise ValueError(
                    f"ZIP 声明的总大小超过限制（最多 {policy.max_total_bytes} 字节）"
                )
        validated.append((info, parts, target))
    return validated


def _relative_file_parts(
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]], suffix: str | None = None
) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = []
    for info, parts, _ in validated:
        if info.is_dir():
            continue
        if suffix is None or Path(parts[-1]).suffix.casefold() == suffix:
            result.append(parts)
    return result


def _find_ue4ss_root(files: list[tuple[str, ...]]) -> tuple[str, ...] | None:
    for parts in sorted(files, key=lambda item: tuple(part.casefold() for part in item)):
        folded = tuple(part.casefold() for part in parts)
        if len(parts) >= 2 and folded[-2:] == ("scripts", "main.lua"):
            return parts[:-2]
    return None


def _find_logic_root(paks: list[tuple[str, ...]]) -> tuple[str, ...] | None:
    for parts in sorted(paks, key=lambda item: tuple(part.casefold() for part in item)):
        for index, part in enumerate(parts[:-1]):
            if part.casefold() == "logicmods":
                return parts[: index + 1]
    return None


def _common_parent(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    parents = [parts[:-1] for parts in paths]
    common: list[str] = []
    for candidates in zip(*parents):
        if len({candidate.casefold() for candidate in candidates}) != 1:
            break
        common.append(candidates[0])
    return tuple(common)


def _clean_display_name(value: str) -> str:
    cleaned = _INVALID_WINDOWS_NAME.sub("_", value).strip().rstrip(". ")
    if not cleaned:
        cleaned = "Mod"
    if cleaned.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:255].rstrip(". ") or "Mod"


def _classify(
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]], dest: Path
) -> InspectedMod:
    files = _relative_file_parts(validated)
    ue4ss_root = _find_ue4ss_root(files)
    paks = _relative_file_parts(validated, ".pak")

    if ue4ss_root is not None:
        root = dest.joinpath(*ue4ss_root)
        fallback = Path(ue4ss_root[-1]).name if ue4ss_root else "UE4SS Mod"
        return InspectedMod(ModKind.UE4SS, _clean_display_name(fallback), root, ())

    if not paks:
        raise ValueError(
            "无法识别该 ZIP：需要 Scripts/main.lua 或至少一个 .pak 文件，请检查压缩包结构"
        )

    logic_root = _find_logic_root(paks)
    kind = ModKind.LOGICPAK if logic_root is not None else ModKind.PAK
    content_parts = logic_root if logic_root is not None else _common_parent(paks)
    content_root = dest.joinpath(*content_parts)

    candidates: dict[tuple[tuple[str, ...], str], dict[str, Path]] = {}
    pak_keys: list[tuple[tuple[str, ...], str]] = []
    for parts in files:
        path = Path(parts[-1])
        suffix = path.suffix.casefold()
        if suffix not in _SIDECAR_SUFFIXES:
            continue
        key = (tuple(part.casefold() for part in parts[:-1]), path.stem.casefold())
        candidates.setdefault(key, {})[suffix] = dest.joinpath(*parts)
        if suffix == ".pak" and key not in pak_keys:
            pak_keys.append(key)

    pak_keys.sort(key=lambda key: (key[0], key[1]))
    groups = tuple(
        tuple(candidates[key][suffix] for suffix in _SIDECAR_SUFFIXES if suffix in candidates[key])
        for key in pak_keys
    )
    first_pak = groups[0][0]
    generic = {"", "paks", "logicmods", "~mods"}
    directory_name = content_root.name if content_parts else ""
    display = first_pak.stem if directory_name.casefold() in generic else directory_name
    return InspectedMod(kind, _clean_display_name(display), content_root, groups)


def _is_reparse_path(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT)


def _assert_safe_chain(root: Path, target: Path) -> None:
    if not target.is_relative_to(root):
        raise _TargetError(f"目标路径逃逸私有暂存目录：{target}")
    for component in (Path(), *target.relative_to(root).parents[::-1], target.relative_to(root)):
        candidate = root if component == Path() else root / component
        if _is_reparse_path(candidate):
            raise _TargetError(f"目标路径包含符号链接、junction 或重解析点：{candidate}")


def _assert_ancestor_chain_safe(path: Path) -> None:
    chain = list(reversed((path, *path.parents)))
    for candidate in chain:
        if _is_reparse_path(candidate):
            raise _TargetError(f"目标路径包含符号链接、junction 或重解析点：{candidate}")


def _mkdir_checked(root: Path, path: Path) -> None:
    _assert_safe_chain(root, path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _TargetError(f"无法创建目标目录：{path}") from error
    _assert_safe_chain(root, path)


def _extract(
    archive: zipfile.ZipFile,
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]],
    policy: ArchivePolicy,
    staging: Path,
) -> None:
    actual_total = 0
    for info, _, target in validated:
        if info.is_dir():
            _mkdir_checked(staging, target)
            continue
        _mkdir_checked(staging, target.parent)
        _assert_safe_chain(staging, target)
        actual_file = 0
        try:
            output = target.open("xb")
        except OSError as error:
            raise _TargetError(f"无法打开目标文件进行写入：{target}") from error
        try:
            _assert_safe_chain(staging, target)
            with archive.open(info, "r") as source, output:
                while chunk := source.read(_CHUNK_SIZE):
                    actual_file += len(chunk)
                    actual_total += len(chunk)
                    if actual_file > policy.max_single_bytes:
                        raise ValueError("解压后的单个文件实际大小超过安全限制")
                    if actual_total > policy.max_total_bytes:
                        raise ValueError("解压后的实际总大小超过安全限制")
                    try:
                        output.write(chunk)
                    except OSError as error:
                        raise _TargetError(f"写入目标文件失败：{target}") from error
                    _assert_safe_chain(staging, target)
        finally:
            if not output.closed:
                output.close()
        _assert_safe_chain(staging, target)


def _assert_tree_safe(root: Path) -> None:
    _assert_safe_chain(root, root)
    try:
        with os.scandir(root) as entries:
            children = [Path(entry.path) for entry in entries]
    except OSError as error:
        raise _TargetError(f"无法检查目标暂存目录：{root}") from error
    for child in children:
        if _is_reparse_path(child):
            raise _TargetError(f"目标暂存目录包含链接或重解析点：{child}")
        if child.is_dir():
            _assert_tree_safe(child)


def _cleanup_staging(staging: Path | None) -> None:
    if staging is None:
        return
    try:
        if _is_reparse_path(staging):
            try:
                staging.unlink()
            except OSError:
                staging.rmdir()
        else:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        pass


def extract_archive_safely(
    archive: Path | str | BinaryIO,
    dest: Path | str,
    policy: ArchivePolicy | None = None,
) -> Path:
    """Validate and stream-extract an archive without applying Mod classification."""
    archive_source = Path(archive) if isinstance(archive, (str, os.PathLike)) else archive
    destination = Path(os.path.abspath(dest))
    selected_policy = policy or ArchivePolicy()
    staging: Path | None = None

    try:
        _assert_ancestor_chain_safe(destination.parent)
        if os.path.lexists(destination):
            raise _TargetError(f"目标目录已存在，请选择空的新目录：{destination}")
        try:
            zip_file = zipfile.ZipFile(archive_source, "r")
        except _ARCHIVE_ERRORS as error:
            raise ValueError("ZIP 文件已损坏或无法解码，请重新下载并确认压缩包完整") from error
        except OSError as error:
            raise ValueError(f"无法读取 ZIP 文件：{archive_source}") from error

        with zip_file:
            try:
                validated = _validate_entries(
                    zip_file.infolist(), destination, selected_policy
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                _assert_ancestor_chain_safe(destination.parent)
                staging = Path(
                    tempfile.mkdtemp(
                        prefix=f".{destination.name}.",
                        suffix=".tmp",
                        dir=destination.parent,
                    )
                )
                _assert_safe_chain(staging, staging)
                staged_entries = [
                    (info, parts, staging.joinpath(*parts))
                    for info, parts, _ in validated
                ]
                _extract(zip_file, staged_entries, selected_policy, staging)
            except _ARCHIVE_ERRORS as error:
                raise ValueError("ZIP 文件已损坏或无法解码，请重新下载并确认压缩包完整") from error

        _assert_tree_safe(staging)
        _assert_ancestor_chain_safe(destination.parent)
        if os.path.lexists(destination):
            raise _TargetError(f"目标目录发布前已被占用：{destination}")
        try:
            os.replace(staging, destination)
        except OSError as error:
            raise _TargetError(f"无法原子发布到目标目录：{destination}") from error
        staging = None
        return destination
    finally:
        _cleanup_staging(staging)


def inspect_and_extract(
    archive: Path | str,
    dest: Path | str,
    policy: ArchivePolicy | None = None,
) -> InspectedMod:
    """Validate, identify and stream-extract one ZIP, then atomically publish it."""
    archive_path = Path(archive)
    destination = Path(os.path.abspath(dest))
    selected_policy = policy or ArchivePolicy()
    staging: Path | None = None

    try:
        _assert_ancestor_chain_safe(destination.parent)
        if os.path.lexists(destination):
            raise _TargetError(f"目标目录已存在，请选择空的新目录：{destination}")

        try:
            zip_file = zipfile.ZipFile(archive_path, "r")
        except _ARCHIVE_ERRORS as error:
            raise ValueError("ZIP 文件已损坏或无法解码，请重新下载并确认压缩包完整") from error
        except OSError as error:
            raise ValueError(f"无法读取 ZIP 文件：{archive_path}") from error

        with zip_file:
            try:
                validated = _validate_entries(
                    zip_file.infolist(), destination, selected_policy
                )
                inspected = _classify(validated, destination)

                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    raise _TargetError(
                        f"无法创建目标目录的父目录：{destination.parent}"
                    ) from error
                _assert_ancestor_chain_safe(destination.parent)
                try:
                    staging = Path(
                        tempfile.mkdtemp(
                            prefix=f".{destination.name}.",
                            suffix=".tmp",
                            dir=destination.parent,
                        )
                    )
                except OSError as error:
                    raise _TargetError("无法创建私有目标暂存目录") from error
                _assert_safe_chain(staging, staging)

                staged_entries = [
                    (info, parts, staging.joinpath(*parts))
                    for info, parts, _ in validated
                ]
                _extract(zip_file, staged_entries, selected_policy, staging)
            except _ARCHIVE_ERRORS as error:
                raise ValueError("ZIP 文件已损坏或无法解码，请重新下载并确认压缩包完整") from error

        _assert_tree_safe(staging)
        _assert_ancestor_chain_safe(destination.parent)
        if os.path.lexists(destination):
            raise _TargetError(f"目标目录发布前已被占用：{destination}")
        try:
            os.replace(staging, destination)
        except OSError as error:
            raise _TargetError(f"无法原子发布到目标目录：{destination}") from error
        staging = None
        return inspected
    finally:
        _cleanup_staging(staging)
