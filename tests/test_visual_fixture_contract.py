import http.cookiejar
import json
import re
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
        assert fixture["api"]["/api/health"]["version"] == "2.3.1-fixture"
        assert "/api/trash" in fixture["api"]

        assert list(_walk(fixture)), "fixture must contain deterministic content"

    mods = json.loads((FIXTURES / "mods.json").read_text(encoding="utf-8"))
    local = [item for item in mods["api"]["/api/mods"] if item.get("source") != "steam_workshop"]
    assert any(item.get("externally_discovered") is True for item in local)
    assert any(item.get("externally_discovered") is False for item in local)
    assert any(
        item.get("adjustable_values") is True
        and item.get("adjustable_value_count", 0) > 0
        for item in local
    )
    trash = mods["api"]["/api/trash"]
    assert trash["items"] and all("payload" not in key.casefold() for item in trash["items"] for key in item)

    settings = json.loads((FIXTURES / "settings.json").read_text(encoding="utf-8"))
    ue4ss = settings["api"]["/api/ue4ss/status"]
    assert ue4ss["managed"] is True and ue4ss["integrity"] == "healthy"
    assert ue4ss["owned_files"] > 0
    assert settings["api"]["/api/mods/ignored"]["count"] >= 0

    nexus = json.loads((FIXTURES / "nexus.json").read_text(encoding="utf-8"))
    serialized = json.dumps(nexus, ensure_ascii=False).lower()
    assert '"adultcontent": true' not in serialized
    for forbidden in ("favorite", "favourite", "收藏", "一键安装", "download_url", "downloadurl", "下载地址"):
        assert forbidden not in serialized
    items = nexus["api"]["/api/nexus/popular"]["items"]
    assert items and all(item.get("adultContent") is False for item in items)


def test_fixture_server_contract_is_loopback_only_and_waits_for_target_render():
    source = (VISUAL / "fixture_server.py").read_text(encoding="utf-8")
    assert 'HOST = "127.0.0.1"' in source
    assert "ThreadingHTTPServer" in source
    assert "ALLOWED_VIEWS" in source
    assert "__fixture__.js" in source
    assert "window.__VISUAL_READY__ = true" in source
    assert 'dataset.visualReady = "true"' in source
    assert "view-${requested}" in source
    assert 'classList.contains("active")' in source
    for selector in ("#modList", "#nexusStatus", "#ue4ssStatus", "#creditsCore"):
        assert selector in source
    assert "await sleep(250)" not in source
    assert "throw new Error" in source
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


def test_capture_script_covers_matrix_ready_handshake_timeout_and_process_tree_cleanup():
    source = (ROOT / "scripts" / "capture_ui.ps1").read_text(encoding="utf-8-sig")
    for view in VIEWS:
        assert f'"{view}"' in source
    compare = (ROOT / "scripts" / "compare_ui_screenshots.py").read_text(encoding="utf-8")
    for size in ("1600x1000", "1280x820", "1236x771", "960x640"):
        assert size in source
        assert size in compare
    for token in (
        "msedge.exe", "--headless", "--virtual-time-budget=3000", "finally",
        "fixture_server.py", "OutputPath", "--ready-dir", "capture=", ".ready",
        "WaitForExit", "taskkill.exe", "/T", "/F", "Quote-NativeArgument",
    ):
        assert token in source


def test_capture_native_argument_quoting_preserves_paths_with_spaces(tmp_path):
    source = (ROOT / "scripts" / "capture_ui.ps1").read_text(encoding="utf-8-sig")
    quote = re.search(r"function Quote-NativeArgument.*?^}", source, re.MULTILINE | re.DOTALL)
    join = re.search(r"function Join-NativeArguments.*?^}", source, re.MULTILINE | re.DOTALL)
    assert quote and join
    probe = tmp_path / "quote-probe.ps1"
    probe.write_text(
        '$ErrorActionPreference = "Stop"\n'
        + quote.group(0) + "\n" + join.group(0)
        + '\nJoin-NativeArguments @("plain", "C:\\\\folder with spaces\\\\file.png")\n',
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(probe)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'plain "C:\\\\folder with spaces\\\\file.png"' in result.stdout.strip()


def test_fixture_ready_endpoint_atomically_records_unique_capture_token(tmp_path):
    ready = tmp_path / "port"
    markers = tmp_path / "ready markers"
    process = subprocess.Popen(
        [sys.executable, str(VISUAL / "fixture_server.py"), "--port", "0", "--ready-file", str(ready), "--ready-dir", str(markers)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.02)
        port = int(ready.read_text(encoding="ascii"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/__visual_ready__?capture=abc123&view=nexus",
            data=b"", method="POST",
        )
        payload = json.load(urllib.request.urlopen(request))
        assert payload == {"ok": True}
        assert (markers / "abc123.ready").read_text(encoding="ascii") == "nexus"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/__visual_ready__?capture=../escape&view=nexus",
                data=b"", method="POST",
            ))
        assert error.value.code == 400
    finally:
        process.terminate()
        process.wait(timeout=5)


def _save_valid_candidate(image_module, directory):
    directory.mkdir()
    image = image_module.new("RGB", (1600, 1000), "white")
    for x in range(10, 20):
        image.putpixel((x, 10), (0, 0, 0))
    image.save(directory / "mods-1600x1000.png")


def _run_compare(candidate, baseline, *options, full_matrix=False):
    matrix = [] if full_matrix else ["--views", "mods", "--sizes", "1600x1000"]
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compare_ui_screenshots.py"), str(candidate), str(baseline), *matrix, *options],
        capture_output=True, text=True, check=False,
    )


def test_compare_requires_approval_only_after_validating_candidate(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "approved"
    _save_valid_candidate(image_module, candidate)

    denied = _run_compare(candidate, baseline)
    assert denied.returncode != 0
    assert "needs approval" in (denied.stdout + denied.stderr).lower()
    allowed = _run_compare(candidate, baseline, "--allow-missing-baseline")
    assert allowed.returncode == 0
    assert not baseline.exists(), "comparison must never create or approve a baseline"


def test_compare_rejects_incomplete_default_five_by_three_matrix(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "approved"
    _save_valid_candidate(image_module, candidate)
    result = _run_compare(candidate, baseline, "--allow-missing-baseline", full_matrix=True)
    assert result.returncode != 0
    assert "missing candidate screenshot" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize("kind", ["missing", "bad-png", "wrong-size", "blank"])
def test_compare_rejects_invalid_candidate_even_when_missing_baseline_is_allowed(tmp_path, kind):
    image_module = pytest.importorskip("PIL.Image")
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "approved"
    candidate.mkdir()
    path = candidate / "mods-1600x1000.png"
    if kind == "bad-png":
        path.write_text("not a png", encoding="utf-8")
    elif kind == "wrong-size":
        image_module.new("RGB", (10, 10), "white").save(path)
    elif kind == "blank":
        image_module.new("RGB", (1600, 1000), "white").save(path)

    result = _run_compare(candidate, baseline, "--allow-missing-baseline")
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    expected = {"missing": "no candidate", "bad-png": "invalid png", "wrong-size": "dimension", "blank": "empty content"}
    assert expected[kind] in output
