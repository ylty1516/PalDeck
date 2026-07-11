"""Safely inspect and extract supported mod ZIP archives."""

from __future__ import annotations

import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PureWindowsPath

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
    return tuple(parts)


def _reject_special_file(info: zipfile.ZipInfo) -> None:
    if info.create_system != 3:
        return
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"ZIP 包含符号链接或其他特殊文件：{info.filename!r}")


def _validate_entries(
    infos: list[zipfile.ZipInfo], dest: Path, policy: ArchivePolicy
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]]:
    if policy.max_files < 0 or policy.max_single_bytes < 0 or policy.max_total_bytes < 0:
        raise ValueError("解包限制不能为负数")

    files = 0
    total = 0
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]] = []
    for info in infos:
        parts = _safe_parts(info)
        _reject_special_file(info)
        if info.flag_bits & 0x1:
            raise ValueError(f"ZIP 包含加密条目，暂不支持解包：{info.filename!r}")

        target = dest.joinpath(*parts).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"ZIP 条目路径逃逸目标目录：{info.filename!r}")

        if not info.is_dir():
            files += 1
            if files > policy.max_files:
                raise ValueError(f"ZIP 文件数超过限制（最多 {policy.max_files} 个）")
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


def _extract(
    archive: zipfile.ZipFile,
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...], Path]],
    policy: ArchivePolicy,
) -> None:
    actual_total = 0
    for info, _, target in validated:
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        actual_file = 0
        with archive.open(info, "r") as source, target.open("xb") as output:
            while chunk := source.read(_CHUNK_SIZE):
                actual_file += len(chunk)
                actual_total += len(chunk)
                if actual_file > policy.max_single_bytes:
                    raise ValueError("解压后的单个文件实际大小超过安全限制")
                if actual_total > policy.max_total_bytes:
                    raise ValueError("解压后的实际总大小超过安全限制")
                output.write(chunk)


def inspect_and_extract(
    archive: Path | str,
    dest: Path | str,
    policy: ArchivePolicy | None = None,
) -> InspectedMod:
    """Validate, identify and stream-extract one ZIP into a new destination."""
    archive_path = Path(archive)
    destination = Path(dest).resolve()
    selected_policy = policy or ArchivePolicy()
    if destination.exists():
        raise ValueError(f"目标目录已存在，请选择空的新目录：{destination}")

    created = False
    try:
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            validated = _validate_entries(zip_file.infolist(), destination, selected_policy)
            inspected = _classify(validated, destination)
            destination.mkdir(parents=True)
            created = True
            _extract(zip_file, validated, selected_policy)
            return inspected
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError) as error:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise ValueError(
            "ZIP 文件已损坏或无法读取，请重新下载并确认压缩包完整"
        ) from error
    except Exception:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise
