from __future__ import annotations

import io
import sys
import threading
import time
import types
from types import SimpleNamespace
import zipfile
from pathlib import Path

import pytest

from backend.app import create_app
from backend.mod_service import GameRunningError
from backend.steam_workshop import SteamWorkshopService, WorkshopDependencyError, WorkshopMod, WorkshopNotFoundError
from backend.ue4ss_provider import Ue4ssAsset


def _pak_zip(name: str = "Example.pak", content: bytes = b"pak-data") -> io.BytesIO:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(name, content)
    payload.seek(0)
    return payload


def _ue4ss_zip_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("dwmapi.dll", b"proxy")
        archive.writestr("ue4ss/UE4SS.dll", b"dll")
        archive.writestr("ue4ss/UE4SS-settings.ini", b"[General]\n")
        archive.writestr("ue4ss/MemberVariableLayout.ini", b"layout")
    return payload.getvalue()


class FakeUe4ssProvider:
    def __init__(self, archive: bytes):
        self.archive = archive
        self.calls = []
        self.asset = Ue4ssAsset("UE4SS-Palworld.zip", len(archive), "a" * 64, "2026-01-02T03:04:05Z", "https://github.com/Okaetsu/RE-UE4SS/releases/download/experimental-palworld/UE4SS-Palworld.zip")
        self.update_available = True

    def bundled_archive(self):
        self.calls.append("bundled")
        return self.archive

    def bundled_status(self):
        return {"available": True, "asset": self.asset}

    def check_upstream(self):
        self.calls.append("check")
        return {"asset": self.asset, "update_available": self.update_available}

    def download_verified(self, asset, destination):
        self.calls.append(("download", asset))
        Path(destination).write_bytes(self.archive)
        return Path(destination)


def test_trash_and_external_routes_require_authentication(app):
    client = app.test_client()
    trash_id = "12345678123456781234567812345678"
    mod_id = "12345678123456781234567812345678"
    routes = (
        ("get", "/api/trash", None),
        ("post", f"/api/trash/{trash_id}/restore", {}),
        ("delete", f"/api/trash/{trash_id}", None),
        ("post", f"/api/mods/{mod_id}/unmanage", {}),
        ("get", "/api/mods/ignored", None),
        ("post", "/api/mods/ignored/reset", {}),
    )
    for method, path, body in routes:
        response = getattr(client, method)(path, json=body)
        assert response.status_code == 403
        assert response.json["error_code"] == "invalid_session"


def test_delete_moves_mod_to_trash_and_restore_round_trips(app, auth_client, tmp_path):
    source = tmp_path / "recoverable.pak"
    source.write_bytes(b"recoverable")
    installed = app.extensions["mod_service"].install(source)

    removed = auth_client.delete(f"/api/mods/{installed['id']}")

    assert removed.status_code == 200
    trash_id = removed.json["data"]["trash_id"]
    assert removed.json["data"]["files_moved"] == 1
    listed = auth_client.get("/api/trash")
    assert listed.status_code == 200
    assert listed.json["data"]["items"][0]["id"] == trash_id
    assert "payload" not in str(listed.json).casefold()

    restored = auth_client.post(f"/api/trash/{trash_id}/restore", json={})

    assert restored.status_code == 200
    assert restored.json["data"]["id"] == installed["id"]
    assert source.name == restored.json["data"]["manifest_files"][0]["relative_path"]
    assert auth_client.get("/api/trash").json["data"]["items"] == []


def test_trash_routes_reject_invalid_ids_bodies_and_delete_queries(
    app, auth_client, tmp_path
):
    assert auth_client.post("/api/trash/not-a-uuid/restore", json={}).status_code == 400
    valid = "12345678123456781234567812345678"
    assert auth_client.post(f"/api/trash/{valid}/restore", json={"path": "C:/bad"}).status_code == 400
    assert auth_client.delete("/api/mods/not-a-uuid?force_modified=maybe").status_code == 400
    assert auth_client.delete("/api/mods/not-a-uuid?extra=true").status_code == 400


def test_trash_restore_conflict_and_permanent_purge_are_explicit(
    app, auth_client, tmp_path
):
    source = tmp_path / "conflict.pak"
    source.write_bytes(b"conflict")
    installed = app.extensions["mod_service"].install(source)
    trash_id = auth_client.delete(f"/api/mods/{installed['id']}").json["data"]["trash_id"]
    destination = app.extensions["mod_service"].game_root / "Pal/Content/Paks/~mods/conflict.pak"
    destination.write_bytes(b"external")

    conflict = auth_client.post(f"/api/trash/{trash_id}/restore", json={})

    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "trash_restore_conflict"
    assert conflict.json["details"]["file_count"] == 1
    destination.unlink()
    purged = auth_client.delete(f"/api/trash/{trash_id}")
    assert purged.status_code == 200
    assert purged.json["data"]["files_deleted"] == 1
    assert auth_client.get("/api/trash").json["data"]["items"] == []


def test_corrupt_trash_record_is_listed_but_cannot_be_restored(
    app, auth_client
):
    trash_id = "12345678123456781234567812345678"
    records = Path(app.config["DATA_DIR"]) / "trash/records"
    records.mkdir(parents=True, exist_ok=True)
    (records / f"{trash_id}.json").write_text("{broken", encoding="utf-8")

    listed = auth_client.get("/api/trash")
    restored = auth_client.post(f"/api/trash/{trash_id}/restore", json={})

    assert trash_id in listed.json["data"]["invalid_records"]
    assert restored.status_code == 400
    assert restored.json["error_code"] == "trash_record_invalid"


def test_external_mod_can_be_unmanaged_ignored_and_rediscovered(
    app, auth_client, fake_game_root
):
    pak = fake_game_root / "Pal/Content/Paks/~mods/ExternalApi.pak"
    pak.parent.mkdir(parents=True, exist_ok=True)
    pak.write_bytes(b"external")
    service = app.extensions["mod_service"]
    external = next(item for item in service.rescan() if item["name"] == "ExternalApi")

    unmanaged = auth_client.post(f"/api/mods/{external['id']}/unmanage", json={})

    assert unmanaged.status_code == 200
    assert pak.read_bytes() == b"external"
    ignored = auth_client.get("/api/mods/ignored")
    assert ignored.status_code == 200
    assert ignored.json["data"]["count"] == 1
    assert service.rescan() == []

    reset = auth_client.post("/api/mods/ignored/reset", json={})

    assert reset.status_code == 200
    assert reset.json["data"]["ignored_removed"] == 1
    assert reset.json["data"]["rediscovered"] == 1
    assert reset.json["data"]["mods"][0]["externally_discovered"] is True


def test_ignored_reset_while_game_runs_keeps_ignore_store(
    app, auth_client, fake_game_root
):
    pak = fake_game_root / "Pal/Content/Paks/~mods/StillIgnored.pak"
    pak.parent.mkdir(parents=True, exist_ok=True)
    pak.write_bytes(b"external")
    service = app.extensions["mod_service"]
    external = next(item for item in service.rescan() if item["name"] == "StillIgnored")
    assert auth_client.post(f"/api/mods/{external['id']}/unmanage", json={}).status_code == 200
    service.game_running = lambda: True

    response = auth_client.post("/api/mods/ignored/reset", json={})

    assert response.status_code == 423
    assert response.json["error_code"] == "ignored_reset_requires_game_stopped"
    assert service.ignored_summary()["count"] == 1


def test_paldeck_installed_mod_cannot_be_unmanaged(app, auth_client, tmp_path):
    source = tmp_path / "managed.pak"
    source.write_bytes(b"managed")
    installed = app.extensions["mod_service"].install(source)

    response = auth_client.post(f"/api/mods/{installed['id']}/unmanage", json={})

    assert response.status_code == 409
    assert response.json["error_code"] == "mod_not_external"


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


def test_index_preserves_only_valid_custom_chrome_contract_across_auth_redirect(app):
    custom = app.test_client().get("/?token=test-token&chrome=1")
    native = app.test_client().get("/?token=test-token&chrome=0")
    invalid = app.test_client().get("/?token=test-token&chrome=javascript:alert(1)")
    assert custom.headers["Location"].endswith("/?chrome=1")
    assert native.headers["Location"].endswith("/?chrome=0")
    assert invalid.headers["Location"].endswith("/")


def test_testing_mode_does_not_bypass_auth(app):
    assert app.testing is True
    assert app.test_client().get("/api/mods").status_code == 403


def test_game_services_share_data_scope_and_rebuild_for_a_new_game(
    app, auth_client, tmp_path
):
    first = app.extensions["mod_service"]
    assert first.trash_service.data_dir == Path(app.config["DATA_DIR"])
    assert first.ignored_store.path == Path(app.config["DATA_DIR"]) / "ignored-mods-v1.json"
    assert app.extensions["trash_service"] is first.trash_service
    assert app.extensions["ignored_mod_store"] is first.ignored_store
    framework = app.extensions["ue4ss_framework_manager"]
    assert framework.data_dir == Path(app.config["DATA_DIR"])
    assert framework.game_root == first.game_root
    first_fingerprint = first.trash_service.game_fingerprint

    second_root = tmp_path / "SecondPalworld"
    (second_root / "Pal/Binaries/Win64").mkdir(parents=True)
    (second_root / "Pal/Content/Paks").mkdir(parents=True)
    (second_root / "Palworld.exe").touch()
    (second_root / "Pal/Binaries/Win64/Palworld-Win64-Shipping.exe").touch()
    response = auth_client.post("/api/game/set", json={"path": str(second_root)})

    assert response.status_code == 200
    second = app.extensions["mod_service"]
    assert second is not first
    assert second.game_root == second_root
    assert second.trash_service.game_fingerprint != first_fingerprint
    assert app.extensions["trash_service"] is second.trash_service
    assert app.extensions["ignored_mod_store"] is second.ignored_store
    assert app.extensions["workshop_service"].game_root == second_root
    assert app.extensions["ue4ss_framework_manager"].game_root == second_root
    assert app.extensions["ue4ss_framework_manager"] is not framework


def test_mods_success_uses_standard_envelope(auth_client):
    response = auth_client.get("/api/mods")
    assert response.status_code == 200
    assert response.json == {"ok": True, "data": []}


def test_first_mod_listing_discovers_preinstalled_mods_across_supported_layouts(
    app, auth_client, fake_game_root
):
    pak = fake_game_root / "Pal" / "Content" / "Paks" / "~mods" / "ExistingPak.pak"
    pak.parent.mkdir(parents=True, exist_ok=True)
    pak.write_bytes(b"pak")
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    for root, name in (
        (win64 / "Mods", "ClassicExisting"),
        (win64 / "ue4ss" / "Mods", "NestedExisting"),
    ):
        script = root / name / "Scripts" / "main.lua"
        script.parent.mkdir(parents=True)
        script.write_bytes(b"lua")
        (root / "mods.txt").write_text(f"{name} : 1\n", encoding="utf-8")
    (win64 / "ue4ss" / "UE4SS.dll").touch()

    response = auth_client.get("/api/mods")

    assert response.status_code == 200
    local = [item for item in response.json["data"] if item.get("source") != "steam_workshop"]
    assert {item["name"] for item in local} == {
        "ExistingPak", "ClassicExisting", "NestedExisting",
    }


def test_ue4ss_endpoints_require_authentication(app):
    for path in ("/api/ue4ss/install-bundled", "/api/ue4ss/check-upstream", "/api/ue4ss/install-upstream"):
        response = app.test_client().post(path, json={})
        assert response.status_code == 403
        assert response.json["error_code"] == "invalid_session"


@pytest.mark.parametrize("mod_operation", ["install", "set_enabled"])
def test_mod_service_and_ue4ss_install_share_game_write_lock(
    app, tmp_path, monkeypatch, mod_operation
):
    service = app.extensions["mod_service"]
    source = tmp_path / f"{mod_operation}.pak"
    source.write_bytes(b"pak")
    entered_mod = threading.Event()
    release_mod = threading.Event()
    entered_ue4ss = threading.Event()
    errors = []

    item = service.install(source) if mod_operation == "set_enabled" else None
    original_stopped = service._assert_stopped

    def blocking_stopped():
        entered_mod.set()
        assert release_mod.wait(5)
        return original_stopped()

    monkeypatch.setattr(service, "_assert_stopped", blocking_stopped)
    operation = (
        (lambda: service.install(source)) if mod_operation == "install"
        else (lambda: service.set_enabled(item["id"], False))
    )

    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr(
        "backend.ue4ss_installer.install_from_bytes",
        lambda *_a, **_k: entered_ue4ss.set() or {"ok": True},
    )

    def run_mod():
        try: operation()
        except Exception as error: errors.append(error)

    def run_ue4ss():
        try:
            client = app.test_client(); client.get("/?token=test-token")
            response = client.post("/api/ue4ss/install-bundled", json={})
            assert response.status_code == 200
        except Exception as error: errors.append(error)

    mod_thread = threading.Thread(target=run_mod)
    ue4ss_thread = threading.Thread(target=run_ue4ss)
    mod_thread.start(); assert entered_mod.wait(5)
    ue4ss_thread.start()
    assert not entered_ue4ss.wait(0.2)
    release_mod.set(); mod_thread.join(5); ue4ss_thread.join(5)

    assert entered_ue4ss.is_set()
    assert errors == []


def test_all_ue4ss_write_routes_share_one_game_lock(app, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    entered = threading.Event()
    release = threading.Event()
    zip_entered = threading.Event()
    responses = []

    def blocking_bytes(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    def observing_zip(*_args, **_kwargs):
        zip_entered.set()
        return {"ok": True}

    monkeypatch.setattr("backend.ue4ss_installer.install_from_bytes", blocking_bytes)
    monkeypatch.setattr("backend.ue4ss_installer.install_from_zip", observing_zip)

    def bundled_request():
        client = app.test_client(); client.get("/?token=test-token")
        responses.append(client.post("/api/ue4ss/install-bundled", json={}))

    def local_request():
        client = app.test_client(); client.get("/?token=test-token")
        responses.append(client.post(
            "/api/ue4ss/install-zip",
            data={"file": (io.BytesIO(_ue4ss_zip_bytes()), "UE4SS.zip")},
            content_type="multipart/form-data",
        ))

    first = threading.Thread(target=bundled_request)
    second = threading.Thread(target=local_request)
    first.start(); assert entered.wait(5)
    second.start()
    assert not zip_entered.wait(0.2)
    release.set(); first.join(5); second.join(5)

    assert zip_entered.is_set()
    assert [response.status_code for response in responses] == [200, 200]


def test_ue4ss_install_rejects_extra_fields_without_calling_provider(app, auth_client):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider

    for path in ("/api/ue4ss/install-bundled", "/api/ue4ss/install-upstream"):
        for field in ("url", "path", "repo", "asset"):
            response = auth_client.post(path, json={field: "attacker-controlled"})
            assert response.status_code == 400
            assert response.json["error_code"] == "invalid_input"
    assert provider.calls == []


def test_ue4ss_bundled_resource_failure_is_server_error(app, auth_client, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr(provider, "bundled_archive", lambda: (_ for _ in ()).throw(ValueError("tampered")))

    response = auth_client.post("/api/ue4ss/install-bundled", json={})

    assert response.status_code >= 500
    assert response.json["error_code"] == "bundled_resource_invalid"


def test_ue4ss_bundled_install_maps_running_and_existing_conflict(app, auth_client, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: True)
    running = auth_client.post("/api/ue4ss/install-bundled", json={})
    assert running.status_code == 423
    assert running.json["error_code"] == "game_running"

    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: False)
    win64 = Path(app.extensions["mod_service"].game_root) / "Pal" / "Binaries" / "Win64"
    (win64 / "dwmapi.dll").write_bytes(b"manual")
    conflict = auth_client.post("/api/ue4ss/install-bundled", json={})
    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "ue4ss_conflict"
    assert conflict.json["details"]["markers"]["dwmapi"] is True
    confirmed = auth_client.post("/api/ue4ss/install-bundled", json={"confirm_replace": True})
    assert confirmed.status_code == 200


def test_ue4ss_check_tracks_server_asset_and_reports_same_or_new_digest(app, auth_client):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    provider.update_available = False
    same = auth_client.post("/api/ue4ss/check-upstream", json={})
    assert same.status_code == 200
    assert same.json["data"]["update_available"] is False
    assert same.json["data"]["asset"]["sha256"] == "a" * 64
    assert app.extensions["ue4ss_pending_asset"]["asset"] is provider.asset

    provider.update_available = True
    newer = auth_client.post("/api/ue4ss/check-upstream", json={})
    assert newer.json["data"]["update_available"] is True


def test_ue4ss_upstream_install_requires_unexpired_server_check(app, auth_client):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    no_check = auth_client.post("/api/ue4ss/install-upstream", json={})
    assert no_check.status_code == 409
    assert no_check.json["error_code"] == "ue4ss_check_required"
    assert not any(isinstance(call, tuple) for call in provider.calls)

    assert auth_client.post("/api/ue4ss/check-upstream", json={}).status_code == 200
    app.extensions["ue4ss_pending_asset"]["expires"] = time.time() - 1
    expired = auth_client.post("/api/ue4ss/install-upstream", json={})
    assert expired.status_code == 410
    assert expired.json["error_code"] == "ue4ss_check_expired"
    assert not any(isinstance(call, tuple) for call in provider.calls)


def test_ue4ss_pending_consumption_does_not_delete_newer_check(app, auth_client, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    asset_a = provider.asset
    asset_b = Ue4ssAsset(asset_a.name, asset_a.size, "b" * 64, "newer", asset_a.download_url)
    checks = iter((asset_a, asset_b))
    provider.check_upstream = lambda: {"asset": next(checks), "update_available": True}
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: False)
    second_client = app.test_client()
    second_client.get("/?token=test-token")

    def interleaved_download(asset, destination):
        assert asset is asset_a
        Path(destination).write_bytes(provider.archive)
        assert second_client.post("/api/ue4ss/check-upstream", json={}).status_code == 200

    provider.download_verified = interleaved_download
    assert auth_client.post("/api/ue4ss/check-upstream", json={}).status_code == 200

    installed = auth_client.post("/api/ue4ss/install-upstream", json={})

    assert installed.status_code == 200
    assert app.extensions["ue4ss_pending_asset"]["asset"] is asset_b


def test_ue4ss_pending_expiry_is_rechecked_after_download(app, auth_client, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    now = [100.0]
    monkeypatch.setattr("backend.app.time.time", lambda: now[0])
    assert auth_client.post("/api/ue4ss/check-upstream", json={}).status_code == 200

    def expiring_download(_asset, destination):
        Path(destination).write_bytes(provider.archive)
        now[0] = 701.0

    provider.download_verified = expiring_download
    called = []
    monkeypatch.setattr("backend.ue4ss_installer.install_from_zip", lambda *a, **k: called.append(True))

    response = auth_client.post("/api/ue4ss/install-upstream", json={})

    assert response.status_code == 410
    assert response.json["error_code"] == "ue4ss_check_expired"
    assert called == []


def test_ue4ss_local_zip_rejects_extra_and_duplicate_multipart_fields(app, auth_client, monkeypatch):
    from werkzeug.datastructures import MultiDict
    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: False)

    cases = [
        {"file": (io.BytesIO(_ue4ss_zip_bytes()), "UE4SS.zip"), "url": "https://evil.example"},
        MultiDict([
            ("file", (io.BytesIO(_ue4ss_zip_bytes()), "UE4SS.zip")),
            ("confirm_replace", "false"), ("confirm_replace", "true"),
        ]),
        MultiDict([
            ("file", (io.BytesIO(_ue4ss_zip_bytes()), "one.zip")),
            ("file", (io.BytesIO(_ue4ss_zip_bytes()), "two.zip")),
        ]),
    ]
    for data in cases:
        response = auth_client.post(
            "/api/ue4ss/install-zip", data=data, content_type="multipart/form-data"
        )
        assert response.status_code == 400
        assert response.json["error_code"] == "invalid_input"


def test_ue4ss_upstream_install_uses_server_asset_and_revalidates_zip(app, auth_client, monkeypatch):
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: False)
    assert auth_client.post("/api/ue4ss/check-upstream", json={}).status_code == 200

    installed = auth_client.post("/api/ue4ss/install-upstream", json={})

    assert installed.status_code == 200
    assert provider.calls[-1] == ("download", provider.asset)
    assert "ue4ss_pending_asset" not in app.extensions


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


def test_native_selection_token_installs_once_without_client_path(app, auth_client, tmp_path):
    archive = tmp_path / "Selected.zip"
    archive.write_bytes(_pak_zip().getvalue())
    grant = app.extensions["import_selection_registry"].issue([archive])[0]

    installed = auth_client.post("/api/mods/import", json={
        "selection_token": grant["selection_token"], "decision": "cancel", "type": "auto",
    })
    assert installed.status_code == 200
    assert installed.json["data"]["name"] == "Example"

    reused = auth_client.post("/api/mods/import", json={
        "selection_token": grant["selection_token"], "decision": "replace",
    })
    assert reused.status_code == 410
    assert reused.json["error_code"] == "selection_expired"


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
    assert reused.status_code == 410
    assert reused.json["error_code"] == "upload_expired"


def test_conflict_cancel_removes_pending_file_without_retrying_service(app, auth_client, monkeypatch):
    assert auth_client.post(
        "/api/mods/import", data={"file": (_pak_zip(), "Example.zip")},
        content_type="multipart/form-data",
    ).status_code == 200
    conflict = auth_client.post(
        "/api/mods/import", data={"file": (_pak_zip(content=b"changed"), "Example.zip")},
        content_type="multipart/form-data",
    )
    token = conflict.json["details"]["upload_token"]
    pending_path = Path(app.extensions["pending_uploads"][token]["path"])
    assert pending_path.is_file()

    monkeypatch.setattr(
        app.extensions["mod_service"], "install",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cancel must not call install")),
    )
    cancelled = auth_client.post(
        "/api/mods/import", json={"upload_token": token, "decision": "cancel"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json == {"ok": True, "data": {"cancelled": True}}
    assert app.extensions["pending_uploads"] == {}
    assert not pending_path.exists()
    reused = auth_client.post(
        "/api/mods/import", json={"upload_token": token, "decision": "replace"},
    )
    assert reused.status_code == 410
    assert reused.json["error_code"] == "upload_expired"


def test_unknown_retry_token_is_410(auth_client):
    response = auth_client.post(
        "/api/mods/import", json={"upload_token": "not-a-token", "decision": "replace"},
    )
    assert response.status_code == 410
    assert response.json["error_code"] == "upload_expired"


def test_modified_delete_returns_409_then_force_succeeds(app, auth_client):
    installed = auth_client.post(
        "/api/mods/import", data={"file": (_pak_zip(), "Modified.zip")},
        content_type="multipart/form-data",
    ).json["data"]
    live_file = Path(installed["install_path"])
    live_file.write_bytes(b"locally modified")

    blocked = auth_client.delete(f"/api/mods/{installed['id']}")

    assert blocked.status_code == 409
    assert blocked.json["error_code"] == "modified_files"
    assert blocked.json["details"] == {"files": [str(live_file)]}
    assert any(mod["id"] == installed["id"] for mod in auth_client.get("/api/mods").json["data"])

    forced = auth_client.delete(f"/api/mods/{installed['id']}?force_modified=true")
    assert forced.status_code == 200
    assert not live_file.exists()
    assert all(mod["id"] != installed["id"] for mod in auth_client.get("/api/mods").json["data"])


def test_game_running_maps_to_423(app, auth_client):
    app.extensions["mod_service"].game_running = lambda: True
    response = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "running.zip")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 423
    assert response.json["error_code"] == "game_running"


def test_toggle_returns_authoritative_mod_and_permission_maps_to_403(app, auth_client, monkeypatch):
    installed = auth_client.post(
        "/api/mods/import",
        data={"file": (_pak_zip(), "Toggle.zip")},
        content_type="multipart/form-data",
    ).json["data"]

    toggled = auth_client.post(f"/api/mods/{installed['id']}/toggle", json={"enabled": False})

    assert toggled.status_code == 200
    assert toggled.json["data"]["id"] == installed["id"]
    assert toggled.json["data"]["enabled"] is False
    assert toggled.json["data"]["status"] == "disabled"

    monkeypatch.setattr(
        app.extensions["mod_service"], "set_enabled",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    denied = auth_client.post(f"/api/mods/{installed['id']}/toggle", json={"enabled": True})
    assert denied.status_code == 403
    assert denied.json["error_code"] == "permission_denied"


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


def test_static_ui_has_no_removed_mod_config_entry_points(app, auth_client):
    html = auth_client.get("/").get_data(as_text=True)
    javascript = app.test_client().get("/app.js").get_data(as_text=True)
    combined = html + javascript

    assert "/api/mod-config" not in combined
    assert "btnInstallBagExpand" not in combined
    assert "btnRefreshConfigs" not in combined
    assert "configList" not in combined
    assert "loadConfigs" not in javascript
    assert "renderConfigs" not in javascript
    assert "installBagExpand" not in javascript


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
        lambda: {"should_exit": True, "prepared": True},
    )

    response = auth_client.post("/api/update/apply", json={})

    assert response.status_code == 200
    assert response.json["data"]["prepared"] is True
    deadline = time.time() + 1
    while not exited and time.time() < deadline:
        time.sleep(0.01)
    assert exited == [0]


def test_credits_endpoint_returns_source_bundled_catalog(auth_client):
    response = auth_client.get("/api/credits")

    assert response.status_code == 200
    items = response.json["data"]
    assert {item["id"] for item in items} >= {
        "okaetsu", "ue4ss", "flask", "pywebview", "pillow", "pyinstaller",
        "palworld-modding-docs",
    }
    assert all(item["source_url"].startswith("https://") for item in items)


def test_open_trusted_link_uses_only_fixed_id_and_system_browser(app, auth_client):
    opened = []
    app.config["OPEN_URL"] = opened.append

    response = auth_client.post("/api/system/open-trusted-link", json={"id": "okaetsu"})

    assert response.status_code == 200
    assert response.json["data"] == {"opened": True, "id": "okaetsu"}
    assert opened == ["https://github.com/Okaetsu/RE-UE4SS"]


@pytest.mark.parametrize("body", [
    {"url": "https://github.com/Okaetsu/RE-UE4SS"},
    {"id": "okaetsu", "url": "https://evil.example"},
    {"id": "unknown"}, {"id": 1}, {}, None,
    {"id": "http://github.com/Okaetsu/RE-UE4SS"},
    {"id": "https://github.com.evil.example/Okaetsu/RE-UE4SS"},
])
def test_open_trusted_link_rejects_url_extra_non_https_and_unknown(app, auth_client, body):
    opened = []
    app.config["OPEN_URL"] = opened.append

    response = auth_client.post("/api/system/open-trusted-link", json=body)

    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"
    assert opened == []


def test_update_apply_rejects_arbitrary_download_url(auth_client, monkeypatch):
    called = []
    monkeypatch.setattr("backend.app.self_updater.prepare_update", lambda: called.append(True))

    response = auth_client.post(
        "/api/update/apply", json={"url": "https://evil.example/PalDeck.exe"},
    )

    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"
    assert called == []


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


def test_pending_total_quota_is_checked_atomically(app, monkeypatch):
    barrier = threading.Barrier(2)

    def conflict(*args, **kwargs):
        barrier.wait(timeout=5)
        from backend.mod_service import ModConflictError
        raise ModConflictError({"files": ["conflict"], "choices": ["cancel"]})

    monkeypatch.setattr(app.extensions["mod_service"], "install", conflict)
    app.config["PENDING_MAX_TOTAL_BYTES"] = 8
    responses = []

    def upload(name):
        client = app.test_client()
        client.get("/?token=test-token")
        responses.append(client.post(
            "/api/mods/import",
            data={"file": (io.BytesIO(b"payload"), name)},
            content_type="multipart/form-data",
        ))

    threads = [
        threading.Thread(target=upload, args=("one.zip",)),
        threading.Thread(target=upload, args=("two.zip",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(response.status_code for response in responses) == [409, 429]
    limited = next(response for response in responses if response.status_code == 429)
    assert limited.json["error_code"] == "pending_upload_quota"
    assert len(app.extensions["pending_uploads"]) == 1
    uploads = Path(app.config["DATA_DIR"]) / "uploads"
    assert len(list(uploads.iterdir())) == 1


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
    monkeypatch.setenv("PALMOD_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PALMOD_DATA_DIR", str(tmp_path / "data"))

    launcher.main()

    assert calls == [{"root": tmp_path / "root", "data_dir": tmp_path / "data"}]


class FakeWorkshopService:
    def __init__(self, mods=None, *, cleanup_pending=None):
        self.mods = list(mods or [])
        self.calls = []
        self.cleanup_pending = list(cleanup_pending or [])
        self.settings_path = Path("F:/trusted/Palworld/Mods/PalModSettings.ini")

    def list_mods(self, *, force=False):
        self.calls.append(("list", force))
        return [dict(mod) for mod in self.mods]

    def set_enabled(self, workshop_id, enabled, *, confirm_dependents=False, conflict_validator=None):
        self.calls.append(("toggle", workshop_id, enabled, confirm_dependents))
        item = next((mod for mod in self.mods if mod["workshop_id"] == workshop_id), None)
        if item is None:
            raise WorkshopNotFoundError(workshop_id)
        if conflict_validator is not None:
            conflict_validator(SimpleNamespace(install_types=tuple(item.get("install_types", []))))
        item["enabled"] = enabled
        item["status"] = "enabled" if enabled else "disabled"
        result = dict(item)
        result["changed_ids"] = [workshop_id]
        if self.cleanup_pending:
            result["cleanup_pending"] = list(self.cleanup_pending)
        return result

    def active_ue4ss_mods(self):
        return [
            dict(mod) for mod in self.mods
            if mod.get("enabled") is True and "UE4SS" in mod.get("install_types", [])
        ]


def _workshop_mod(**changes):
    value = {
        "id": "steam-workshop:3625223587",
        "workshop_id": "3625223587",
        "name": "Workshop Example",
        "mod_name": "Workshop Example",
        "package_name": "WorkshopExample",
        "source": "steam_workshop",
        "source_dir": "F:/Steam/workshop/content/1623730/3625223587",
        "author": "Workshop Author",
        "version": "1.2.3",
        "dependencies": ["3625000000"],
        "install_types": ["Paks"],
        "enabled": False,
        "global_enabled": False,
        "deployed": False,
        "needs_restart": False,
        "status": "disabled",
        "valid": True,
        "can_delete": False,
        "can_toggle": True,
    }
    value.update(changes)
    return value


def test_create_app_configures_workshop_with_discovery_game_and_shared_lock(app):
    workshop = app.extensions["workshop_service"]
    assert workshop._steam_roots is None
    assert workshop.game_root == app.extensions["mod_service"].game_root
    assert workshop.lock_root == Path(app.config["DATA_DIR"])


def test_workshop_routes_require_authentication(app):
    client = app.test_client()
    for path, method in (
        ("/api/workshop/mods", "get"),
        ("/api/workshop/rescan", "post"),
        ("/api/workshop/3625223587/enable", "post"),
        ("/api/workshop/3625223587/disable", "post"),
        ("/api/workshop/3625223587/open-page", "post"),
    ):
        response = getattr(client, method)(path, json={} if method == "post" else None)
        assert response.status_code == 403
        assert response.json["error_code"] == "invalid_session"


def test_unified_and_workshop_lists_include_authoritative_workshop_items(app, auth_client):
    workshop = FakeWorkshopService([_workshop_mod()])
    app.extensions["workshop_service"] = workshop

    dedicated = auth_client.get("/api/workshop/mods")
    unified = auth_client.get("/api/mods")

    assert dedicated.status_code == 200
    item = dedicated.json["data"][0]
    assert item["id"] == "steam-workshop:3625223587"
    assert item["source"] == "steam_workshop"
    assert item["can_delete"] is False
    assert item["can_toggle"] is True
    assert unified.json["data"] == [item]


def test_workshop_rescan_and_toggle_use_only_scanned_id_and_boolean_confirmation(app, auth_client):
    workshop = FakeWorkshopService([_workshop_mod()])
    app.extensions["workshop_service"] = workshop

    rescanned = auth_client.post("/api/workshop/rescan", json={})
    enabled = auth_client.post("/api/workshop/3625223587/enable", json={})
    disabled = auth_client.post(
        "/api/workshop/3625223587/disable", json={"confirm_dependents": True}
    )

    assert rescanned.status_code == 200
    assert enabled.json["data"]["mods"][0]["enabled"] is True
    assert enabled.json["data"]["mods"][0]["needs_restart"] is True
    assert enabled.json["data"]["changed_ids"] == ["3625223587"]
    assert enabled.json["data"]["cleanup_pending"] == []
    assert disabled.json["data"]["mods"][0]["enabled"] is False
    assert disabled.json["data"]["mods"][0]["needs_restart"] is True
    assert disabled.json["data"]["changed_ids"] == ["3625223587"]
    assert workshop.calls == [
        ("list", True),
        ("toggle", "3625223587", True, False),
        ("list", False),
        ("toggle", "3625223587", False, True),
        ("list", False),
    ]


def test_workshop_toggle_returns_only_app_generated_cleanup_paths(app, auth_client):
    workshop = FakeWorkshopService([_workshop_mod()])
    trusted = workshop.settings_path.parent / "..PalModSettings.ini.tx.tmp.safe.quarantine"
    workshop.cleanup_pending = [str(trusted), "F:/private/not-generated.txt"]
    app.extensions["workshop_service"] = workshop

    response = auth_client.post("/api/workshop/3625223587/enable", json={})

    assert response.status_code == 200
    assert response.json["data"]["cleanup_pending"] == [str(trusted)]
    assert "not-generated" not in response.get_data(as_text=True)


@pytest.mark.parametrize("workshop_id", ["0", "01", "-1", "1.5", "steam-workshop:1", "1" * 21])
def test_workshop_toggle_rejects_non_positive_decimal_id(app, auth_client, workshop_id):
    workshop = FakeWorkshopService([_workshop_mod()])
    app.extensions["workshop_service"] = workshop

    response = auth_client.post(f"/api/workshop/{workshop_id}/enable", json={})

    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"
    assert workshop.calls == []


@pytest.mark.parametrize("body", [
    {"confirm_dependents": "true"}, {"path": "F:/evil"},
    {"package": "Injected"}, {"package_name": "Injected"},
    {"url": "https://evil.example"}, {"confirm_dependents": False, "path": "F:/evil"},
])
def test_workshop_toggle_rejects_non_boolean_and_extra_fields(app, auth_client, body):
    workshop = FakeWorkshopService([_workshop_mod()])
    app.extensions["workshop_service"] = workshop

    response = auth_client.post("/api/workshop/3625223587/disable", json=body)

    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"
    assert workshop.calls == []


def test_unknown_valid_workshop_id_is_consistently_404(app, auth_client):
    app.extensions["workshop_service"] = FakeWorkshopService([_workshop_mod()])
    for suffix in ("enable", "disable"):
        response = auth_client.post(f"/api/workshop/9999/{suffix}", json={})
        assert response.status_code == 404
        assert response.json["error_code"] == "workshop_mod_not_found"
    for path, method in (
        ("/api/workshop/9999/open-page", "post"),
        ("/api/workshop/9999/open-folder", "get"),
    ):
        response = getattr(auth_client, method)(path, json={} if method == "post" else None)
        assert response.status_code == 404
        assert response.json["error_code"] == "workshop_mod_not_found"


def test_workshop_errors_map_game_running_and_dependents_to_structured_responses(app, auth_client):
    workshop = FakeWorkshopService([_workshop_mod()])
    app.extensions["workshop_service"] = workshop
    workshop.set_enabled = lambda *_a, **_k: (_ for _ in ()).throw(GameRunningError("running"))
    running = auth_client.post("/api/workshop/3625223587/enable", json={})
    assert running.status_code == 423
    assert running.json["error_code"] == "game_running"

    workshop.set_enabled = lambda *_a, **_k: (_ for _ in ()).throw(
        WorkshopDependencyError({"reason": "enabled_dependents", "dependents": ["1001"]})
    )
    conflict = auth_client.post("/api/workshop/3625223587/disable", json={})
    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "workshop_dependency_conflict"
    assert conflict.json["details"]["dependents"] == ["1001"]


def test_enabling_mod_with_workshop_ue4ss_dependency_is_blocked_by_manual_ue4ss(
    app, auth_client, tmp_path, monkeypatch
):
    game_root = Path(app.extensions["mod_service"].game_root)
    settings = game_root / "Mods" / "PalModSettings.ini"
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = "[PalModSettings]\nbGlobalEnableMod=False\n"
    settings.write_text(original, encoding="utf-8")
    workshop = SteamWorkshopService(
        tmp_path / "Steam", game_root, game_running=lambda: False,
        lock_root=app.config["DATA_DIR"],
    )
    mods = [
        WorkshopMod("1001", "Framework", "Framework", "Author", "1", (), ("UE4SS",), ("Pal/Binaries/Win64",), tmp_path / "1001", 1, True, None),
        WorkshopMod("1002", "Feature", "Feature", "Author", "1", ("1001",), ("Paks",), ("Pal/Content/Paks/~mods",), tmp_path / "1002", 1, True, None),
    ]
    monkeypatch.setattr(workshop, "scan", lambda *, force=False: mods)
    app.extensions["workshop_service"] = workshop
    win64 = game_root / "Pal" / "Binaries" / "Win64"
    (win64 / "dwmapi.dll").write_bytes(b"manual")

    response = auth_client.post("/api/workshop/1002/enable", json={})

    assert response.status_code == 409
    assert response.json["error_code"] == "ue4ss_conflict"
    assert settings.read_text(encoding="utf-8") == original


def test_workshop_ue4ss_and_manual_ue4ss_are_mutually_exclusive(app, auth_client, monkeypatch):
    workshop = FakeWorkshopService([_workshop_mod(install_types=["UE4SS"])])
    app.extensions["workshop_service"] = workshop
    win64 = Path(app.extensions["mod_service"].game_root) / "Pal" / "Binaries" / "Win64"
    (win64 / "dwmapi.dll").write_bytes(b"manual")

    blocked_enable = auth_client.post("/api/workshop/3625223587/enable", json={})
    assert blocked_enable.status_code == 409
    assert blocked_enable.json["error_code"] == "ue4ss_conflict"
    assert workshop.mods[0]["enabled"] is False

    (win64 / "dwmapi.dll").unlink()
    workshop.mods[0].update(enabled=True, status="enabled")
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    blocked_install = auth_client.post("/api/ue4ss/install-bundled", json={})
    assert blocked_install.status_code == 409
    assert blocked_install.json["error_code"] == "ue4ss_conflict"
    assert blocked_install.json["details"]["reason"] == "workshop_ue4ss_active"
    assert provider.calls == []


def test_workshop_open_page_uses_only_server_scanned_id_and_system_browser(app, auth_client):
    opened = []
    app.config["OPEN_URL"] = opened.append
    app.extensions["workshop_service"] = FakeWorkshopService([_workshop_mod()])

    response = auth_client.post("/api/workshop/3625223587/open-page", json={})

    assert response.status_code == 200
    assert opened == ["https://steamcommunity.com/sharedfiles/filedetails/?id=3625223587"]
    assert response.json["data"] == {"opened": True}


@pytest.mark.parametrize("body", [
    {"url": "https://evil.example"}, {"path": "F:/evil"},
    {"workshop_id": "9999"}, None,
])
def test_workshop_open_page_rejects_url_paths_extra_fields_and_non_object_body(app, auth_client, body):
    opened = []
    app.config["OPEN_URL"] = opened.append
    app.extensions["workshop_service"] = FakeWorkshopService([_workshop_mod()])

    response = auth_client.post("/api/workshop/3625223587/open-page", json=body)

    assert response.status_code == 400
    assert response.json["error_code"] == "invalid_input"
    assert opened == []


def test_workshop_open_folder_uses_scanned_path_only(app, auth_client):
    opened = []
    app.config["OPEN_FOLDER"] = opened.append
    app.extensions["workshop_service"] = FakeWorkshopService([_workshop_mod()])

    response = auth_client.get("/api/workshop/3625223587/open-folder")

    assert response.status_code == 200
    assert opened == ["F:/Steam/workshop/content/1623730/3625223587"]
    rejected = auth_client.get("/api/workshop/9999/open-folder?path=F:/evil")
    assert rejected.status_code == 404
    assert opened == ["F:/Steam/workshop/content/1623730/3625223587"]


def test_concurrent_workshop_ue4ss_enable_and_bundled_install_never_both_succeed(
    app, tmp_path, monkeypatch
):
    workshop_root = tmp_path / "Steam"
    game_root = Path(app.extensions["mod_service"].game_root)
    settings = game_root / "Mods" / "PalModSettings.ini"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("[PalModSettings]\nbGlobalEnableMod=False\n", encoding="utf-8")
    mod = WorkshopMod(
        "3625223587", "Workshop UE4SS", "WorkshopFramework", "Author", "1.0",
        (), ("UE4SS",), ("Pal/Binaries/Win64",),
        workshop_root / "steamapps/workshop/content/1623730/3625223587",
        1, True, None,
    )
    workshop = SteamWorkshopService(
        workshop_root, game_root, game_running=lambda: False,
        lock_root=app.config["DATA_DIR"],
    )
    monkeypatch.setattr(workshop, "scan", lambda *, force=False: [mod])
    app.extensions["workshop_service"] = workshop
    provider = FakeUe4ssProvider(_ue4ss_zip_bytes())
    app.extensions["ue4ss_provider"] = provider
    monkeypatch.setattr("backend.ue4ss_installer.is_palworld_running", lambda: False)

    installed = []
    def install_manual(root, *_args, **_kwargs):
        win64 = Path(root) / "Pal" / "Binaries" / "Win64"
        (win64 / "dwmapi.dll").write_bytes(b"manual")
        installed.append(True)
        return {"installed": True}
    monkeypatch.setattr("backend.ue4ss_installer.install_from_bytes", install_manual)

    barrier = threading.Barrier(2)
    responses = []
    def call(path):
        client = app.test_client(); client.get("/?token=test-token")
        barrier.wait(timeout=5)
        responses.append(client.post(path, json={}))

    threads = [
        threading.Thread(target=call, args=("/api/workshop/3625223587/enable",)),
        threading.Thread(target=call, args=("/api/ue4ss/install-bundled",)),
    ]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)

    assert sorted(response.status_code for response in responses) == [200, 409]
    active = workshop.active_ue4ss_mods()
    manual = (game_root / "Pal" / "Binaries" / "Win64" / "dwmapi.dll").is_file()
    assert not (active and manual)
    assert bool(installed) is manual


def test_internal_errors_do_not_leak_exception_text(app, auth_client, monkeypatch):
    secret = "SECRET-local-path"

    def fail():
        raise RuntimeError(secret)

    monkeypatch.setattr(app.extensions["mod_service"], "list_mods", fail)
    response = auth_client.get("/api/mods")
    assert response.status_code == 500
    assert response.json["error_code"] == "internal_error"
    assert secret not in response.get_data(as_text=True)
