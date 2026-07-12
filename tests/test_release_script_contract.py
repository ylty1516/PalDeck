from __future__ import annotations

from pathlib import Path


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


def test_smoke_version_cannot_be_overridden_by_environment():
    script = read_script("smoke_portable.ps1")

    clear_override = 'Remove-Item Env:PALMOD_VERSION -ErrorAction SilentlyContinue'
    read_backend_version = 'from backend.version import APP_VERSION; print(APP_VERSION)'
    assert clear_override in script
    assert read_backend_version in script
    assert script.index(clear_override) < script.index(read_backend_version)
