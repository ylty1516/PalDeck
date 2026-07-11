"""Import, list, enable/disable Palworld mods."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .game_detector import ensure_mod_folders, get_mod_directories

def _app_dir() -> Path:
    import os
    import sys

    env = os.environ.get("PALMOD_ROOT")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    import os
    import sys

    env = os.environ.get("PALMOD_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


APP_DIR = _app_dir()
DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
REGISTRY_PATH = DATA_DIR / "mods_registry.json"

# Compatibility wiring is deliberately lazy: task 4 can ship before ModService,
# and importing this legacy facade must keep working during that transition.
_manifest_store_instance: Any | None = None
_mod_service_instance: Any | None = None


def get_manifest_store():
    global _manifest_store_instance
    if _manifest_store_instance is None:
        from .manifest_store import ManifestStore

        _manifest_store_instance = ManifestStore(DATA_DIR / "manifests")
    return _manifest_store_instance


def _get_mod_service(*, required: bool = True):
    global _mod_service_instance
    if _mod_service_instance is not None:
        return _mod_service_instance
    try:
        from .mod_service import ModService
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.mod_service":
            raise
        if required:
            raise RuntimeError("ModService is not available yet") from exc
        return None
    _mod_service_instance = ModService(get_manifest_store())
    return _mod_service_instance


@dataclass
class ManagedMod:
    id: str
    name: str
    mod_type: str  # pak | logicpak | ue4ss | workshop | loose
    enabled: bool
    install_path: str
    source_name: str
    files: list[str]
    installed_at: str
    size_bytes: int
    notes: str = ""
    nexus_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, cfg)


def get_game_path() -> str | None:
    cfg = load_config()
    path = cfg.get("game_path")
    if path and Path(path).is_dir():
        return str(path)
    return None


def set_game_path(path: str) -> dict[str, Any]:
    result = ensure_mod_folders(path)
    cfg = load_config()
    cfg["game_path"] = str(Path(path))
    save_config(cfg)
    # Re-scan filesystem into registry
    resync_from_disk()
    return result


def load_registry() -> list[ManagedMod]:
    raw = _load_json(REGISTRY_PATH, [])
    mods: list[ManagedMod] = []
    if not isinstance(raw, list):
        return mods
    for item in raw:
        try:
            mods.append(
                ManagedMod(
                    id=item["id"],
                    name=item["name"],
                    mod_type=item["mod_type"],
                    enabled=bool(item.get("enabled", True)),
                    install_path=item["install_path"],
                    source_name=item.get("source_name", ""),
                    files=list(item.get("files", [])),
                    installed_at=item.get("installed_at", ""),
                    size_bytes=int(item.get("size_bytes", 0)),
                    notes=item.get("notes", ""),
                    nexus_id=item.get("nexus_id"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return mods


def save_registry(mods: list[ManagedMod]) -> None:
    _save_json(REGISTRY_PATH, [m.to_dict() for m in mods])


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> list[str]:
    """Extract zip preventing path traversal. Returns relative file paths."""
    extracted: list[str] = []
    dest = dest.resolve()
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        # Drop absolute / drive / parent refs
        parts = [p for p in Path(name).parts if p not in ("", ".", "..")]
        if not parts:
            continue
        if parts[0].endswith(":"):
            parts = parts[1:]
        if not parts:
            continue
        target = dest.joinpath(*parts)
        try:
            target.resolve().relative_to(dest)
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        extracted.append(str(Path(*parts)).replace("\\", "/"))
    return extracted


def _detect_mod_kind(extract_root: Path) -> tuple[str, Path, str]:
    """
    Detect mod type from extracted content.
    Returns (mod_type, content_root, display_name)
    """
    # Prefer Info.json workshop package
    for info in extract_root.rglob("Info.json"):
        try:
            data = json.loads(info.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict) and (
                "PackageName" in data or "packageName" in data or "ModName" in data
            ):
                return "workshop", info.parent, info.parent.name
        except (json.JSONDecodeError, OSError):
            pass

    # UE4SS: Scripts/main.lua anywhere
    for main_lua in extract_root.rglob("main.lua"):
        if main_lua.parent.name.lower() == "scripts":
            mod_root = main_lua.parent.parent
            return "ue4ss", mod_root, mod_root.name

    # enabled.txt + Scripts is classic UE4SS
    for scripts in extract_root.rglob("Scripts"):
        if scripts.is_dir():
            return "ue4ss", scripts.parent, scripts.parent.name

    paks = list(extract_root.rglob("*.pak")) + list(extract_root.rglob("*.pak.disabled"))
    if paks:
        # Heuristic: LogicMods in path name
        for pak in paks:
            parts_lower = [p.lower() for p in pak.parts]
            if "logicmods" in parts_lower or "logic_mods" in parts_lower:
                return "logicpak", pak.parent, pak.stem.replace(".pak", "")
        # ~mods or default pak
        return "pak", paks[0].parent if len(paks) == 1 else extract_root, paks[0].stem

    # Single top-level folder
    children = [c for c in extract_root.iterdir() if not c.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return "loose", children[0], children[0].name

    return "loose", extract_root, extract_root.name


def _install_target_for(mod_type: str, game_root: Path) -> Path:
    dirs = get_mod_directories(game_root)
    if mod_type == "pak":
        return dirs["tilde_mods"]
    if mod_type == "logicpak":
        return dirs["logic_mods"]
    if mod_type == "ue4ss":
        return dirs["ue4ss_mods"]
    if mod_type == "workshop":
        return dirs["workshop"]
    # loose defaults to ~mods if has pak else ue4ss
    return dirs["tilde_mods"]


def _unique_name(base: str, existing: set[str]) -> str:
    name = base
    i = 2
    while name.lower() in existing:
        name = f"{base}_{i}"
        i += 1
    return name


def import_mod_file(
    file_path: str | Path,
    *,
    preferred_type: str | None = None,
    display_name: str | None = None,
    nexus_id: int | None = None,
) -> dict[str, Any]:
    """Import a .zip / .pak / folder into the correct Palworld mod location.

    .zip is extracted automatically — no need to unpack manually first.
    UE4SS framework packages are detected and installed into Win64.
    """
    game = get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径，请先检测或选择幻兽帕鲁安装目录")
    game_root = Path(game)
    ensure_mod_folders(game_root)

    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"文件不存在: {src}")

    # UE4SS framework zip → install into Binaries/Win64 (auto-extract)
    from . import ue4ss_installer

    if src.is_file() and src.suffix.lower() == ".zip":
        if preferred_type in (None, "auto", "ue4ss_framework") or preferred_type == "":
            if preferred_type == "ue4ss_framework" or ue4ss_installer.looks_like_ue4ss_framework(src):
                result = ue4ss_installer.install_from_zip(game_root, src)
                return {
                    "ok": True,
                    "kind": "ue4ss_framework",
                    "ue4ss": result,
                    "mod": {
                        "id": "ue4ss-framework",
                        "name": display_name or "UE4SS (RE-UE4SS)",
                        "mod_type": "ue4ss_framework",
                        "enabled": True,
                        "install_path": result.get("win64", ""),
                        "source_name": src.name,
                        "files": [],
                        "installed_at": _now_iso(),
                        "size_bytes": src.stat().st_size,
                        "notes": result.get("message", "UE4SS 框架已安装"),
                        "nexus_id": nexus_id,
                    },
                }

    temp_dir: Path | None = None
    try:
        if src.is_file() and src.suffix.lower() == ".pak":
            mod_type = preferred_type if preferred_type in ("pak", "logicpak") else "pak"
            target_dir = _install_target_for(mod_type, game_root)
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / src.name
            if dest.exists():
                dest = target_dir / f"{src.stem}_{uuid.uuid4().hex[:6]}{src.suffix}"
            shutil.copy2(src, dest)
            mod = ManagedMod(
                id=uuid.uuid4().hex,
                name=display_name or src.stem,
                mod_type=mod_type,
                enabled=True,
                install_path=str(dest),
                source_name=src.name,
                files=[dest.name],
                installed_at=_now_iso(),
                size_bytes=dest.stat().st_size,
                notes="直接导入的 .pak 文件",
                nexus_id=nexus_id,
            )
            mods = load_registry()
            mods.append(mod)
            save_registry(mods)
            _sync_ue4ss_mods_txt(game_root)
            return {"ok": True, "mod": mod.to_dict()}

        if src.is_file() and src.suffix.lower() in (".zip", ".pak.zip"):
            temp_dir = Path(tempfile.mkdtemp(prefix="palmod_"))
            with zipfile.ZipFile(src, "r") as zf:
                _safe_extract_zip(zf, temp_dir)
            content_root = temp_dir
            source_name = src.name
        elif src.is_dir():
            temp_dir = Path(tempfile.mkdtemp(prefix="palmod_"))
            # Copy folder contents
            shutil.copytree(src, temp_dir / src.name)
            content_root = temp_dir
            source_name = src.name
        else:
            raise ValueError(f"不支持的文件类型: {src.suffix or '(无扩展名)'}. 请使用 .zip 或 .pak")

        detected_type, payload_root, auto_name = _detect_mod_kind(content_root)
        mod_type = preferred_type if preferred_type else detected_type
        if preferred_type == "auto" or not preferred_type:
            mod_type = detected_type

        name = display_name or auto_name or src.stem
        target_base = _install_target_for(mod_type, game_root)
        target_base.mkdir(parents=True, exist_ok=True)

        if mod_type in ("pak", "logicpak"):
            # Copy all .pak files into target
            paks = list(payload_root.rglob("*.pak"))
            if not paks:
                paks = list(content_root.rglob("*.pak"))
            if not paks:
                raise ValueError("未在压缩包中找到 .pak 文件")
            files: list[str] = []
            primary = ""
            total = 0
            for pak in paks:
                dest = target_base / pak.name
                if dest.exists():
                    dest = target_base / f"{pak.stem}_{uuid.uuid4().hex[:6]}.pak"
                shutil.copy2(pak, dest)
                files.append(dest.name)
                primary = str(dest)
                total += dest.stat().st_size
            install_path = primary if len(files) == 1 else str(target_base)
            mod = ManagedMod(
                id=uuid.uuid4().hex,
                name=name,
                mod_type=mod_type,
                enabled=True,
                install_path=install_path,
                source_name=source_name,
                files=files,
                installed_at=_now_iso(),
                size_bytes=total,
                notes=f"安装到 {target_base.name}",
                nexus_id=nexus_id,
            )
        else:
            # Folder-based install (ue4ss / workshop / loose)
            existing = {p.name.lower() for p in target_base.iterdir()} if target_base.is_dir() else set()
            folder_name = _unique_name(re.sub(r'[<>:"/\\|?*]', "_", name), existing)
            dest_dir = target_base / folder_name
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            if payload_root.is_dir():
                shutil.copytree(payload_root, dest_dir)
            else:
                dest_dir.mkdir(parents=True)
                shutil.copy2(payload_root, dest_dir / payload_root.name)

            files = []
            for p in dest_dir.rglob("*"):
                if p.is_file():
                    files.append(str(p.relative_to(dest_dir)).replace("\\", "/"))

            # Workshop: ensure ActiveModList
            notes = f"安装到 {target_base}"
            if mod_type == "workshop":
                _ensure_workshop_active(game_root, dest_dir, enable=True)
                notes = "官方 Workshop 格式，已写入 ActiveModList"

            if mod_type == "ue4ss":
                _enable_ue4ss_mod_entry(game_root, dest_dir.name, True)
                notes = "UE4SS Lua/Script 模组"

            mod = ManagedMod(
                id=uuid.uuid4().hex,
                name=name,
                mod_type=mod_type,
                enabled=True,
                install_path=str(dest_dir),
                source_name=source_name,
                files=files,
                installed_at=_now_iso(),
                size_bytes=_dir_size(dest_dir),
                notes=notes,
                nexus_id=nexus_id,
            )

        mods = load_registry()
        mods.append(mod)
        save_registry(mods)
        _sync_ue4ss_mods_txt(game_root)
        return {"ok": True, "mod": mod.to_dict()}
    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _read_package_name(mod_dir: Path) -> str | None:
    info = mod_dir / "Info.json"
    if not info.is_file():
        return None
    try:
        data = json.loads(info.read_text(encoding="utf-8", errors="ignore"))
        return data.get("PackageName") or data.get("packageName") or data.get("ModName")
    except (json.JSONDecodeError, OSError):
        return None


def _ensure_workshop_active(game_root: Path, mod_dir: Path, enable: bool) -> None:
    dirs = get_mod_directories(game_root)
    settings = dirs["pal_mod_settings"]
    package = _read_package_name(mod_dir) or mod_dir.name
    if not settings.is_file():
        ensure_mod_folders(game_root)
    text = settings.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    key = f"ActiveModList={package}"
    has = any(ln.strip() == key or ln.strip().lower() == key.lower() for ln in lines)
    if enable and not has:
        if lines and not lines[-1].endswith("\n"):
            pass
        lines.append(key)
        settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not enable and has:
        lines = [ln for ln in lines if ln.strip().lower() != key.lower()]
        settings.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ue4ss_mods_txt_path(game_root: Path) -> Path | None:
    dirs = get_mod_directories(game_root)
    candidates = [
        dirs["ue4ss_mods"] / "mods.txt",
        dirs["win64"] / "Mods" / "mods.txt",
        dirs["win64"] / "ue4ss" / "Mods" / "mods.txt",
    ]
    for c in candidates:
        if c.is_file() or c.parent.is_dir():
            return c
    return dirs["ue4ss_mods"] / "mods.txt"


def _enable_ue4ss_mod_entry(game_root: Path, mod_name: str, enable: bool) -> None:
    path = _ue4ss_mods_txt_path(game_root)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    found = False
    new_lines: list[str] = []
    for ln in lines:
        m = re.match(r"^\s*([^:]+)\s*:\s*([01])\s*$", ln)
        if m and m.group(1).strip() == mod_name:
            new_lines.append(f"{mod_name} : {'1' if enable else '0'}")
            found = True
        else:
            new_lines.append(ln)
    if not found:
        new_lines.append(f"{mod_name} : {'1' if enable else '0'}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _sync_ue4ss_mods_txt(game_root: Path) -> None:
    """Ensure BPModLoaderMod enabled if LogicMods present; keep user mods listed."""
    dirs = get_mod_directories(game_root)
    mods_dir = dirs["ue4ss_mods"]
    if not mods_dir.is_dir():
        return
    path = _ue4ss_mods_txt_path(game_root)
    if path is None:
        return
    existing: dict[str, str] = {}
    if path.is_file():
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"^\s*([^:]+)\s*:\s*([01])\s*$", ln)
            if m:
                existing[m.group(1).strip()] = m.group(2)

    # Builtin helpers that should stay on when logic mods exist
    if any(dirs["logic_mods"].glob("*.pak")) if dirs["logic_mods"].is_dir() else False:
        existing.setdefault("BPModLoaderMod", "1")

    for child in mods_dir.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            if child.name.endswith(".disabled"):
                base = child.name[: -len(".disabled")]
                existing[base] = "0"
            else:
                existing.setdefault(child.name, "1")

    lines = [f"{k} : {v}" for k, v in existing.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _path_enabled(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".disabled"):
        return False
    if name.endswith(".pak.disabled"):
        return False
    return True


def set_mod_enabled(mod_id: str, enabled: bool) -> dict[str, Any]:
    game = get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    game_root = Path(game)
    mods = load_registry()
    target = next((m for m in mods if m.id == mod_id), None)
    if not target:
        raise KeyError("未找到该模组")

    path = Path(target.install_path)
    if target.mod_type in ("pak", "logicpak"):
        # May be single file or multi-file list under parent
        files_to_toggle: list[Path] = []
        if path.is_file() or str(path).lower().endswith(".pak") or str(path).lower().endswith(".pak.disabled"):
            files_to_toggle.append(path)
        else:
            parent = path if path.is_dir() else path.parent
            for fname in target.files:
                files_to_toggle.append(parent / fname)

        new_files: list[str] = []
        primary = None
        for f in files_to_toggle:
            # Normalize path variants
            candidates = [f]
            if f.suffix.lower() == ".disabled" and f.name.lower().endswith(".pak.disabled"):
                candidates.append(f)
            elif not f.exists() and Path(str(f) + ".disabled").exists():
                candidates = [Path(str(f) + ".disabled")]
            elif not f.exists() and f.name.endswith(".disabled"):
                candidates.append(Path(str(f)[: -len(".disabled")]))

            actual = next((c for c in candidates if c.exists()), None)
            if actual is None:
                # try sibling naming
                if Path(str(f) + ".disabled").exists():
                    actual = Path(str(f) + ".disabled")
                elif f.name.endswith(".disabled") and Path(str(f)[: -len(".disabled")]).exists():
                    actual = Path(str(f)[: -len(".disabled")])
                else:
                    continue

            if enabled:
                if actual.name.lower().endswith(".pak.disabled"):
                    new_path = actual.with_name(actual.name[: -len(".disabled")])
                    actual.rename(new_path)
                    actual = new_path
                elif actual.suffix.lower() == ".disabled":
                    new_path = actual.with_suffix("")
                    actual.rename(new_path)
                    actual = new_path
            else:
                if actual.name.lower().endswith(".pak") and not actual.name.lower().endswith(".pak.disabled"):
                    new_path = Path(str(actual) + ".disabled")
                    actual.rename(new_path)
                    actual = new_path
            new_files.append(actual.name)
            primary = actual

        if primary is not None:
            target.install_path = str(primary if len(new_files) == 1 else primary.parent)
            target.files = new_files
        target.enabled = enabled

    elif target.mod_type in ("ue4ss", "workshop", "loose"):
        if path.exists() or Path(str(path) + ".disabled").exists() or (
            path.name.endswith(".disabled") and Path(str(path)[: -len(".disabled")]).exists()
        ):
            actual = path
            if not actual.exists():
                if Path(str(path) + ".disabled").exists():
                    actual = Path(str(path) + ".disabled")
                elif path.name.endswith(".disabled"):
                    actual = Path(str(path)[: -len(".disabled")])

            if enabled and actual.name.endswith(".disabled"):
                new_path = actual.with_name(actual.name[: -len(".disabled")])
                actual.rename(new_path)
                actual = new_path
            elif not enabled and not actual.name.endswith(".disabled"):
                new_path = actual.with_name(actual.name + ".disabled")
                actual.rename(new_path)
                actual = new_path
            target.install_path = str(actual)

        if target.mod_type == "ue4ss":
            base_name = Path(target.install_path).name
            if base_name.endswith(".disabled"):
                base_name = base_name[: -len(".disabled")]
            _enable_ue4ss_mod_entry(game_root, base_name, enabled)
        if target.mod_type == "workshop":
            base = Path(target.install_path)
            if base.name.endswith(".disabled"):
                # Info.json inside disabled folder may still be readable
                pass
            live = base if not base.name.endswith(".disabled") else Path(str(base)[: -len(".disabled")])
            if live.exists() or base.exists():
                _ensure_workshop_active(game_root, live if live.exists() else base, enabled)

        target.enabled = enabled
    else:
        target.enabled = enabled

    save_registry(mods)
    _sync_ue4ss_mods_txt(game_root)
    return {"ok": True, "mod": target.to_dict()}


def delete_mod(mod_id: str) -> dict[str, Any]:
    game = get_game_path()
    game_root = Path(game) if game else None
    mods = load_registry()
    target = next((m for m in mods if m.id == mod_id), None)
    if not target:
        raise KeyError("未找到该模组")

    path = Path(target.install_path)
    # Also remove disabled variants
    candidates = [path, Path(str(path) + ".disabled")]
    if path.name.endswith(".disabled"):
        candidates.append(Path(str(path)[: -len(".disabled")]))

    for c in candidates:
        if c.is_file():
            c.unlink(missing_ok=True)
        elif c.is_dir():
            shutil.rmtree(c, ignore_errors=True)

    # Multi-file paks
    if target.mod_type in ("pak", "logicpak") and game_root:
        dirs = get_mod_directories(game_root)
        base = dirs["tilde_mods"] if target.mod_type == "pak" else dirs["logic_mods"]
        for fname in target.files:
            for variant in (base / fname, Path(str(base / fname) + ".disabled")):
                if variant.is_file():
                    variant.unlink(missing_ok=True)

    if game_root and target.mod_type == "workshop":
        _ensure_workshop_active(game_root, path, False)
    if game_root and target.mod_type == "ue4ss":
        name = path.name.replace(".disabled", "")
        txt = _ue4ss_mods_txt_path(game_root)
        if txt and txt.is_file():
            lines = [
                ln
                for ln in txt.read_text(encoding="utf-8", errors="ignore").splitlines()
                if not re.match(rf"^\s*{re.escape(name)}\s*:", ln)
            ]
            txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mods = [m for m in mods if m.id != mod_id]
    save_registry(mods)
    return {"ok": True, "deleted": mod_id}


def resync_from_disk() -> list[dict[str, Any]]:
    """Scan game mod folders and merge into registry."""
    game = get_game_path()
    if not game:
        return [m.to_dict() for m in load_registry()]
    game_root = Path(game)
    ensure_mod_folders(game_root)
    dirs = get_mod_directories(game_root)
    existing = load_registry()
    by_path = {Path(m.install_path).resolve() if Path(m.install_path).exists() else Path(m.install_path): m for m in existing}
    # Also index by file names
    known_files: set[str] = set()
    for m in existing:
        for f in m.files:
            known_files.add(f.lower())
        known_files.add(Path(m.install_path).name.lower())

    def already_tracked(path: Path) -> bool:
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if rp in by_path:
            return True
        if path.name.lower() in known_files:
            return True
        # disabled variants
        for m in existing:
            if Path(m.install_path).name.replace(".disabled", "") == path.name.replace(".disabled", ""):
                if m.mod_type in ("pak", "logicpak", "ue4ss", "workshop", "loose"):
                    return True
        return False

    new_mods: list[ManagedMod] = []

    # Scan pak folders
    for folder, mtype in ((dirs["tilde_mods"], "pak"), (dirs["logic_mods"], "logicpak")):
        if not folder.is_dir():
            continue
        for pak in list(folder.glob("*.pak")) + list(folder.glob("*.pak.disabled")):
            if already_tracked(pak):
                continue
            enabled = not pak.name.lower().endswith(".disabled")
            new_mods.append(
                ManagedMod(
                    id=uuid.uuid4().hex,
                    name=pak.name.replace(".pak.disabled", "").replace(".pak", ""),
                    mod_type=mtype,
                    enabled=enabled,
                    install_path=str(pak),
                    source_name=pak.name,
                    files=[pak.name],
                    installed_at=_now_iso(),
                    size_bytes=pak.stat().st_size,
                    notes="从游戏目录扫描发现",
                )
            )

    # UE4SS folders
    ue4ss = dirs["ue4ss_mods"]
    if ue4ss.is_dir():
        skip = {"shared", "consolecommandsmod", "consoleenablermod", "bpml_genericfunctions", "bpmodloadermod", "keybinds", "cheatmanagermod"}
        for child in ue4ss.iterdir():
            if not child.is_dir():
                continue
            base = child.name.replace(".disabled", "")
            if base.lower() in skip:
                continue
            if already_tracked(child):
                continue
            # Only track if looks like a mod (Scripts or enabled.txt)
            if not ((child / "Scripts").exists() or (child / "enabled.txt").exists() or any(child.rglob("*.lua"))):
                continue
            new_mods.append(
                ManagedMod(
                    id=uuid.uuid4().hex,
                    name=base,
                    mod_type="ue4ss",
                    enabled=not child.name.endswith(".disabled"),
                    install_path=str(child),
                    source_name=child.name,
                    files=[str(p.relative_to(child)).replace("\\", "/") for p in child.rglob("*") if p.is_file()],
                    installed_at=_now_iso(),
                    size_bytes=_dir_size(child),
                    notes="从 UE4SS Mods 扫描发现",
                )
            )

    # Workshop
    workshop = dirs["workshop"]
    if workshop.is_dir():
        for child in workshop.iterdir():
            if not child.is_dir():
                continue
            if already_tracked(child):
                continue
            if not (child / "Info.json").is_file() and not (Path(str(child).replace(".disabled", "")) / "Info.json").exists():
                # still allow folders without info
                if not any(child.rglob("*")):
                    continue
            new_mods.append(
                ManagedMod(
                    id=uuid.uuid4().hex,
                    name=child.name.replace(".disabled", ""),
                    mod_type="workshop",
                    enabled=not child.name.endswith(".disabled"),
                    install_path=str(child),
                    source_name=child.name,
                    files=[str(p.relative_to(child)).replace("\\", "/") for p in child.rglob("*") if p.is_file()][:200],
                    installed_at=_now_iso(),
                    size_bytes=_dir_size(child),
                    notes="从官方 Workshop 目录扫描发现",
                )
            )

    if new_mods:
        existing.extend(new_mods)
        save_registry(existing)

    # Refresh enabled state from disk for known mods
    changed = False
    for m in existing:
        p = Path(m.install_path)
        alt = Path(str(p) + ".disabled") if not p.name.endswith(".disabled") else Path(str(p)[: -len(".disabled")])
        if p.exists():
            m.enabled = _path_enabled(p)
        elif alt.exists():
            m.install_path = str(alt)
            m.enabled = _path_enabled(alt)
            changed = True
    if changed or new_mods:
        save_registry(existing)

    return [m.to_dict() for m in existing]


def list_mods() -> list[dict[str, Any]]:
    if get_game_path():
        return resync_from_disk()
    return [m.to_dict() for m in load_registry()]


def open_mod_folder(mod_id: str | None = None) -> str:
    game = get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    if mod_id:
        mods = load_registry()
        m = next((x for x in mods if x.id == mod_id), None)
        if not m:
            raise KeyError("未找到模组")
        p = Path(m.install_path)
        if p.is_file():
            return str(p.parent)
        return str(p if p.exists() else p.parent)
    dirs = get_mod_directories(game)
    return str(dirs["tilde_mods"])
