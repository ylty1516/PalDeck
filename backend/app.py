"""Flask application for Palworld Mod Manager."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

def _resolve_root() -> Path:
    env = os.environ.get("PALMOD_ROOT")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Allow running as script, package, or frozen EXE
ROOT = _resolve_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import game_detector, mod_manager, nexus_api

STATIC_DIR = ROOT / "frontend"
if not STATIC_DIR.is_dir():
    # Fallback: frontend next to executable
    alt = Path(sys.executable).resolve().parent / "frontend" if getattr(sys, "frozen", False) else ROOT / "frontend"
    if alt.is_dir():
        STATIC_DIR = alt

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")


def ok(data=None, **extra):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return jsonify(payload)


def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    return ok({"status": "up", "version": "1.0.0"})


@app.get("/api/game/detect")
def api_detect():
    try:
        found = game_detector.find_palworld_installs()
        current = mod_manager.get_game_path()
        return ok({"installs": found, "current": current})
    except Exception as e:
        return err(str(e), 500)


@app.post("/api/game/set")
def api_set_game():
    body = request.get_json(silent=True) or {}
    path = (body.get("path") or "").strip()
    if not path:
        return err("请提供游戏路径")
    try:
        result = mod_manager.set_game_path(path)
        return ok(result)
    except Exception as e:
        return err(str(e), 400)


@app.get("/api/game/status")
def api_game_status():
    path = mod_manager.get_game_path()
    if not path:
        return ok({"configured": False, "path": None})
    try:
        info = game_detector.validate_game_path(path)
        return ok({"configured": True, **info})
    except Exception as e:
        return err(str(e), 500)


@app.post("/api/game/ensure-folders")
def api_ensure_folders():
    path = mod_manager.get_game_path()
    if not path:
        return err("尚未设置游戏路径")
    try:
        return ok(game_detector.ensure_mod_folders(path))
    except Exception as e:
        return err(str(e), 400)


@app.get("/api/mods")
def api_list_mods():
    try:
        return ok(mod_manager.list_mods())
    except Exception as e:
        return err(str(e), 500)


@app.post("/api/mods/import")
def api_import_mod():
    preferred = request.form.get("type") or request.args.get("type") or "auto"
    display_name = request.form.get("name") or None
    nexus_id = request.form.get("nexus_id")
    nexus_id_val = int(nexus_id) if nexus_id and str(nexus_id).isdigit() else None

    # Multipart file upload
    if "file" in request.files:
        f = request.files["file"]
        if not f or not f.filename:
            return err("未选择文件")
        upload_dir = mod_manager.DATA_DIR / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize filename
        safe_name = Path(f.filename).name
        dest = upload_dir / safe_name
        # Avoid overwrite collisions
        if dest.exists():
            dest = upload_dir / f"{Path(safe_name).stem}_{os.getpid()}{Path(safe_name).suffix}"
        f.save(dest)
        try:
            result = mod_manager.import_mod_file(
                dest,
                preferred_type=None if preferred == "auto" else preferred,
                display_name=display_name,
                nexus_id=nexus_id_val,
            )
            return ok(result)
        except Exception as e:
            traceback.print_exc()
            return err(str(e), 400)
        finally:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass

    body = request.get_json(silent=True) or {}
    path = body.get("path")
    if not path:
        return err("请上传文件或提供本地路径")
    try:
        result = mod_manager.import_mod_file(
            path,
            preferred_type=None if (body.get("type") or preferred) == "auto" else (body.get("type") or preferred),
            display_name=body.get("name") or display_name,
            nexus_id=body.get("nexus_id") or nexus_id_val,
        )
        return ok(result)
    except Exception as e:
        traceback.print_exc()
        return err(str(e), 400)


@app.post("/api/mods/<mod_id>/toggle")
def api_toggle_mod(mod_id: str):
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return err("缺少 enabled 字段")
    try:
        result = mod_manager.set_mod_enabled(mod_id, bool(body["enabled"]))
        return ok(result)
    except KeyError:
        return err("未找到该模组", 404)
    except Exception as e:
        traceback.print_exc()
        return err(str(e), 400)


@app.delete("/api/mods/<mod_id>")
def api_delete_mod(mod_id: str):
    try:
        return ok(mod_manager.delete_mod(mod_id))
    except KeyError:
        return err("未找到该模组", 404)
    except Exception as e:
        return err(str(e), 400)


@app.post("/api/mods/resync")
def api_resync():
    try:
        return ok(mod_manager.resync_from_disk())
    except Exception as e:
        return err(str(e), 500)


@app.get("/api/mods/open-folder")
def api_open_folder():
    mod_id = request.args.get("id")
    try:
        folder = mod_manager.open_mod_folder(mod_id)
        os.startfile(folder)  # type: ignore[attr-defined]
        return ok({"path": folder})
    except Exception as e:
        return err(str(e), 400)


@app.get("/api/nexus/popular")
def api_nexus_popular():
    sort = request.args.get("sort") or "downloads"
    count = int(request.args.get("count") or 24)
    try:
        return ok(nexus_api.fetch_popular(count=count, sort=sort))
    except Exception as e:
        return err(str(e), 502)


@app.get("/api/nexus/latest")
def api_nexus_latest():
    count = int(request.args.get("count") or 24)
    try:
        return ok(nexus_api.fetch_latest(count=count))
    except Exception as e:
        return err(str(e), 502)


@app.get("/api/nexus/search")
def api_nexus_search():
    q = request.args.get("q") or ""
    count = int(request.args.get("count") or 24)
    try:
        return ok(nexus_api.search_mods(q, count=count))
    except Exception as e:
        return err(str(e), 502)


@app.get("/api/nexus/mod/<int:mod_id>")
def api_nexus_mod(mod_id: int):
    try:
        mod = nexus_api.get_mod(mod_id)
        if not mod:
            return err("未找到该 N 网模组", 404)
        return ok(mod)
    except Exception as e:
        return err(str(e), 502)


def create_app() -> Flask:
    return app


def _port_free(host: str, p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, p))
            return True
        except OSError:
            return False


def _pick_port(host: str, preferred: int) -> int:
    if _port_free(host, preferred):
        return preferred
    for candidate in range(preferred + 1, preferred + 30):
        if _port_free(host, candidate):
            return candidate
    return preferred


def _wait_server(url: str, timeout: float = 15.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _run_flask(host: str, port: int) -> None:
    # werkzeug logs to stderr; keep quiet in desktop mode
    import logging

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


def _icon_path() -> str | None:
    candidates = [
        ROOT / "assets" / "app.ico",
        Path(sys.executable).resolve().parent / "assets" / "app.ico" if getattr(sys, "frozen", False) else None,
        Path(__file__).resolve().parent.parent / "assets" / "app.ico",
    ]
    for c in candidates:
        if c and c.is_file():
            return str(c)
    return None


def main():
    """Start local API + native desktop window (no system browser)."""
    host = os.environ.get("PALMOD_HOST", "127.0.0.1")
    port = _pick_port(host, int(os.environ.get("PALMOD_PORT", "17865")))
    url = f"http://{host}:{port}/"
    force_browser = os.environ.get("PALMOD_BROWSER", "").strip() in ("1", "true", "yes")

    server = threading.Thread(target=_run_flask, args=(host, port), daemon=True)
    server.start()

    if not _wait_server(url):
        # Last resort: still try to open something so user sees an error surface
        print("服务启动超时，请检查防火墙或端口占用。")

    if force_browser:
        import webbrowser

        webbrowser.open(url)
        # Keep process alive while using browser mode
        try:
            while server.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    try:
        import webview
    except ImportError:
        import webbrowser

        print("未安装 pywebview，回退为浏览器模式。可执行: pip install pywebview")
        webbrowser.open(url)
        try:
            while server.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    webview.create_window(
        title="幻兽帕鲁 Mod 管理面板",
        url=url,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#EEF4FF",
        text_select=True,
        confirm_close=False,
        resizable=True,
    )

    # Block until user closes the window — then process exits
    # private_mode=False allows cache; gui auto-selects Edge WebView2 on Windows
    webview.start(debug=False, private_mode=False)
    os._exit(0)


if __name__ == "__main__":
    main()
