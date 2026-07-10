"""Discover and edit adjustable values for installed mods (palmod_config.json)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .game_detector import get_mod_directories
from . import mod_manager


def _ue4ss_mod_roots(game_root: Path) -> list[Path]:
    dirs = get_mod_directories(game_root)
    roots: list[Path] = []
    for key in ("ue4ss_mods",):
        p = dirs.get(key)
        if p and Path(p).is_dir():
            roots.append(Path(p))
    win64 = dirs.get("win64")
    if win64:
        win64 = Path(win64)
        for extra in (win64 / "Mods", win64 / "ue4ss" / "Mods"):
            if extra.is_dir() and extra not in roots:
                roots.append(extra)
    return roots


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sync_config_lua(mod_dir: Path, values: dict[str, Any]) -> None:
    """Write Scripts/config.lua from values for UE4SS require('config')."""
    scripts = mod_dir / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    lines = [
        "-- Auto-synced by Palworld Mod Manager — do not hand-edit if using the panel.",
        "return {",
    ]
    for k, v in values.items():
        if isinstance(v, bool):
            lines.append(f"    {k} = {'true' if v else 'false'},")
        elif isinstance(v, (int, float)):
            lines.append(f"    {k} = {v},")
        elif isinstance(v, str):
            safe = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'    {k} = "{safe}",')
        else:
            lines.append(f"    -- skipped unsupported value for {k}")
    lines.append("}")
    lines.append("")
    (scripts / "config.lua").write_text("\n".join(lines), encoding="utf-8")


def _read_values(mod_dir: Path, schema: dict[str, Any]) -> dict[str, Any]:
    fields = schema.get("fields") or []
    defaults = {f["key"]: f.get("default") for f in fields if "key" in f}
    values = dict(defaults)

    cfg_path = mod_dir / "config.json"
    if cfg_path.is_file():
        try:
            raw = _load_json(cfg_path)
            if isinstance(raw, dict):
                for k in defaults:
                    if k in raw:
                        values[k] = raw[k]
        except (json.JSONDecodeError, OSError):
            pass
    return values


def _find_schema_dirs(game_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for root in _ue4ss_mod_roots(game_root):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.endswith(".disabled"):
                continue
            schema_path = child / "palmod_config.json"
            if not schema_path.is_file():
                continue
            try:
                schema = _load_json(schema_path)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(schema, dict) or "fields" not in schema:
                continue
            found.append((child, schema))
    return found


def list_configurable_mods() -> list[dict[str, Any]]:
    game = mod_manager.get_game_path()
    if not game:
        return []
    out: list[dict[str, Any]] = []
    for mod_dir, schema in _find_schema_dirs(Path(game)):
        values = _read_values(mod_dir, schema)
        out.append(
            {
                "mod_folder": mod_dir.name.replace(".disabled", ""),
                "install_path": str(mod_dir),
                "schema": schema,
                "values": values,
                "enabled": not mod_dir.name.endswith(".disabled"),
            }
        )
    return out


def get_mod_config(mod_folder: str) -> dict[str, Any]:
    game = mod_manager.get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    for mod_dir, schema in _find_schema_dirs(Path(game)):
        name = mod_dir.name.replace(".disabled", "")
        if name.lower() == mod_folder.lower() or mod_dir.name.lower() == mod_folder.lower():
            values = _read_values(mod_dir, schema)
            return {
                "mod_folder": name,
                "install_path": str(mod_dir),
                "schema": schema,
                "values": values,
            }
    raise KeyError(f"未找到可配置模组: {mod_folder}")


def _coerce_value(field: dict[str, Any], raw: Any) -> Any:
    ftype = (field.get("type") or "string").lower()
    if ftype == "int":
        v = int(raw)
        if "min" in field:
            v = max(int(field["min"]), v)
        if "max" in field:
            v = min(int(field["max"]), v)
        return v
    if ftype == "float" or ftype == "number":
        v = float(raw)
        if "min" in field:
            v = max(float(field["min"]), v)
        if "max" in field:
            v = min(float(field["max"]), v)
        return v
    if ftype == "bool" or ftype == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        return s in ("1", "true", "yes", "on")
    return str(raw)


def update_mod_config(mod_folder: str, updates: dict[str, Any]) -> dict[str, Any]:
    game = mod_manager.get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    target_dir = None
    schema = None
    for mod_dir, sch in _find_schema_dirs(Path(game)):
        name = mod_dir.name.replace(".disabled", "")
        if name.lower() == mod_folder.lower() or mod_dir.name.lower() == mod_folder.lower():
            target_dir = mod_dir
            schema = sch
            break
    if not target_dir or not schema:
        raise KeyError(f"未找到可配置模组: {mod_folder}")

    fields = {f["key"]: f for f in (schema.get("fields") or []) if "key" in f}
    values = _read_values(target_dir, schema)
    for k, raw in updates.items():
        if k not in fields:
            continue
        values[k] = _coerce_value(fields[k], raw)

    _write_json(target_dir / "config.json", values)
    _sync_config_lua(target_dir, values)

    return {
        "mod_folder": target_dir.name.replace(".disabled", ""),
        "install_path": str(target_dir),
        "schema": schema,
        "values": values,
        "message": "配置已保存。请重新进入游戏/存档后生效。",
    }


def install_bundled_mod(mod_name: str = "ConfigurableBagExpand") -> dict[str, Any]:
    """Copy bundled mod into game UE4SS Mods and enable in mods.txt."""
    game = mod_manager.get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    game_root = Path(game)

    # bundled path: next to package or frozen resources
    import os
    import sys

    candidates = [
        Path(__file__).resolve().parent.parent / "bundled_mods" / mod_name,
    ]
    env_root = os.environ.get("PALMOD_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root) / "bundled_mods" / mod_name)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.insert(0, Path(meipass) / "bundled_mods" / mod_name)
        candidates.insert(0, Path(sys.executable).resolve().parent / "bundled_mods" / mod_name)

    src = next((p for p in candidates if p.is_dir()), None)
    if not src:
        raise FileNotFoundError(f"找不到内置模组: {mod_name}")

    dirs = get_mod_directories(game_root)
    # Prefer modern ue4ss/Mods if present
    win64 = Path(dirs["win64"])
    if (win64 / "ue4ss" / "Mods").is_dir() or (win64 / "ue4ss").is_dir():
        dest_root = win64 / "ue4ss" / "Mods"
    else:
        dest_root = Path(dirs["ue4ss_mods"])
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / mod_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    # ensure config.lua synced
    schema_path = dest / "palmod_config.json"
    if schema_path.is_file():
        schema = _load_json(schema_path)
        values = _read_values(dest, schema)
        _write_json(dest / "config.json", values)
        _sync_config_lua(dest, values)

    # enable in mods.txt
    for mods_txt in (dest_root / "mods.txt", win64 / "Mods" / "mods.txt"):
        if not mods_txt.parent.is_dir():
            continue
        lines: list[str] = []
        if mods_txt.is_file():
            lines = mods_txt.read_text(encoding="utf-8", errors="ignore").splitlines()
        found = False
        new_lines = []
        for ln in lines:
            if re.match(rf"^\s*{re.escape(mod_name)}\s*:", ln, re.I):
                new_lines.append(f"{mod_name} : 1")
                found = True
            else:
                new_lines.append(ln)
        if not found:
            new_lines.append(f"{mod_name} : 1")
        mods_txt.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # enabled.txt
    (dest / "enabled.txt").write_text("", encoding="utf-8")

    return {
        "ok": True,
        "mod_folder": mod_name,
        "install_path": str(dest),
        "config": get_mod_config(mod_name),
        "message": f"已安装 {mod_name} 到 UE4SS Mods",
    }
