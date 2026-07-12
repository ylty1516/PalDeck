"""Flask application factory and hardened loopback API."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

from backend import game_detector, nexus_api, process_utils, self_updater, smoke_check, ue4ss_installer
from backend.appearance import AppearanceService
from backend.game_lock import game_write_lock
from backend.mod_service import GameRunningError, ModConflictError, ModifiedFilesError, ModService
from backend.steam_workshop import SteamWorkshopService, WorkshopDependencyError, WorkshopNotFoundError
from backend.storage import JsonStore
from backend.ue4ss_provider import Ue4ssProvider
from backend.version import APP_VERSION

COOKIE_NAME = "paldeck_session"
UPLOAD_TTL_SECONDS = 15 * 60
MAX_UPLOAD_BYTES = 2 * 1024**3
MAX_PENDING_ITEMS = 8
MAX_PENDING_BYTES = 4 * 1024**3
WORKSHOP_ID = re.compile(r"[1-9][0-9]{0,19}\Z")


def _resolve_root() -> Path:
    configured = os.environ.get("PALMOD_ROOT")
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


class ApiError(Exception):
    def __init__(self, message: str, status: int, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or {}


def create_app(
    *,
    root: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    session_token: str | None = None,
    testing: bool = False,
) -> Flask:
    """Build an isolated application. Authentication is never disabled in tests."""
    app_root = Path(root) if root is not None else _resolve_root()
    writable = Path(data_dir) if data_dir is not None else Path(
        os.environ.get("PALMOD_DATA_DIR", app_root / "data")
    )
    token = session_token or secrets.token_urlsafe(32)
    static_dir = app_root / "frontend"
    if not static_dir.is_dir():
        static_dir = _resolve_root() / "frontend"

    app = Flask(__name__, static_folder=str(static_dir), static_url_path="")
    app.config.update(
        TESTING=testing,
        DATA_DIR=str(writable),
        SESSION_TOKEN=token,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        PENDING_MAX_ITEMS=MAX_PENDING_ITEMS,
        PENDING_MAX_TOTAL_BYTES=MAX_PENDING_BYTES,
        OPEN_FOLDER=getattr(os, "startfile", None),
        OPEN_URL=webbrowser.open,
        EXIT_PROCESS=os._exit,
        UPDATE_EXIT_DELAY=1.2,
    )
    writable.mkdir(parents=True, exist_ok=True)
    default_background = app_root / "assets" / "default-background.webp"
    if not default_background.is_file():
        default_background = _resolve_root() / "assets" / "default-background.webp"
    appearance = AppearanceService(writable, default_background)
    app.extensions["appearance_service"] = appearance
    app.extensions["nexus_catalog"] = nexus_api.NexusCatalog(writable / "nexus-cache")
    app.extensions["ue4ss_provider"] = Ue4ssProvider(resource_root=app_root)
    app.extensions["ue4ss_cache"] = writable / "ue4ss-cache"
    app.extensions["ue4ss_pending_lock"] = threading.Lock()

    game_path = os.environ.get("PALMOD_GAME_PATH")
    if not game_path:
        config_path = writable / "config.json"
        if config_path.is_file():
            try:
                import json
                candidate = json.loads(config_path.read_text(encoding="utf-8")).get("game_path")
                if candidate and Path(candidate).is_dir():
                    game_path = str(candidate)
            except (OSError, ValueError, AttributeError):
                pass
    if game_path:
        app.extensions["mod_service"] = ModService(game_path, writable)
        app.extensions["workshop_service"] = SteamWorkshopService(
            game_root=game_path, lock_root=writable,
        )
    pending: dict[str, dict[str, Any]] = {}
    pending_lock = threading.Lock()
    app.extensions["pending_uploads"] = pending
    app.extensions["pending_uploads_lock"] = pending_lock
    upload_dir = writable / "uploads"

    def cleanup_pending_uploads(*, remove_all_orphans: bool = False) -> None:
        now = time.time()
        with pending_lock:
            for upload_token, item in list(pending.items()):
                if item["expires"] <= now or not Path(item["path"]).is_file():
                    Path(item["path"]).unlink(missing_ok=True)
                    pending.pop(upload_token, None)
            tracked = {Path(item["path"]).resolve() for item in pending.values()}
        if not upload_dir.is_dir():
            return
        for child in upload_dir.iterdir():
            try:
                if child.resolve() in tracked:
                    continue
                expired = now - child.stat().st_mtime >= UPLOAD_TTL_SECONDS
                if remove_all_orphans or expired:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            except OSError:
                continue

    cleanup_pending_uploads(remove_all_orphans=True)

    if not testing:
        def periodic_cleanup() -> None:
            while True:
                time.sleep(60)
                cleanup_pending_uploads()

        threading.Thread(target=periodic_cleanup, daemon=True).start()

    def success(data: Any = None):
        return jsonify({"ok": True, "data": data})

    def failure(message: str, status: int, code: str, details: dict[str, Any] | None = None):
        return jsonify({
            "ok": False,
            "error": message,
            "error_code": code,
            "details": details or {},
        }), status

    def service(required: bool = True) -> ModService | None:
        current = app.extensions.get("mod_service")
        if current is None and required:
            raise ApiError("尚未设置游戏路径", 400, "game_not_configured")
        return current

    def workshop_service(required: bool = True) -> SteamWorkshopService | None:
        current = app.extensions.get("workshop_service")
        if current is None and required:
            raise ApiError("尚未设置游戏路径", 400, "game_not_configured")
        return current

    @app.before_request
    def require_session():
        cleanup_pending_uploads()
        if not request.path.startswith("/api/") or request.path == "/api/health":
            return None
        supplied = request.cookies.get(COOKIE_NAME, "")
        if not supplied or not secrets.compare_digest(supplied, token):
            return failure("会话无效", 403, "invalid_session")
        return None

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return failure(exc.message, exc.status, exc.code, exc.details)

    @app.errorhandler(ModConflictError)
    def handle_conflict(exc: ModConflictError):
        return failure("模组文件冲突", 409, "mod_conflict", exc.details)

    @app.errorhandler(WorkshopNotFoundError)
    def handle_workshop_not_found(_exc: WorkshopNotFoundError):
        return failure("未找到该 Workshop 模组", 404, "workshop_mod_not_found")

    @app.errorhandler(WorkshopDependencyError)
    def handle_workshop_dependency(exc: WorkshopDependencyError):
        return failure(
            "Workshop 模组依赖冲突", 409,
            "workshop_dependency_conflict", exc.details,
        )

    @app.errorhandler(ModifiedFilesError)
    def handle_modified_files(exc: ModifiedFilesError):
        return failure("模组文件已修改，确认后可强制删除", 409, "modified_files", exc.details)

    @app.errorhandler(GameRunningError)
    @app.errorhandler(ue4ss_installer.Ue4ssGameRunningError)
    def handle_game_running(_exc: Exception):
        return failure("幻兽帕鲁正在运行，无法修改模组", 423, "game_running")

    @app.errorhandler(ue4ss_installer.Ue4ssConflictError)
    def handle_ue4ss_conflict(exc: ue4ss_installer.Ue4ssConflictError):
        return failure("检测到已有 UE4SS 安装，确认后可替换", 409, "ue4ss_conflict", exc.details)

    @app.errorhandler(PermissionError)
    def handle_permission(_exc: PermissionError):
        return failure("没有执行此操作所需的文件权限", 403, "permission_denied")

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_exc: RequestEntityTooLarge):
        return failure("上传文件过大", 413, "upload_too_large")

    @app.errorhandler(ValueError)
    @app.errorhandler(FileNotFoundError)
    def handle_bad_input(_exc: Exception):
        return failure("输入无效或请求的文件不存在", 400, "invalid_input")

    @app.errorhandler(KeyError)
    def handle_missing(_exc: KeyError):
        return failure("请求的对象不存在", 404, "not_found")

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        return failure("请求的资源不存在" if exc.code == 404 else "请求失败", exc.code or 500, "not_found" if exc.code == 404 else "http_error")

    @app.errorhandler(Exception)
    def handle_internal(exc: Exception):
        app.logger.error("Unhandled API error", exc_info=exc)
        return failure("内部操作失败", 500, "internal_error")

    @app.get("/")
    def index():
        supplied = request.args.get("token", "")
        cookie = request.cookies.get(COOKIE_NAME, "")
        if supplied:
            if not secrets.compare_digest(supplied, token):
                return failure("会话无效", 403, "invalid_session")
            response = redirect("/", code=302)
            response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Strict", path="/")
            return response
        if not cookie or not secrets.compare_digest(cookie, token):
            return failure("会话无效", 403, "invalid_session")
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/health")
    def health():
        return success({"status": "up", "version": APP_VERSION, "frozen": self_updater.is_frozen()})

    @app.get("/api/appearance")
    def get_appearance():
        return success(appearance.get_settings())

    @app.post("/api/appearance")
    def update_appearance():
        body = request.get_json(silent=True)
        if body is None:
            raise ApiError("请提供外观设置", 400, "invalid_input")
        return success(appearance.update_settings(body))

    @app.post("/api/appearance/background")
    def upload_background():
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            raise ApiError("请选择背景图片", 400, "missing_file")
        suffix = Path(uploaded.filename).suffix.casefold()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ApiError("仅支持 PNG、JPEG 和 WEBP 图片", 400, "invalid_filename")
        temporary_dir = writable / "uploads"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / f"appearance-{uuid.uuid4().hex}{suffix}"
        uploaded.save(temporary)
        try:
            appearance.set_background(temporary, declared_mime=uploaded.mimetype)
            return success(appearance.get_settings())
        finally:
            temporary.unlink(missing_ok=True)

    @app.delete("/api/appearance/background")
    def reset_background():
        return success(appearance.reset_background())

    @app.get("/api/appearance/background/current")
    def current_background():
        handle, filename = appearance.open_current_background()
        try:
            response = send_file(
                handle, download_name=filename, conditional=False, max_age=0,
            )
        except Exception:
            handle.close()
            raise
        response.call_on_close(handle.close)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/api/mods")
    def list_mods():
        current = service(required=False)
        workshop = workshop_service(required=False)
        local_mods = current.list_mods() if current else []
        workshop_mods = workshop.list_mods() if workshop else []
        return success([*local_mods, *workshop_mods])

    @app.get("/api/workshop/mods")
    def list_workshop_mods():
        return success(workshop_service().list_mods())

    @app.post("/api/workshop/rescan")
    def rescan_workshop_mods():
        if request.get_json(silent=True) != {}:
            raise ApiError("Workshop 重扫请求不接受参数", 400, "invalid_input")
        return success(workshop_service().list_mods(force=True))

    def workshop_toggle_body() -> bool:
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not set(body) <= {"confirm_dependents"}:
            raise ApiError("Workshop 启停仅接受 confirm_dependents", 400, "invalid_input")
        confirm = body.get("confirm_dependents", False)
        if type(confirm) is not bool:
            raise ApiError("confirm_dependents 必须是布尔值", 400, "invalid_input")
        return confirm

    def validate_workshop_id(workshop_id: str) -> str:
        if WORKSHOP_ID.fullmatch(workshop_id) is None:
            raise ApiError("Workshop ID 必须是正十进制字符串", 400, "invalid_input")
        return workshop_id

    def workshop_record(workshop_id: str) -> dict[str, object]:
        validated = validate_workshop_id(workshop_id)
        matches = [
            item for item in workshop_service().list_mods()
            if item.get("workshop_id") == validated
        ]
        if len(matches) != 1:
            raise ApiError("未找到该 Workshop 模组", 404, "workshop_mod_not_found")
        return matches[0]

    @app.post("/api/workshop/<workshop_id>/enable")
    @app.post("/api/workshop/<workshop_id>/disable")
    def toggle_workshop_mod(workshop_id: str):
        confirm = workshop_toggle_body()
        validate_workshop_id(workshop_id)
        enabled = request.path.endswith("/enable")

        def reject_manual_ue4ss(target) -> None:
            is_ue4ss = any(
                str(kind).casefold() == "ue4ss" for kind in target.install_types
            )
            if enabled and is_ue4ss and ue4ss_installer.status(service().game_root)["installed"]:
                raise ApiError(
                    "请先移除手动或内置 UE4SS，再启用 Workshop UE4SS",
                    409, "ue4ss_conflict", {"reason": "manual_ue4ss_installed"},
                )

        workshop = workshop_service()
        operation = workshop.set_enabled(
            workshop_id, enabled, confirm_dependents=confirm,
            conflict_validator=reject_manual_ue4ss,
        )
        settings_parent = Path(workshop.settings_path).parent
        safe_cleanup: list[str] = []
        for value in operation.get("cleanup_pending", []):
            candidate = Path(str(value))
            name = candidate.name
            if (
                candidate.parent == settings_parent
                and name.startswith(".")
                and "PalModSettings.ini" in name
                and name.endswith(".quarantine")
            ):
                safe_cleanup.append(str(candidate))
        return success({
            "mods": workshop.list_mods(),
            "changed_ids": list(operation.get("changed_ids", [workshop_id])),
            "cleanup_pending": safe_cleanup,
        })

    @app.post("/api/workshop/<workshop_id>/open-page")
    def open_workshop_page(workshop_id: str):
        if request.get_json(silent=True) != {}:
            raise ApiError("打开 Workshop 页面请求不接受参数", 400, "invalid_input")
        item = workshop_record(workshop_id)
        trusted_id = str(item["workshop_id"])
        validate_workshop_id(trusted_id)
        opener = app.config.get("OPEN_URL")
        if not callable(opener):
            raise ApiError("当前系统不支持打开网页", 500, "open_url_unavailable")
        opener(f"https://steamcommunity.com/sharedfiles/filedetails/?id={trusted_id}")
        return success({"opened": True})

    @app.get("/api/workshop/<workshop_id>/open-folder")
    def open_workshop_folder(workshop_id: str):
        item = workshop_record(workshop_id)
        opener = app.config.get("OPEN_FOLDER")
        if not callable(opener):
            raise ApiError("当前系统不支持打开目录", 500, "open_folder_unavailable")
        path = str(item["source_dir"])
        opener(path)
        return success({"path": path})

    @app.post("/api/mods/import")
    def import_mod():
        current = service()
        body = request.get_json(silent=True) or {}
        retry_token = body.get("upload_token")
        retained = False
        dest: Path | None = None
        if retry_token:
            decision = body.get("decision", "cancel")
            with pending_lock:
                item = pending.pop(str(retry_token), None)
                if not item:
                    raise ApiError("上传暂存已过期", 410, "upload_expired")
                if decision == "cancel":
                    Path(item["path"]).unlink(missing_ok=True)
                    return success({"cancelled": True})
            dest = Path(item["path"])
            options = item["options"]
        elif "file" in request.files:
            uploaded = request.files["file"]
            if not uploaded or not uploaded.filename:
                raise ApiError("未选择文件", 400, "missing_file")
            filename = secure_filename(Path(uploaded.filename).name)
            if not filename:
                raise ApiError("文件名无效", 400, "invalid_filename")
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / f"{uuid.uuid4().hex}-{filename}"
            uploaded.save(dest)
            file_size = dest.stat().st_size
            if file_size > int(app.config["MAX_CONTENT_LENGTH"]):
                dest.unlink(missing_ok=True)
                raise ApiError("上传文件过大", 413, "upload_too_large")
            nexus = request.form.get("nexus_id")
            if nexus and not nexus.isdigit():
                dest.unlink(missing_ok=True)
                raise ApiError("nexus_id 必须是整数", 400, "invalid_input")
            options = {
                "preferred_kind": None if request.form.get("type", "auto") == "auto" else request.form.get("type"),
                "display_name": request.form.get("name") or None,
                "nexus_id": int(nexus) if nexus else None,
            }
            decision = request.form.get("decision", "cancel")
        else:
            path = body.get("path")
            if not path:
                raise ApiError("请上传文件或提供本地路径", 400, "missing_file")
            dest = Path(path)
            options = {
                "preferred_kind": None if body.get("type", "auto") == "auto" else body.get("type"),
                "display_name": body.get("name"),
                "nexus_id": body.get("nexus_id"),
            }
            decision = body.get("decision", "cancel")
        try:
            return success(current.install(dest, decision=decision, **options))
        except ModConflictError as exc:
            if dest is not None and (retry_token or dest.parent == upload_dir):
                new_token = secrets.token_urlsafe(24)
                with pending_lock:
                    if len(pending) >= int(app.config["PENDING_MAX_ITEMS"]):
                        raise ApiError("待处理上传过多", 429, "pending_upload_limit") from exc
                    pending_bytes = sum(
                        Path(item["path"]).stat().st_size
                        for item in pending.values()
                        if Path(item["path"]).is_file()
                    )
                    if pending_bytes + dest.stat().st_size > int(app.config["PENDING_MAX_TOTAL_BYTES"]):
                        raise ApiError("待处理上传总量超限", 429, "pending_upload_quota") from exc
                    pending[new_token] = {
                        "path": str(dest), "options": options,
                        "expires": time.time() + UPLOAD_TTL_SECONDS,
                    }
                retained = True
                raise ModConflictError({**exc.details, "upload_token": new_token}) from exc
            raise
        finally:
            if dest is not None and dest.parent == upload_dir and not retained:
                dest.unlink(missing_ok=True)

    @app.get("/api/mods/open-folder")
    def open_mod_folder():
        current = service()
        mod_id = request.args.get("id") or None
        try:
            folder = current.folder_for(mod_id)
        except KeyError as exc:
            raise ApiError("未找到该模组", 404, "mod_not_found") from exc
        opener = app.config.get("OPEN_FOLDER")
        if not callable(opener):
            raise ApiError("当前系统不支持打开目录", 500, "open_folder_unavailable")
        path = str(folder)
        opener(path)
        return success({"path": path})

    @app.post("/api/mods/<mod_id>/toggle")
    def toggle_mod(mod_id: str):
        body = request.get_json(silent=True) or {}
        if type(body.get("enabled")) is not bool:
            raise ApiError("缺少布尔型 enabled 字段", 400, "invalid_input")
        return success(service().set_enabled(mod_id, body["enabled"]))

    @app.delete("/api/mods/<mod_id>")
    def delete_mod(mod_id: str):
        force = request.args.get("force_modified", "false").casefold() == "true"
        return success(service().delete(mod_id, force_modified=force))

    @app.post("/api/mods/resync")
    def resync_mods():
        current = service(required=False)
        return success(current.rescan() if current else [])

    @app.get("/api/game/detect")
    def detect_game():
        current = service(required=False)
        return success({"installs": game_detector.find_palworld_installs(), "current": str(current.game_root) if current else None})

    @app.post("/api/game/set")
    def set_game():
        body = request.get_json(silent=True) or {}
        path = str(body.get("path", "")).strip()
        if not path:
            raise ApiError("请提供游戏路径", 400, "invalid_input")
        info = game_detector.validate_game_path(path)
        game_detector.ensure_mod_folders(path)
        config_store = JsonStore(writable / "config.json")

        def save_game_path(config):
            if not isinstance(config, dict):
                config = {}
            config["game_path"] = path
            return config

        config_store.update(save_game_path)
        app.extensions["mod_service"] = ModService(path, writable)
        app.extensions["workshop_service"] = SteamWorkshopService(
            game_root=path, lock_root=writable,
        )
        return success(info)

    @app.get("/api/game/status")
    def game_status():
        current = service(required=False)
        if current is None:
            return success({"configured": False, "path": None})
        return success({"configured": True, **game_detector.validate_game_path(current.game_root)})

    @app.post("/api/game/ensure-folders")
    def ensure_folders():
        return success(game_detector.ensure_mod_folders(service().game_root))

    def ue4ss_install_body() -> bool:
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not set(body) <= {"confirm_replace"}:
            raise ApiError("UE4SS 安装请求仅接受 confirm_replace", 400, "invalid_input")
        confirm = body.get("confirm_replace", False)
        if not isinstance(confirm, bool):
            raise ApiError("confirm_replace 必须是布尔值", 400, "invalid_input")
        return confirm

    def serialized_asset(asset):
        return asdict(asset) if asset is not None else None

    def ue4ss_game_lock(_game_root: Path | str):
        return game_write_lock(writable)

    def reject_active_workshop_ue4ss() -> None:
        workshop = workshop_service(required=False)
        active = workshop.active_ue4ss_mods() if workshop else []
        if active:
            raise ApiError(
                "请先禁用 Workshop UE4SS，再安装手动或内置 UE4SS",
                409, "ue4ss_conflict",
                {
                    "reason": "workshop_ue4ss_active",
                    "workshop_ids": [item["workshop_id"] for item in active],
                },
            )

    @app.get("/api/ue4ss/status")
    def ue4ss_status():
        result = ue4ss_installer.status(service().game_root)
        bundled = app.extensions["ue4ss_provider"].bundled_status()
        result["bundled"] = {
            **bundled,
            "asset": serialized_asset(bundled.get("asset")),
        }
        return success(result)

    @app.post("/api/ue4ss/install-bundled")
    def ue4ss_bundled():
        confirm = ue4ss_install_body()
        current = service()
        provider = app.extensions["ue4ss_provider"]
        with ue4ss_game_lock(current.game_root):
            reject_active_workshop_ue4ss()
            try:
                payload = provider.bundled_archive()
            except Exception as exc:
                app.logger.error("Bundled UE4SS validation failed", exc_info=exc)
                raise ApiError(
                    "内置 UE4SS 资源损坏或不可用", 500, "bundled_resource_invalid"
                ) from exc
            return success(ue4ss_installer.install_from_bytes(
                current.game_root, payload, confirm_replace=confirm,
                require_palworld_layout=True,
            ))

    @app.post("/api/ue4ss/check-upstream")
    def ue4ss_check_upstream():
        body = request.get_json(silent=True)
        if body != {}:
            raise ApiError("检查请求不接受参数", 400, "invalid_input")
        try:
            checked = app.extensions["ue4ss_provider"].check_upstream()
        except Exception as exc:
            app.logger.warning("UE4SS upstream check failed", exc_info=exc)
            return failure("无法检查 UE4SS 上游版本", 502, "upstream_error")
        with app.extensions["ue4ss_pending_lock"]:
            app.extensions["ue4ss_pending_asset"] = {
                "asset": checked["asset"],
                "expires": time.time() + 600,
            }
        return success({
            "asset": serialized_asset(checked["asset"]),
            "update_available": checked["update_available"],
        })

    @app.post("/api/ue4ss/install-upstream")
    def ue4ss_install_upstream():
        confirm = ue4ss_install_body()
        current = service()
        with ue4ss_game_lock(current.game_root):
            reject_active_workshop_ue4ss()
            with app.extensions["ue4ss_pending_lock"]:
                pending_asset = app.extensions.pop("ue4ss_pending_asset", None)
            if pending_asset is None:
                raise ApiError("请先检查 UE4SS 上游版本", 409, "ue4ss_check_required")
            expires = pending_asset["expires"]
            if expires <= time.time():
                raise ApiError("UE4SS 检查结果已过期，请重新检查", 410, "ue4ss_check_expired")
            provider = app.extensions["ue4ss_provider"]
            cache_dir = Path(app.extensions["ue4ss_cache"])
            cache_dir.mkdir(parents=True, exist_ok=True)
            archive = cache_dir / f"{uuid.uuid4().hex}.zip"
            try:
                try:
                    provider.download_verified(pending_asset["asset"], archive)
                except Exception as exc:
                    app.logger.warning("UE4SS upstream download failed", exc_info=exc)
                    return failure("UE4SS 下载或校验失败", 502, "upstream_error")
                if expires <= time.time():
                    raise ApiError(
                        "UE4SS 检查结果已过期，请重新检查", 410,
                        "ue4ss_check_expired",
                    )
                try:
                    result = ue4ss_installer.install_from_zip(
                        current.game_root, archive, confirm_replace=confirm,
                        require_palworld_layout=True,
                    )
                except ue4ss_installer.Ue4ssConflictError:
                    with app.extensions["ue4ss_pending_lock"]:
                        app.extensions.setdefault("ue4ss_pending_asset", pending_asset)
                    raise
            finally:
                archive.unlink(missing_ok=True)
            return success(result)

    @app.post("/api/ue4ss/install-zip")
    def ue4ss_zip():
        current = service()
        if set(request.files) != {"file"} or len(request.files.getlist("file")) != 1:
            raise ApiError("UE4SS 本地安装仅接受一个 file", 400, "invalid_input")
        if not set(request.form) <= {"confirm_replace"} or len(
            request.form.getlist("confirm_replace")
        ) > 1:
            raise ApiError("UE4SS 本地安装包含额外或重复字段", 400, "invalid_input")
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            raise ApiError("请上传 UE4SS 的 .zip 文件", 400, "missing_file")
        name = secure_filename(Path(uploaded.filename).name)
        if not name or Path(name).suffix.casefold() != ".zip":
            raise ApiError("仅支持 .zip 文件", 400, "invalid_filename")
        confirm_raw = request.form.get("confirm_replace", "false").casefold()
        if confirm_raw not in {"true", "false"}:
            raise ApiError("confirm_replace 必须是布尔值", 400, "invalid_input")
        upload_dir = writable / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{uuid.uuid4().hex}-{name}"
        uploaded.save(dest)
        try:
            with ue4ss_game_lock(current.game_root):
                reject_active_workshop_ue4ss()
                return success(ue4ss_installer.install_from_zip(
                    current.game_root, dest, confirm_replace=confirm_raw == "true"
                ))
        finally:
            dest.unlink(missing_ok=True)

    def upstream(call):
        try:
            return success(call())
        except Exception as exc:
            app.logger.warning("Upstream request failed", exc_info=exc)
            return failure("远程服务请求失败", 502, "upstream_error")

    @app.get("/api/update/check")
    def update_check():
        return upstream(self_updater.check_for_update)

    @app.post("/api/update/apply")
    def update_apply():
        body = request.get_json(silent=True)
        if body not in (None, {}):
            raise ApiError("更新请求不接受下载 URL 或其他参数", 400, "invalid_input")
        try:
            result = self_updater.prepare_update()
        except Exception as exc:
            app.logger.warning("Update preparation failed", exc_info=exc)
            return failure("远程服务请求失败", 502, "upstream_error")
        if result.get("should_exit"):
            def delayed_exit() -> None:
                time.sleep(float(app.config["UPDATE_EXIT_DELAY"]))
                app.config["EXIT_PROCESS"](0)

            threading.Thread(target=delayed_exit, daemon=True).start()
        return success(result)

    def nexus_count() -> int:
        raw = request.args.get("count", "24")
        if not raw.isdecimal():
            raise ApiError("count 必须是整数", 400, "invalid_input")
        count = int(raw)
        if not 1 <= count <= 50:
            raise ApiError("count 必须介于 1 和 50", 400, "invalid_input")
        return count

    def nexus_force() -> bool:
        raw = request.args.get("force", "0").casefold()
        if raw not in {"0", "1", "false", "true"}:
            raise ApiError("force 必须是布尔值", 400, "invalid_input")
        return raw in {"1", "true"}

    def nexus_result(call):
        try:
            return success(call())
        except ApiError:
            raise
        except nexus_api.NexusError as exc:
            app.logger.warning("Nexus request failed", exc_info=exc)
            return failure(str(exc), 502, "upstream_error")
        except Exception as exc:
            app.logger.warning("Nexus request failed", exc_info=exc)
            return failure("Nexus 请求失败", 502, "upstream_error")

    @app.get("/api/nexus/popular")
    def nexus_popular():
        sort = request.args.get("sort", "downloads")
        if sort not in {"downloads", "endorsements", "latest"}:
            raise ApiError("sort 必须是 downloads、endorsements 或 latest", 400, "invalid_input")
        return nexus_result(lambda: app.extensions["nexus_catalog"].popular(
            sort=sort, force=nexus_force(), count=nexus_count()))

    @app.get("/api/nexus/latest")
    def nexus_latest():
        return nexus_result(lambda: app.extensions["nexus_catalog"].popular(
            sort="latest", force=nexus_force(), count=nexus_count()))

    @app.get("/api/nexus/search")
    def nexus_search():
        return nexus_result(lambda: app.extensions["nexus_catalog"].search(
            request.args.get("q", ""), force=nexus_force(), count=nexus_count()))

    @app.get("/api/nexus/mod/<int:mod_id>")
    def nexus_mod(mod_id: int):
        return nexus_result(lambda: app.extensions["nexus_catalog"].get(mod_id, force=nexus_force()))

    @app.post("/api/system/restart-admin")
    def restart_admin():
        argv = list(sys.argv) if getattr(sys, "frozen", False) else [sys.executable, *sys.argv]
        process_utils.restart_as_admin(argv)
        return success({"restarting": True})

    return app


def _pick_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_server(url: str, timeout: float = 15.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1.0) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def main(*, root: Path | None = None, data_dir: Path | None = None) -> None:
    """Start the authenticated API on a random loopback port and open the UI."""
    host = "127.0.0.1"
    port = _pick_port(host)
    token = secrets.token_urlsafe(32)
    application = create_app(root=root, data_dir=data_dir, session_token=token)
    base_url = f"http://{host}:{port}/"
    launch_url = f"{base_url}?token={token}"

    def run() -> None:
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        application.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)

    server = threading.Thread(target=run, daemon=True)
    server.start()
    server_ready = _wait_server(base_url)
    if not server_ready:
        print("服务启动超时，请检查防火墙。")

    frozen = self_updater.is_frozen()
    smoke = smoke_check.smoke_context(
        os.environ.get("PALDECK_SMOKE_REPORT"),
        os.environ.get("PALDECK_SMOKE_HANDSHAKE"),
        application.config["DATA_DIR"],
        frozen=frozen,
    )
    if smoke is not None:
        def run_smoke_report() -> None:
            failed = False
            try:
                if not server_ready:
                    raise RuntimeError("loopback server did not become ready")
                smoke_check.run_http_smoke(
                    base_url, token, smoke.report_path, frozen=frozen,
                )
            except Exception:
                failed = True
                traceback.print_exc()
            finally:
                smoke.marker_path.unlink(missing_ok=True)
            if failed:
                os._exit(2)

        threading.Thread(target=run_smoke_report, name="paldeck-smoke-report", daemon=True).start()

    force_browser = os.environ.get("PALMOD_BROWSER", "").strip().casefold() in {"1", "true", "yes"}
    try:
        if force_browser:
            raise ImportError
        import webview
    except ImportError:
        import webbrowser
        webbrowser.open(launch_url)
        try:
            while server.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    webview.create_window("幻兽帕鲁 Mod 管理面板", launch_url, width=1280, height=820, min_size=(960, 640), background_color="#EEF4FF", text_select=True, confirm_close=False, resizable=True)
    webview.start(debug=False, private_mode=False)


if __name__ == "__main__":
    main()
