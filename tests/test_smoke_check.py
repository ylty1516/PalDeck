from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from backend.app import create_app
from backend.smoke_check import run_http_smoke, smoke_context, smoke_report_path


def test_smoke_report_path_only_accepts_f_drive_file():
    assert smoke_report_path(None) is None
    assert smoke_report_path("") is None
    assert smoke_report_path(r"C:\temp\report.json") is None
    assert smoke_report_path(r"relative\report.json") is None
    accepted = smoke_report_path(r"F:\temp\paldeck-report.json")
    assert accepted is not None
    assert accepted.drive.casefold() == "f:"


def test_http_smoke_uses_real_cookie_session_and_records_all_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("PALMOD_GAME_PATH", raising=False)
    monkeypatch.setattr("backend.self_updater.is_frozen", lambda: True)
    application = create_app(
        root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / "fresh-data",
        session_token="smoke-session-token",
        testing=True,
    )
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report_path = tmp_path / "report.json"
    try:
        report = run_http_smoke(
            f"http://127.0.0.1:{server.server_port}/",
            "smoke-session-token",
            report_path,
            frozen=True,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert saved["ok"] is True
    assert all(item["pass"] is True for item in saved["items"])
    assert {item["name"] for item in saved["items"]} == {
        "index_five_views_and_release_markers",
        "v22_responsive_shell",
        "import_queue_empty",
        "nexus_adult_filtered",
        "trash_lifecycle_markers",
        "external_mod_markers",
        "ue4ss_lifecycle_markers",
        "paldeck_update_origin",
        "health",
        "fresh_data_no_game_path",
        "workshop_empty_state",
        "bundled_ue4ss_metadata",
        "appearance_get",
        "theme_aurora-glass",
        "theme_ivory-sakura",
        "theme_starlit-night",
        "petal_style_natural",
        "petal_style_watercolor",
        "petal_style_minimal",
        "petals_high",
        "petals_off",
        "default_background_webp",
    }


def test_smoke_entry_requires_frozen_handshake_marker_and_tree_owned_script():
    root = Path(__file__).resolve().parents[1]
    data_dir = root / ".tmp" / f"smoke-context-{uuid.uuid4().hex}"
    report_path = data_dir / "report.json"
    handshake = uuid.uuid4().hex
    marker = data_dir / f".paldeck-smoke-{handshake}"
    data_dir.mkdir(parents=True)
    try:
        assert smoke_context(str(report_path), handshake, data_dir, frozen=False) is None
        assert smoke_context(str(report_path), handshake, data_dir, frozen=True) is None
        marker.write_text(handshake, encoding="ascii")
        context = smoke_context(str(report_path), handshake, data_dir, frozen=True)
        assert context is not None
        assert context.report_path == report_path.resolve()
        assert context.marker_path == marker.resolve()
        report_path.write_text("{}", encoding="ascii")
        assert smoke_context(str(report_path), handshake, data_dir, frozen=True) is None

        smoke_script = (root / "scripts" / "smoke_portable.ps1").read_text(encoding="utf-8-sig")
        assert "ParentProcessId" in smoke_script
        assert "$started.Id" in smoke_script
        assert "Get-ProcessTreeIds" in smoke_script
        assert "Get-Process -Name" not in smoke_script
        assert "$before" not in smoke_script
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_http_smoke_failure_writes_error_report(tmp_path):
    report_path = tmp_path / "failed.json"
    with pytest.raises(Exception):
        run_http_smoke(
            "http://127.0.0.1:1/", "unused-token", report_path, frozen=True,
        )
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["ok"] is False
    assert saved["error"]
    assert saved["items"] == []
