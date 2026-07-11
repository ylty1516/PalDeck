from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from backend.app import create_app
from backend.smoke_check import run_http_smoke, smoke_report_path


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
        "index_four_views_and_petal_canvas",
        "health",
        "fresh_data_no_game_path",
        "appearance_get",
        "theme_aurora-glass",
        "theme_ivory-sakura",
        "theme_starlit-night",
        "petals_high",
        "petals_off",
        "default_background_webp",
    }


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
