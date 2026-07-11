from __future__ import annotations

import io
import sys
import time
import types
import zipfile
from pathlib import Path

from backend.app import create_app


def _pak_zip(name: str = "Example.pak", content: bytes = b"pak-data") -> io.BytesIO:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(name, content)
    payload.seek(0)
    return payload


def test_static_assets_are_public(app):
    response = app.test_client().get("/app.js")
    assert response.status_code == 200


def test_health_is_public_and_structured(app):
    response = app.test_client().get("/api/health")
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["data"]["status"] == "up"


def test_api_rejects_missing_session_cookie(app):
    response = app.test_client().get("/api/mods")
    assert response.status_code == 403
    assert response.json == {
        "ok": False,
        "error": "会话无效",
        "error_code": "invalid_session",
        "details": {},
    }


def test_index_requires_matching_token_and_redirects_without_token(app):
    client = app.test_client()
    assert client.get("/?token=wrong").status_code == 403

    response = client.get("/?token=test-token")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    cookie = response.headers["Set-Cookie"]
    assert "paldeck_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "test-token" not in response.headers["Location"]


def test_testing_mode_does_not_bypass_auth(app):
    assert app.testing is True
    assert app.test_client().get("/api/mods").status_code == 403


def test_mods_success_uses_standard_envelope(auth_client):
    response = auth_client.get("/api/mods")
    assert response.status_code == 200
    assert response.json == {"ok": True, "data": []}


def test_open_mod_folder_requires_authentication(app):
    response = app.test_client().get("/api/mods/open-folder")
    assert response.status_code == 403
    assert response.json["error_code"] == "invalid_session"


def test_open_mod_folder_without_id_opens_tilde_mods(app, auth_client):
    opened = []
    app.config["OPEN_FOLDER"] = lambda path: opened.append(path)

    response = auth_client.get("/api/mods/open-folder")

    assert response.status_code == 200
    assert Path(response.json["data"]["path"]).parts[-4:] == ("Pal", "Content", "Paks", "~mods")
    assert opened == [response.json["data"]["path"]]


def test_open_mod_folder_with_id_opens_managed_install_root(app, auth_client):
    opened = []
    app.config["OPEN_FOLDER"] = lambda path: opened.append(path)
    installed = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "Example.zip")},
        content_type="multipart/form-data",
    ).json["data"]

    response = auth_client.get(f"/api/mods/open-folder?id={installed['id']}")

    assert response.status_code == 200
    assert Path(response.json["data"]["path"]).name == "~mods"
    assert opened == [response.json["data"]["path"]]


def test_open_mod_folder_rejects_unknown_id(app, auth_client):
    app.config["OPEN_FOLDER"] = lambda _path: None

    response = auth_client.get("/api/mods/open-folder?id=not-managed")

    assert response.status_code == 404
    assert response.json["error_code"] == "mod_not_found"


def test_import_conflict_returns_retry_token(auth_client):
    first = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "Example.zip")},
        content_type="multipart/form-data",
    )
    assert first.status_code == 200

    conflict = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(content=b"different"), "../Example.zip")},
        content_type="multipart/form-data",
    )
    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "mod_conflict"
    details = conflict.json["details"]
    assert set(details["choices"]) == {"replace", "keep_both", "cancel"}
    assert details["upload_token"]

    retry = auth_client.post(
        "/api/mods/import",
        json={"upload_token": details["upload_token"], "decision": "keep_both"},
    )
    assert retry.status_code == 200
    reused = auth_client.post(
        "/api/mods/import",
        json={"upload_token": details["upload_token"], "decision": "keep_both"},
    )
    assert reused.status_code == 400
    assert reused.json["error_code"] == "upload_expired"


def test_game_running_maps_to_423(app, auth_client):
    app.extensions["mod_service"].game_running = lambda: True
    response = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "running.zip")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 423
    assert response.json["error_code"] == "game_running"


def test_upload_temporary_file_is_cleaned_after_error(app, auth_client, monkeypatch):
    service = app.extensions["mod_service"]

    def fail(*args, **kwargs):
        raise ValueError("bad archive")

    monkeypatch.setattr(service, "install", fail)
    response = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "../../unsafe.zip")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    upload_dir = Path(app.config["DATA_DIR"]) / "uploads"
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_mod_config_routes_are_not_exposed(auth_client):
    assert auth_client.get("/api/mod-config").status_code == 404
    assert auth_client.get("/api/mod-config/example").status_code == 404
    assert auth_client.post("/api/mod-config/install-bundled", json={}).status_code in {404, 405}


def test_update_apply_exits_after_response(app, auth_client, monkeypatch):
    exited = []
    app.config["EXIT_PROCESS"] = lambda code: exited.append(code)
    app.config["UPDATE_EXIT_DELAY"] = 0.01
    monkeypatch.setattr(
        "backend.app.self_updater.prepare_update",
        lambda download_url=None: {"should_exit": True, "prepared": True},
    )

    response = auth_client.post("/api/update/apply", json={})

    assert response.status_code == 200
    assert response.json["data"]["prepared"] is True
    deadline = time.time() + 1
    while not exited and time.time() < deadline:
        time.sleep(0.01)
    assert exited == [0]


def test_create_app_removes_orphaned_uploads(tmp_path, fake_game_root, monkeypatch):
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True)
    orphan = uploads / "orphan.zip"
    orphan.write_bytes(b"orphan")
    monkeypatch.setenv("PALMOD_GAME_PATH", str(fake_game_root))

    create_app(data_dir=tmp_path / "data", session_token="cleanup", testing=True)

    assert not orphan.exists()


def test_pending_upload_count_limit_returns_429(app, auth_client):
    app.config["PENDING_MAX_ITEMS"] = 1
    installed = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "Example.zip")},
        content_type="multipart/form-data",
    )
    assert installed.status_code == 200
    first = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(content=b"different-1"), "Example.zip")},
        content_type="multipart/form-data",
    )
    assert first.status_code == 409

    second = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(content=b"different-2"), "Example.zip")},
        content_type="multipart/form-data",
    )

    assert second.status_code == 429
    assert second.json["error_code"] == "pending_upload_limit"


def test_single_upload_size_limit_returns_413(app, auth_client):
    app.config["MAX_CONTENT_LENGTH"] = 100

    response = auth_client.post(
        "/api/mods/import",
        data={"file": (io.BytesIO(b"x" * 1024), "large.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert response.json["error_code"] == "upload_too_large"


def test_deep_value_error_does_not_leak_paths(app, auth_client, monkeypatch):
    secret = "SECRET_PRIVATE_PATH"

    def fail(*args, **kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(app.extensions["mod_service"], "install", fail)
    response = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "safe.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert secret not in response.get_data(as_text=True)


def test_invalid_nexus_count_is_400(auth_client):
    response = auth_client.get("/api/nexus/latest?count=not-a-number")
    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"


def test_launcher_passes_runtime_paths_to_factory_main(tmp_path, monkeypatch):
    import launcher

    calls = []
    fake_app = types.ModuleType("backend.app")
    fake_app.main = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "backend.app", fake_app)
    monkeypatch.setattr(launcher, "_resource_root", lambda: tmp_path / "root")
    monkeypatch.setattr(launcher, "_writable_data_dir", lambda: tmp_path / "data")

    launcher.main()

    assert calls == [{"root": tmp_path / "root", "data_dir": tmp_path / "data"}]


def test_internal_errors_do_not_leak_exception_text(app, auth_client, monkeypatch):
    secret = "SECRET-local-path"

    def fail():
        raise RuntimeError(secret)

    monkeypatch.setattr(app.extensions["mod_service"], "list_mods", fail)
    response = auth_client.get("/api/mods")
    assert response.status_code == 500
    assert response.json["error_code"] == "internal_error"
    assert secret not in response.get_data(as_text=True)
