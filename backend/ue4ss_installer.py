"""Install / detect UE4SS (RE-UE4SS) for Palworld."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .archive_utils import extract_archive_safely
from .domain import ArchivePolicy
from .game_detector import get_mod_directories, has_ue4ss
from .manifest_store import validate_no_reparse_ancestors
from .process_utils import check_directory_writable, is_palworld_running

GITHUB_API = "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/latest"
USER_AGENT = "PalworldModManager/1.1 (UE4SS installer)"
PALWORLD_REQUIRED_FILES = {
    "dwmapi.dll",
    "ue4ss/UE4SS.dll",
    "ue4ss/UE4SS-settings.ini",
    "ue4ss/MemberVariableLayout.ini",
}


class Ue4ssConflictError(Exception):
    def __init__(self, markers: dict[str, bool]):
        super().__init__("检测到已有 UE4SS 安装")
        self.details = {"markers": markers}


class Ue4ssGameRunningError(RuntimeError):
    pass


def _win64(game_root: Path | str) -> Path:
    return Path(game_root) / "Pal" / "Binaries" / "Win64"


def status(game_root: Path | str) -> dict[str, Any]:
    root = Path(game_root)
    win64 = _win64(root)
    installed = has_ue4ss(root)
    markers = {
        "dwmapi": (win64 / "dwmapi.dll").is_file(),
        "ue4ss_dll_root": (win64 / "UE4SS.dll").is_file(),
        "ue4ss_dll_sub": (win64 / "ue4ss" / "UE4SS.dll").is_file(),
        "settings_root": (win64 / "UE4SS-settings.ini").is_file(),
        "settings_sub": (win64 / "ue4ss" / "UE4SS-settings.ini").is_file(),
        "mods_dir": (win64 / "Mods").is_dir() or (win64 / "ue4ss" / "Mods").is_dir(),
    }
    bp_mod_loader = _bp_mod_loader_enabled(win64)
    return {
        "installed": installed,
        "win64": str(win64),
        "markers": markers,
        "bp_mod_loader": bp_mod_loader,
        "logicmods_ready": installed and bp_mod_loader is True,
        "logicmods_requirement": (
            "BPModLoaderMod 已启用" if bp_mod_loader is True
            else "LogicMods 需要用户在 mods.txt 中启用 BPModLoaderMod"
        ),
    }


def _bp_mod_loader_enabled(win64: Path) -> bool | None:
    for p in (win64 / "Mods" / "mods.txt", win64 / "ue4ss" / "Mods" / "mods.txt"):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"BPModLoaderMod\s*:\s*([01])", text, re.I)
        if m:
            return m.group(1) == "1"
    return None


def looks_like_ue4ss_framework(path: Path) -> bool:
    """True if path is a zip or extracted tree of RE-UE4SS itself."""
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = [n.replace("\\", "/").lower() for n in zf.namelist()]
        except zipfile.BadZipFile:
            return False
        return _names_look_like_ue4ss(names)

    if path.is_dir():
        names = [str(p.relative_to(path)).replace("\\", "/").lower() for p in path.rglob("*") if p.is_file()]
        return _names_look_like_ue4ss(names)
    return False


def _names_look_like_ue4ss(names: list[str]) -> bool:
    basenames = {n.rsplit("/", 1)[-1] for n in names}
    has_dll = "ue4ss.dll" in basenames
    has_settings = "ue4ss-settings.ini" in basenames
    has_proxy = "dwmapi.dll" in basenames or "xinput1_3.dll" in basenames
    # Framework package, not a small lua mod
    if has_dll and (has_settings or has_proxy):
        return True
    if has_settings and has_proxy and any("mods/" in n or n.endswith("mods.txt") for n in names):
        return True
    # Zip named / rooted like official release
    if has_dll and any(n.endswith("mods.txt") or "/mods/" in n for n in names):
        return True
    return False


def _find_package_root(extract_dir: Path) -> Path:
    """If zip has a single top folder, use it; else extract_dir."""
    # Prefer directory that contains UE4SS.dll or dwmapi.dll
    for marker in ("UE4SS.dll", "dwmapi.dll", "UE4SS-settings.ini"):
        hits = list(extract_dir.rglob(marker))
        if hits:
            # If UE4SS.dll is under ue4ss/, package root is parent of ue4ss or extract root
            p = hits[0]
            if p.name.lower() == "ue4ss.dll" and p.parent.name.lower() == "ue4ss":
                return p.parent.parent
            if p.name.lower() in ("ue4ss.dll", "dwmapi.dll", "ue4ss-settings.ini"):
                return p.parent
    children = [c for c in extract_dir.iterdir() if not c.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


def _patch_settings(win64: Path) -> list[str]:
    actions: list[str] = []
    for settings in (
        win64 / "UE4SS-settings.ini",
        win64 / "ue4ss" / "UE4SS-settings.ini",
    ):
        if not settings.is_file():
            continue
        text = settings.read_text(encoding="utf-8", errors="ignore")
        new = text
        if re.search(r"bUseUObjectArrayCache\s*=\s*true", new, re.I):
            new = re.sub(
                r"bUseUObjectArrayCache\s*=\s*true",
                "bUseUObjectArrayCache = false",
                new,
                flags=re.I,
            )
            actions.append(f"set bUseUObjectArrayCache=false in {settings.name}")
        elif not re.search(r"bUseUObjectArrayCache", new, re.I):
            new = new.rstrip() + "\n\n[General]\nbUseUObjectArrayCache = false\n"
            actions.append(f"append bUseUObjectArrayCache=false to {settings.name}")
        if new != text:
            settings.write_text(new, encoding="utf-8")
    return actions


def install_from_zip(
    game_root: Path | str,
    zip_path: Path | str,
    policy: ArchivePolicy | None = None,
    *,
    confirm_replace: bool = False,
    require_palworld_layout: bool = False,
) -> dict[str, Any]:
    """Transactionally install a local UE4SS ZIP (including legacy layouts)."""
    archive = Path(zip_path)
    if not archive.is_file():
        raise FileNotFoundError(f"找不到文件: {archive}")
    if archive.suffix.lower() != ".zip":
        raise ValueError("UE4SS 安装仅支持 .zip（可直接拖入 zip，无需手动解压）")
    return _install_archive(
        game_root, archive, policy=policy, confirm_replace=confirm_replace,
        require_palworld_layout=require_palworld_layout,
    )


def install_from_bytes(
    game_root: Path | str,
    archive_bytes: bytes,
    policy: ArchivePolicy | None = None,
    *,
    confirm_replace: bool = False,
    require_palworld_layout: bool = False,
) -> dict[str, Any]:
    """Install an immutable verified snapshot without reopening its source path."""
    if not isinstance(archive_bytes, bytes):
        raise TypeError("archive_bytes must be immutable bytes")
    return _install_archive(
        game_root, None, archive_bytes=archive_bytes, policy=policy,
        confirm_replace=confirm_replace, require_palworld_layout=require_palworld_layout,
    )


def _install_archive(
    game_root: Path | str,
    archive_path: Path | None,
    *,
    archive_bytes: bytes | None = None,
    policy: ArchivePolicy | None = None,
    confirm_replace: bool = False,
    require_palworld_layout: bool = False,
) -> dict[str, Any]:
    root = Path(game_root)
    if is_palworld_running():
        raise Ue4ssGameRunningError("幻兽帕鲁正在运行，无法安装 UE4SS")

    win64 = _win64(root)
    if not win64.is_dir():
        raise FileNotFoundError(f"找不到 Win64 目录: {win64}")
    validate_no_reparse_ancestors(win64)
    if not check_directory_writable(win64):
        raise PermissionError(f"目录不可写：{win64}")
    current_status = status(root)
    if current_status["installed"] and not confirm_replace:
        raise Ue4ssConflictError(current_status["markers"])

    temp = Path(tempfile.mkdtemp(prefix="ue4ss_install_"))
    backups = temp / "backups"
    published: list[Path] = []
    replaced: list[tuple[Path, Path]] = []
    created_dirs: list[Path] = []
    removed_legacy = False
    try:
        extract = temp / "extract"
        source_archive = archive_path
        if archive_bytes is not None:
            source_archive = temp / "archive.zip"
            source_archive.write_bytes(archive_bytes)
        if source_archive is None:
            raise ValueError("缺少 UE4SS 压缩包")
        extract_archive_safely(source_archive, extract, policy=policy)
        if require_palworld_layout:
            extracted_names = {
                str(path.relative_to(extract)).replace("\\", "/")
                for path in extract.rglob("*") if path.is_file()
            }
            missing = PALWORLD_REQUIRED_FILES - extracted_names
            if missing:
                raise ValueError(f"Palworld 专用 UE4SS 布局缺少文件: {sorted(missing)}")
        pkg = _find_package_root(extract)
        if not looks_like_ue4ss_framework(pkg) and not looks_like_ue4ss_framework(extract):
            raise ValueError(
                "压缩包不像 UE4SS 官方发布包。请从 UE4SS-RE/RE-UE4SS 的 GitHub Release 下载"
            )

        # Patch only the staged package; never mutate unrelated live settings.
        actions = _patch_settings(pkg)
        package_files = [path for path in pkg.rglob("*") if path.is_file()]
        if not package_files:
            raise ValueError("UE4SS 压缩包不包含可安装文件")
        planned: list[tuple[Path, Path]] = []
        for source in package_files:
            destination = win64 / source.relative_to(pkg)
            validate_no_reparse_ancestors(destination)
            planned.append((source, destination))

        legacy = win64 / "xinput1_3.dll"
        if legacy.is_file():
            backup = backups / "legacy" / legacy.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy, backup)
            replaced.append((backup, legacy))
            removed_legacy = True

        for index, (source, destination) in enumerate(planned):
            missing = [parent for parent in reversed(destination.parents) if parent != win64 and parent.is_relative_to(win64) and not parent.exists()]
            for directory in missing:
                directory.mkdir(exist_ok=True)
                created_dirs.append(directory)
            if destination.exists():
                if not destination.is_file():
                    raise ValueError(f"安装目标不是普通文件：{destination}")
                backup = backups / str(index) / destination.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                replaced.append((backup, destination))
            staged = temp / "publish" / str(index) / destination.name
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
            os.replace(staged, destination)
            published.append(destination)

        mods = get_mod_directories(root)["ue4ss_mods"]
        if not mods.exists():
            mods.mkdir(parents=True)
            created_dirs.append(mods)
        st = status(root)
        return {
            "ok": True,
            "installed": st["installed"],
            "win64": str(win64),
            "files_copied": len(published),
            "removed_xinput1_3": removed_legacy,
            "actions": actions,
            "status": st,
            "message": (
                "UE4SS 已安装。请启动一次游戏完成初始化；LogicMods 仅在用户启用 "
                "BPModLoaderMod 后可用，管理器未自动修改该开关。"
            ),
        }
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        for backup, destination in reversed(replaced):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                os.replace(backup, destination)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def download_latest_zip(dest_dir: Path | str) -> Path:
    """Download latest non-DEV UE4SS release zip from GitHub."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(
        GITHUB_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"无法获取 UE4SS 版本信息 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 GitHub: {e.reason}") from e

    assets = data.get("assets") or []
    # Prefer UE4SS_vX.Y.Z.zip, skip zDEV / zCustom / source
    candidates: list[dict[str, Any]] = []
    for a in assets:
        name = (a.get("name") or "").strip()
        lower = name.lower()
        if not lower.endswith(".zip"):
            continue
        if lower.startswith("zdev") or "zdev-" in lower:
            continue
        if lower.startswith("zcustom") or lower.startswith("zmap"):
            continue
        if "source" in lower:
            continue
        if lower.startswith("ue4ss") or re.match(r"ue4ss_v?\d", lower):
            candidates.append(a)

    if not candidates:
        # fallback: any zip not zDEV
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(".zip") and "zdev" not in name and "source" not in name:
                candidates.append(a)

    if not candidates:
        raise RuntimeError("GitHub Release 中未找到可下载的 UE4SS zip")

    # Prefer exact UE4SS_v*.zip over others
    def score(a: dict) -> tuple:
        n = (a.get("name") or "").lower()
        return (0 if re.match(r"ue4ss_v?\d", n) else 1, len(n))

    asset = sorted(candidates, key=score)[0]
    url = asset.get("browser_download_url")
    name = asset.get("name") or "UE4SS.zip"
    if not url:
        raise RuntimeError("资源缺少下载链接")
    official_prefix = "https://github.com/UE4SS-RE/RE-UE4SS/releases/download/"
    if not isinstance(url, str) or not url.startswith(official_prefix):
        raise RuntimeError("GitHub API 返回了非 UE4SS 官方下载地址，已拒绝下载")

    out = dest_dir / name
    temporary = out.with_name(f".{out.name}.download.tmp")
    req2 = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req2, timeout=300) as resp, open(temporary, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
    return out


def install_latest(game_root: Path | str, cache_dir: Path | str | None = None) -> dict[str, Any]:
    root = Path(game_root)
    cache = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "palmod_ue4ss_cache"
    cache.mkdir(parents=True, exist_ok=True)
    zip_path = download_latest_zip(cache)
    result = install_from_zip(root, zip_path)
    result["downloaded"] = str(zip_path)
    result["download_name"] = zip_path.name
    return result
