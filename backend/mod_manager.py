"""Configuration helpers and lazy ModService facade."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .game_detector import ensure_mod_folders, get_mod_directories


def _app_dir() -> Path:
    env = os.environ.get("PALMOD_ROOT")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    env = os.environ.get("PALMOD_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


APP_DIR = _app_dir()
DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
REGISTRY_PATH = DATA_DIR / "mods_registry.json"  # migration compatibility only
_manifest_store_instance: Any | None = None
_mod_service_instance: Any | None = None


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_config() -> dict[str, Any]:
    config = _load_json(CONFIG_PATH, {})
    return config if isinstance(config, dict) else {}


def save_config(config: dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, config)


def get_game_path() -> str | None:
    value = load_config().get("game_path")
    return str(value) if value and Path(value).is_dir() else None


def set_game_path(path: str) -> dict[str, Any]:
    global _mod_service_instance, _manifest_store_instance
    result = ensure_mod_folders(path)
    config = load_config()
    config["game_path"] = str(Path(path))
    save_config(config)
    _mod_service_instance = None
    _manifest_store_instance = None
    resync_from_disk()
    return result


def get_manifest_store():
    global _manifest_store_instance
    if _manifest_store_instance is None:
        from .manifest_store import ManifestStore
        _manifest_store_instance = ManifestStore(DATA_DIR / "manifests")
    return _manifest_store_instance


def _get_mod_service(*, required: bool = True):
    global _mod_service_instance, _manifest_store_instance
    if _mod_service_instance is not None:
        return _mod_service_instance
    game = get_game_path()
    if not game:
        if required:
            raise RuntimeError("尚未设置游戏路径")
        return None
    from .mod_service import ModService
    _mod_service_instance = ModService(game, DATA_DIR)
    # Keep the compatibility accessor and service on one store instance.
    _manifest_store_instance = _mod_service_instance.store
    return _mod_service_instance


def import_mod_file(
    file_path: str | Path,
    *,
    preferred_type: str | None = None,
    display_name: str | None = None,
    nexus_id: int | None = None,
    decision: str = "cancel",
) -> dict[str, object]:
    installed = _get_mod_service().install(
        file_path,
        preferred_kind=preferred_type,
        display_name=display_name,
        nexus_id=nexus_id,
        decision=decision,
    )
    return {**installed, "ok": True, "mod": installed}


def set_mod_enabled(mod_id: str, enabled: bool) -> dict[str, object]:
    return _get_mod_service().set_enabled(mod_id, enabled)


def delete_mod(mod_id: str, force_modified: bool = False) -> dict[str, object]:
    return _get_mod_service().delete(mod_id, force_modified=force_modified)


def resync_from_disk() -> list[dict[str, object]]:
    service = _get_mod_service(required=False)
    return service.rescan() if service is not None else []


def list_mods() -> list[dict[str, object]]:
    service = _get_mod_service(required=False)
    return service.list_mods() if service is not None else []


def open_mod_folder(mod_id: str | None = None) -> str:
    game = get_game_path()
    if not game:
        raise RuntimeError("尚未设置游戏路径")
    if mod_id:
        manifest = get_manifest_store().get(mod_id)
        return str(manifest.install_root)
    return str(get_mod_directories(game)["tilde_mods"])
