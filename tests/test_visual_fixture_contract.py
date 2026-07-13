import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VISUAL = ROOT / "tests" / "visual"
FIXTURES = VISUAL / "fixtures"
VIEWS = ("mods", "import", "nexus", "settings", "credits")


def _walk(value):
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_fixture_files_are_fixed_safe_json_for_all_views():
    assert sorted(path.stem for path in FIXTURES.glob("*.json")) == sorted(VIEWS)
    for view in VIEWS:
        fixture = json.loads((FIXTURES / f"{view}.json").read_text(encoding="utf-8"))
        assert fixture["view"] == view
        assert isinstance(fixture["api"], dict)
        assert all(path.startswith("/api/") for path in fixture["api"])

        assert list(_walk(fixture)), "fixture must contain deterministic content"

    nexus = json.loads((FIXTURES / "nexus.json").read_text(encoding="utf-8"))
    serialized = json.dumps(nexus, ensure_ascii=False).lower()
    assert '"adultcontent": true' not in serialized
    for forbidden in ("favorite", "favourite", "收藏", "一键安装", "download_url", "downloadurl", "下载地址"):
        assert forbidden not in serialized
    items = nexus["api"]["/api/nexus/popular"]["items"]
    assert items and all(item.get("adultContent") is False for item in items)


def test_fixture_server_contract_is_loopback_only_and_test_injects_whitelisted_view():
    source = (VISUAL / "fixture_server.py").read_text(encoding="utf-8")
    assert 'HOST = "127.0.0.1"' in source
    assert "ThreadingHTTPServer" in source
    assert "ALLOWED_VIEWS" in source
    assert "__fixture__.js" in source
    assert "window.__VISUAL_READY__" in source
    assert "query" in source.lower() or "parse_qs" in source
    assert "../frontend" in source.replace("\\", "/") or '"frontend"' in source


def test_fixture_server_serves_real_frontend_and_fixed_api_on_random_port(tmp_path):
    ready = tmp_path / "port"
    process = subprocess.Popen(
        [sys.executable, str(VISUAL / "fixture_server.py"), "--port", "0", "--ready-file", str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.02)
        port = int(ready.read_text(encoding="ascii"))
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        index = opener.open(f"http://127.0.0.1:{port}/index.html?view=nexus").read().decode("utf-8")
        assert '<script type="module" src="app.js"></script>' in index
        assert '<script type="module" src="/__fixture__.js"></script>' in index
        css = opener.open(f"http://127.0.0.1:{port}/styles.css").read().decode("utf-8")
        assert ".app-shell" in css
        payload = json.load(opener.open(f"http://127.0.0.1:{port}/api/nexus/popular?sort=downloads"))
        assert payload["ok"] is True
        assert payload["data"]["items"][0]["adultContent"] is False
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(f"http://127.0.0.1:{port}/index.html?view=not-a-view")
        assert error.value.code == 400
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_visual_assets_are_excluded_from_release_build_and_baselines_are_never_written():
    build = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
    assert "tests/visual" not in build.replace("\\", "/").lower()
    assert "--add-data" in build and "frontend" in build

    capture = (ROOT / "scripts" / "capture_ui.ps1").read_text(encoding="utf-8-sig")
    compare = (ROOT / "scripts" / "compare_ui_screenshots.py").read_text(encoding="utf-8")
    assert "baselines" not in capture.lower()
    assert "copyfile" not in compare.lower()
    assert "shutil.copy" not in compare.lower()


def test_capture_script_covers_matrix_edge_cleanup_and_stability_budget():
    source = (ROOT / "scripts" / "capture_ui.ps1").read_text(encoding="utf-8-sig")
    for view in VIEWS:
        assert f'"{view}"' in source
    for size in ("1600x1000", "1280x820", "960x640"):
        assert size in source
    assert "msedge.exe" in source.lower()
    assert "--headless" in source
    assert "--virtual-time-budget=3000" in source
    assert "finally" in source
    assert "fixture_server.py" in source
    assert "OutputPath" in source


def test_compare_requires_approval_for_missing_baseline(tmp_path):
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "approved"
    candidate.mkdir()
    command = [sys.executable, str(ROOT / "scripts" / "compare_ui_screenshots.py"), str(candidate), str(baseline)]

    denied = subprocess.run(command, capture_output=True, text=True, check=False)
    assert denied.returncode != 0
    assert "needs approval" in (denied.stdout + denied.stderr).lower()
    allowed = subprocess.run([*command, "--allow-missing-baseline"], capture_output=True, text=True, check=False)
    assert allowed.returncode == 0
    assert not baseline.exists(), "comparison must never create or approve a baseline"


def test_compare_rejects_wrong_dimensions_and_blank_content(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "approved"
    candidate.mkdir()
    baseline.mkdir()
    image_module.new("RGB", (10, 10), "white").save(candidate / "mods-1600x1000.png")
    image_module.new("RGB", (11, 10), "black").save(baseline / "mods-1600x1000.png")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compare_ui_screenshots.py"), str(candidate), str(baseline)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "dimension" in output or "尺寸" in output
