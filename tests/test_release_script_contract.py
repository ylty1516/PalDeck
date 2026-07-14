from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from backend.version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_build_binds_artifact_to_clean_packaged_source_commit():
    script = read_script("build_portable.ps1")

    for path in (
        '"backend/"',
        '"frontend/"',
        '"launcher.py"',
        '"assets/"',
        '"bundled_mods/"',
        '"third_party/"',
        '"package.json"',
        '"requirements-lock*"',
        '"scripts/build_portable.ps1"',
        '"scripts/create_portable_zip.py"',
        '"build_exe.bat"',
    ):
        assert path in script

    assert "git status --porcelain=v1 -- @packagedSourcePaths" in script
    assert "git log -1 --format=%H -- @packagedSourcePaths" in script
    assert 'git show -s --format=%ct $sourceCommit' in script
    assert '$env:SOURCE_DATE_EPOCH = $sourceEpoch' in script
    assert 'Test-Path "Env:PALMOD_VERSION"' in script
    assert 'SOURCE_COMMIT: $sourceCommit' in script
    assert 'Source commit: $sourceCommit' in script


@pytest.mark.parametrize(
    ("exit_code", "expected_returncode", "expected_failure"),
    [(0, 0, False), (1, 1, True)],
)
def test_invoke_checked_preserves_native_stderr_and_uses_exit_code(
    tmp_path, exit_code, expected_returncode, expected_failure
):
    script = read_script("build_portable.ps1")
    function_match = re.search(
        r"function Invoke-Checked \{.*?^\}", script, flags=re.MULTILINE | re.DOTALL
    )
    assert function_match is not None

    fake_command = tmp_path / "fake-native.cmd"
    fake_command.write_text(
        f"@echo off\r\necho native-stderr-marker 1>&2\r\nexit /b {exit_code}\r\n",
        encoding="ascii",
    )
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + function_match.group(0)
        + "\ntry {\n"
        + f"  & {{ Invoke-Checked -Label 'fake native' -FilePath '{fake_command}' }} 2>&1 | Out-String | Write-Output\n"
        + "} catch {\n"
        + "  Write-Output $_.Exception.Message\n"
        + "  exit 1\n"
        + "}\n",
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == expected_returncode, output
    assert "native-stderr-marker" in output
    assert ("exit 1" in output) is expected_failure


def test_release_version_assets_and_node_tests_are_derived_and_packaged():
    script = read_script("build_portable.ps1")

    assert APP_VERSION == "2.2.4"
    assert '((Join-Path $repoRoot "third_party") + $separator + "third_party")' in script
    assert 'Invoke-Checked "运行 Node 测试" "npm" @("test")' in script
    assert script.index('Node 语法检查：') < script.index('Invoke-Checked "运行 Node 测试"')
    assert "PalDeck-v2.1.0" not in script
    assert "PalDeck-v2.0.0" not in script


def test_portable_readme_documents_v21_scope_and_new_repository():
    script = read_script("build_portable.ps1")

    for text in (
        "https://github.com/ylty1516/PalDeck",
        "只管理 Steam 客户端已订阅并下载的 Workshop 模组启停",
        "不提供 Workshop 订阅、取消订阅或删除",
        "Workshop UE4SS 与手动或内置 UE4SS 互斥",
        "Okaetsu/RE-UE4SS",
    ):
        assert text in script


def test_smoke_script_requires_all_v21_report_items():
    script = read_script("smoke_portable.ps1")
    for name in (
        "index_five_views_and_release_markers",
        "workshop_empty_state",
        "bundled_ue4ss_metadata",
        "petal_style_natural",
        "petal_style_watercolor",
        "petal_style_minimal",
        "v22_responsive_shell",
        "import_queue_empty",
        "nexus_adult_filtered",
    ):
        assert f'"{name}"' in script


def test_smoke_version_cannot_be_overridden_by_environment():
    script = read_script("smoke_portable.ps1")

    clear_override = 'Remove-Item Env:PALMOD_VERSION -ErrorAction SilentlyContinue'
    read_backend_version = 'from backend.version import APP_VERSION; print(APP_VERSION)'
    assert clear_override in script
    assert read_backend_version in script
    assert script.index(clear_override) < script.index(read_backend_version)
