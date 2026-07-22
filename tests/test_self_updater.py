from __future__ import annotations

import hashlib
import zipfile

import pytest

from backend import self_updater
from backend.self_updater import (
    _download_verified_asset,
    _extract_exe_from_zip,
    _pick_asset,
    _select_release_assets,
)


RELEASE = "https://github.com/ylty1516/PalDeck/releases/download/v2.3.0"


def test_update_trust_root_is_paldeck_only():
    assert self_updater.TRUSTED_GITHUB_OWNER == "ylty1516"
    assert self_updater.TRUSTED_GITHUB_REPO == "PalDeck"


def test_fetch_latest_release_uses_only_paldeck_api(monkeypatch):
    calls = []
    monkeypatch.setattr(self_updater, "_api_get", lambda url: calls.append(url) or {"tag_name": "v2.3.0"})
    assert self_updater.fetch_latest_release()["tag_name"] == "v2.3.0"
    assert calls == ["https://api.github.com/repos/ylty1516/PalDeck/releases/latest"]


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


def test_verified_update_requires_checksum_sidecar(tmp_path):
    asset = {
        "name": "PalDeck.exe",
        "browser_download_url": f"{RELEASE}/PalDeck.exe",
    }
    with pytest.raises(RuntimeError, match="checksum"):
        _download_verified_asset(asset, None, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_verified_update_deletes_download_when_checksum_is_wrong(tmp_path, monkeypatch):
    asset = {
        "name": "PalDeck.exe",
        "browser_download_url": f"{RELEASE}/PalDeck.exe",
    }
    checksum = {
        "name": "PalDeck.exe.sha256",
        "browser_download_url": f"{RELEASE}/PalDeck.exe.sha256",
    }

    def fake_download(url, destination):
        destination.write_bytes(
            ("0" * 64 + "  PalDeck.exe\n").encode() if url.endswith(".sha256") else b"payload"
        )

    monkeypatch.setattr("backend.self_updater._download", fake_download)
    with pytest.raises(RuntimeError, match="SHA-256"):
        _download_verified_asset(asset, checksum, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_verified_update_accepts_matching_hash_and_exact_asset_name(tmp_path, monkeypatch):
    payload = b"verified release payload"
    digest = hashlib.sha256(payload).hexdigest()
    assets = [
        {
            "name": "PalDeck-v2.3.0-windows-portable.zip",
            "browser_download_url": f"{RELEASE}/PalDeck-v2.3.0-windows-portable.zip",
        },
        {
            "name": "PalDeck-v2.3.0-windows-portable.zip.sha256",
            "browser_download_url": f"{RELEASE}/PalDeck-v2.3.0-windows-portable.zip.sha256",
        },
    ]
    asset, checksum = _select_release_assets(assets)

    def fake_download(url, destination):
        destination.write_bytes(
            (f"{digest}  {asset['name']}\n").encode() if url.endswith(".sha256") else payload
        )

    monkeypatch.setattr("backend.self_updater._download", fake_download)
    downloaded = _download_verified_asset(asset, checksum, tmp_path)
    assert downloaded.read_bytes() == payload
    assert not (tmp_path / checksum["name"]).exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/ylty1516/palworld-mod-manager/releases/download/v2.3.0/PalDeck.exe",
        "https://github.com/ylty1516/PalDeck.evil/releases/download/v2.3.0/PalDeck.exe",
        "https://github.com.evil.example/ylty1516/PalDeck/releases/download/v2.3.0/PalDeck.exe",
        "http://github.com/ylty1516/PalDeck/releases/download/v2.3.0/PalDeck.exe",
        "https://github.com/ylty1516/PalDeck/releases/download/v2.3.0/Other.exe",
    ],
)
def test_validate_release_url_rejects_every_non_paldeck_origin(url):
    with pytest.raises(RuntimeError):
        self_updater._validate_release_url(url, "PalDeck.exe")
