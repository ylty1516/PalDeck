"""Check GitHub Releases and self-update the panel EXE."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .version import APP_VERSION, GITHUB_OWNER, GITHUB_REPO

USER_AGENT = f"PalworldModManager/{APP_VERSION}"


def _parse_version(v: str) -> tuple[int, ...]:
    v = (v or "").strip().lstrip("vV")
    # take leading digits.digits
    m = re.match(r"(\d+(?:\.\d+)*)", v)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def compare_versions(a: str, b: str) -> int:
    """Return 1 if a>b, 0 if equal, -1 if a<b."""
    ta, tb = _parse_version(a), _parse_version(b)
    # pad
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    if ta > tb:
        return 1
    if ta < tb:
        return -1
    return 0


def current_version() -> str:
    return APP_VERSION


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_path() -> Path | None:
    if is_frozen():
        return Path(sys.executable).resolve()
    return None


def _api_get(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"GitHub API 错误 HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 GitHub: {e.reason}") from e


def fetch_latest_release() -> dict[str, Any]:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    data = _api_get(url)
    if not isinstance(data, dict) or data.get("message") == "Not Found":
        raise RuntimeError("未找到 Release，请确认仓库已发布版本")
    return data


def _pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer PalMod.exe / 帕鲁Mod.exe, then zip with panel name."""
    if not assets:
        return None

    def score(a: dict) -> tuple:
        name = (a.get("name") or "").lower()
        # higher priority = lower score tuple
        if name == "palmod.exe":
            return (0, 0)
        if name.endswith(".exe") and "palmod" in name:
            return (0, 1)
        if name.endswith(".exe") and ("mod" in name or "panel" in name):
            return (1, 0)
        if name.endswith(".zip") and ("palmod" in name or "modmanager" in name or "管理" in (a.get("name") or "")):
            return (2, 0)
        if name.endswith(".zip"):
            return (3, 0)
        if name.endswith(".exe"):
            return (4, 0)
        return (9, 0)

    ranked = sorted(assets, key=score)
    best = ranked[0]
    if score(best)[0] >= 9:
        return None
    return best


def check_for_update() -> dict[str, Any]:
    local = current_version()
    release = fetch_latest_release()
    tag = (release.get("tag_name") or release.get("name") or "").strip()
    remote = tag.lstrip("vV") or "0"
    assets = release.get("assets") or []
    asset = _pick_asset(assets)
    cmp = compare_versions(remote, local)
    return {
        "local_version": local,
        "remote_version": remote.lstrip("v") if remote else None,
        "tag_name": tag,
        "release_name": release.get("name") or tag,
        "release_url": release.get("html_url") or "",
        "body": (release.get("body") or "")[:2000],
        "update_available": cmp > 0,
        "is_newer_remote": cmp > 0,
        "is_same": cmp == 0,
        "is_frozen": is_frozen(),
        "executable": str(executable_path()) if executable_path() else None,
        "asset": {
            "name": asset.get("name"),
            "size": asset.get("size"),
            "browser_download_url": asset.get("browser_download_url"),
            "content_type": asset.get("content_type"),
        }
        if asset
        else None,
        "github": f"{GITHUB_OWNER}/{GITHUB_REPO}",
    }


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_exe_from_zip(zip_path: Path, dest_exe: Path) -> Path:
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Prefer PalMod.exe / *Mod*.exe
        names = [n for n in zf.namelist() if n.lower().endswith(".exe") and not n.endswith("/")]
        if not names:
            raise RuntimeError("更新包 zip 内没有 .exe")
        preferred = None
        for n in names:
            base = Path(n).name.lower()
            if base == "palmod.exe" or "palmod" in base or "帕鲁" in Path(n).name:
                preferred = n
                break
        if not preferred:
            preferred = names[0]
        target = dest_exe
        with zf.open(preferred) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        return target


def prepare_update(download_url: str | None = None) -> dict[str, Any]:
    """
    Download new build next to current EXE and write a batch updater.
    Caller should exit the app after this so the batch can replace the file.
    """
    info = check_for_update()
    if not info.get("update_available") and not os.environ.get("PALMOD_FORCE_UPDATE"):
        # Allow re-download same version if forced via asset url
        if not download_url:
            raise RuntimeError("当前已是最新版本，无需更新")

    asset = info.get("asset")
    url = download_url or (asset or {}).get("browser_download_url")
    if not url:
        raise RuntimeError("Release 中没有可下载的更新文件（需要 PalMod.exe 或 zip）")

    exe = executable_path()
    if not exe or not exe.is_file():
        # Dev mode: still download for testing into data/updates
        base = Path(os.environ.get("PALMOD_DATA_DIR") or Path.cwd() / "data")
        target_exe = base / "updates" / "PalMod_update.exe"
        staging_dir = base / "updates"
    else:
        staging_dir = exe.parent / "data" / "updates"
        target_exe = exe  # final destination

    staging_dir.mkdir(parents=True, exist_ok=True)
    name = (asset or {}).get("name") or "update.bin"
    raw_path = staging_dir / name

    _download(url, raw_path)

    new_exe = staging_dir / "PalMod_new.exe"
    if raw_path.suffix.lower() == ".zip":
        _extract_exe_from_zip(raw_path, new_exe)
    elif raw_path.suffix.lower() == ".exe":
        shutil.copy2(raw_path, new_exe)
    else:
        # try as zip first
        try:
            _extract_exe_from_zip(raw_path, new_exe)
        except zipfile.BadZipFile:
            shutil.copy2(raw_path, new_exe)

    if not new_exe.is_file() or new_exe.stat().st_size < 1_000_000:
        raise RuntimeError("下载的更新文件无效或过小")

    if not exe:
        return {
            "ok": True,
            "mode": "dev_download_only",
            "new_exe": str(new_exe),
            "message": "开发模式：已下载更新文件，请手动替换。打包为 EXE 后可一键更新。",
            "version": info.get("remote_version"),
        }

    # Write updater batch (GBK-friendly simple ASCII)
    bat = staging_dir / "apply_update.bat"
    exe_name = exe.name
    # Use short paths carefully with quotes
    bat_content = f"""@echo off
chcp 65001 >nul
set "TARGET={exe}"
set "NEW={new_exe}"
set "WORKDIR={exe.parent}"
echo Updating Palworld Mod Manager...
ping 127.0.0.1 -n 3 >nul
:wait
tasklist /FI "IMAGENAME eq {exe_name}" 2>nul | find /I "{exe_name}" >nul
if not errorlevel 1 (
  ping 127.0.0.1 -n 2 >nul
  goto wait
)
copy /Y "%NEW%" "%TARGET%" >nul
if errorlevel 1 (
  echo Update failed: cannot copy file.
  pause
  exit /b 1
)
start "" "%TARGET%"
del "%NEW%" >nul 2>&1
del "%~f0" >nul 2>&1
exit /b 0
"""
    bat.write_text(bat_content, encoding="utf-8")

    # Launch updater detached (no window flash if possible)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags |= 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        cwd=str(exe.parent),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    return {
        "ok": True,
        "mode": "apply_and_restart",
        "new_exe": str(new_exe),
        "target": str(exe),
        "updater": str(bat),
        "version": info.get("remote_version"),
        "message": "更新已下载，面板即将关闭并自动替换为新版本后重启。",
        "should_exit": True,
    }
