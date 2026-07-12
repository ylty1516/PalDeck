from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.credits import CATALOG, TRUSTED_LINKS, catalog_payload


REQUIRED_NAMES = {
    "Okaetsu/RE-UE4SS",
    "UE4SS-RE/RE-UE4SS",
    "Flask",
    "pywebview",
    "Pillow",
    "PyInstaller",
    "Palworld Modding Docs",
}
REQUIRED_FIELDS = {"id", "name", "purpose", "author", "license", "version", "source_url", "core"}
# Verified against Pillow 12.3.0 wheel METADATA (License-Expression) and the lock file.
AUTHORITATIVE_DIRECT_METADATA = {
    "Pillow": {"version": "12.3.0", "license": "MIT-CMU"},
}


def test_catalog_is_fixed_complete_offline_metadata():
    assert isinstance(CATALOG, tuple)
    assert all(isinstance(item, MappingProxyType) for item in CATALOG)
    assert REQUIRED_NAMES <= {item["name"] for item in CATALOG}
    assert all(REQUIRED_FIELDS <= set(item) for item in CATALOG)
    assert all(str(item["source_url"]).startswith("https://") for item in CATALOG)
    assert all(item["source_url"] == TRUSTED_LINKS[item["id"]] for item in CATALOG)
    assert len({item["id"] for item in CATALOG}) == len(CATALOG)
    ue4ss = next(item for item in CATALOG if item["name"] == "UE4SS-RE/RE-UE4SS")
    assert ue4ss["license"] == "MIT"
    assert "Narknon" in ue4ss["author"]
    assert "Copyright (c) 2022 Narknon" in ue4ss["copyright"]
    okaetsu = next(item for item in CATALOG if item["name"] == "Okaetsu/RE-UE4SS")
    assert "Palworld 专用构建" in okaetsu["purpose"]


def test_locked_pillow_metadata_matches_authoritative_wheel_metadata():
    pillow = next(item for item in CATALOG if item["name"] == "Pillow")
    assert {key: pillow[key] for key in ("version", "license")} == AUTHORITATIVE_DIRECT_METADATA["Pillow"]
    assert "MIT-CMU" in pillow["license_text"]
    assert "Historical Permission Notice and Disclaimer" not in pillow["license_text"]


def test_catalog_and_trusted_links_reject_mutation():
    with pytest.raises(TypeError):
        CATALOG[0]["name"] = "tampered"
    with pytest.raises(TypeError):
        TRUSTED_LINKS["evil"] = "https://evil.example"


def test_catalog_payload_returns_detached_json_ready_values():
    payload = catalog_payload()
    assert isinstance(payload, list)
    assert payload[0] is not CATALOG[0]
    payload[0]["name"] = "changed response"
    assert CATALOG[0]["name"] != "changed response"
