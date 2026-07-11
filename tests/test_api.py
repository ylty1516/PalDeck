from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path


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
