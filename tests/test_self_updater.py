from __future__ import annotations

import zipfile

from backend.self_updater import _extract_exe_from_zip, _pick_asset


def test_pick_asset_prefers_paldeck_portable_names_and_keeps_legacy_compatibility():
    assets = [
        {"name": "PalMod.exe"},
        {"name": "PalDeck-v2.0.0-windows-portable.zip"},
        {"name": "unrelated.zip"},
    ]
    assert _pick_asset(assets)["name"] == "PalDeck-v2.0.0-windows-portable.zip"
    assert _pick_asset([{"name": "PalDeck.exe"}, *assets])["name"] == "PalDeck.exe"
    assert _pick_asset([{"name": "PalMod.exe"}])["name"] == "PalMod.exe"


def test_extract_exe_prefers_paldeck_over_other_executables(tmp_path):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("PalDeck-portable/PalMod.exe", b"legacy")
        bundle.writestr("PalDeck-portable/PalDeck.exe", b"current")

    destination = tmp_path / "new.exe"
    assert _extract_exe_from_zip(archive, destination) == destination
    assert destination.read_bytes() == b"current"
