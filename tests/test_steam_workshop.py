import json
import os
from pathlib import Path

import pytest

from backend.steam_workshop import SteamWorkshopService


def write_library_config(steam_root: Path, libraries: list[Path]) -> None:
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    entries = " ".join(
        f'"{index}" {{ "path" "{library.as_posix()}" }}'
        for index, library in enumerate(libraries, 1)
    )
    (steamapps / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ {entries} }}', encoding="utf-8"
    )


def write_acf(library: Path, ids: list[str], *, mixed_case: bool = False) -> Path:
    workshop = library / "steamapps" / "workshop"
    workshop.mkdir(parents=True, exist_ok=True)
    entries = " ".join(f'"{item_id}" {{ "manifest" "1" }}' for item_id in ids)
    root = "aPpWoRkShOp" if mixed_case else "AppWorkshop"
    installed = "wOrKsHoPiTeMsInStAlLeD" if mixed_case else "WorkshopItemsInstalled"
    path = workshop / "appworkshop_1623730.acf"
    path.write_text(
        f'"{root}" {{ "appid" "1623730" "{installed}" {{ {entries} }} }}',
        encoding="utf-8",
    )
    return path


def valid_info(**overrides):
    info = {
        "ModName": "Example Mod",
        "PackageName": "Example_Mod",
        "Author": "Author",
        "Version": "1.2.3",
        "Dependencies": ["Base_Mod", "3625000000"],
        "InstallRule": [
            {"Type": "UE4SS", "Target": "Pal/Binaries/Win64"},
            {"Type": "Paks", "Target": "Pal/Content/Paks/~mods"},
        ],
    }
    info.update(overrides)
    return info


def write_item(library: Path, item_id: str, info=None) -> Path:
    item = library / "steamapps" / "workshop" / "content" / "1623730" / item_id
    item.mkdir(parents=True, exist_ok=True)
    (item / "Info.json").write_text(json.dumps(info or valid_info()), encoding="utf-8")
    return item


def test_scans_two_libraries_from_only_fixed_palworld_paths(tmp_path):
    steam = tmp_path / "Steam"
    second = tmp_path / "SecondLibrary"
    write_library_config(steam, [second])
    write_acf(steam, ["1001"], mixed_case=True)
    write_acf(second, ["3625223587"])
    write_item(steam, "1001")
    expected = write_item(second, "3625223587")
    # These sentinels must not be traversed merely because they exist.
    (second / "steamapps" / "common" / "Sentinel").mkdir(parents=True)
    (second / "steamapps" / "workshop" / "content" / "999999" / "Sentinel").mkdir(parents=True)

    service = SteamWorkshopService(steam)
    mods = service.scan(force=True)

    assert [mod.workshop_id for mod in mods] == ["1001", "3625223587"]
    assert mods[1].source_dir == expected
    assert mods[1].install_types == ("UE4SS", "Paks")
    assert mods[1].install_targets == (
        "Pal/Binaries/Win64",
        "Pal/Content/Paks/~mods",
    )
    assert all(
        "workshop/content/1623730" in path.replace("\\", "/")
        or path.replace("\\", "/").endswith("workshop/appworkshop_1623730.acf")
        for path in service.last_scan_paths
    )
    assert not any("999999" in path or "common" in path for path in service.last_scan_paths)


def test_manifest_ids_must_be_positive_decimal_and_appid_must_match(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["123", "0", "-1", "../escape", "12x"])
    write_item(steam, "123")
    write_item(steam, "0")

    mods = SteamWorkshopService(steam).scan(force=True)

    assert [mod.workshop_id for mod in mods] == ["123"]


def test_missing_manifest_recovers_only_direct_numeric_children(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_item(steam, "2002")
    write_item(steam, "not-an-id")
    nested = steam / "steamapps" / "workshop" / "content" / "1623730" / "container"
    write_item(nested, "3003")

    mods = SteamWorkshopService(steam).scan(force=True)

    assert [mod.workshop_id for mod in mods] == ["2002"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ModName": 7}, "ModName"),
        ({"ModName": "x" * 257}, "ModName"),
        ({"PackageName": "../Escape"}, "PackageName"),
        ({"Dependencies": "Base"}, "Dependencies"),
        ({"InstallRule": [{"Type": 1, "Target": "Paks"}]}, "InstallRule"),
        ({"InstallRule": [{"Type": "Paks", "Target": 1}]}, "InstallRule"),
    ],
)
def test_invalid_info_fields_return_disabled_invalid_record(tmp_path, change, message):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["4004"])
    write_item(steam, "4004", valid_info(**change))

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.workshop_id == "4004"
    assert mod.valid is False
    assert message in mod.error
    assert mod.to_dict()["can_toggle"] is False


def test_malformed_json_and_non_regular_info_are_invalid_records(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["5005", "5006"])
    broken = write_item(steam, "5005") / "Info.json"
    broken.write_text("{", encoding="utf-8")
    item = write_item(steam, "5006")
    (item / "Info.json").unlink()
    (item / "Info.json").mkdir()

    mods = SteamWorkshopService(steam).scan(force=True)

    assert [mod.valid for mod in mods] == [False, False]
    assert all(mod.error for mod in mods)


def test_to_dict_has_read_only_workshop_identity(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["6006"])
    write_item(steam, "6006")

    value = SteamWorkshopService(steam).scan(force=True)[0].to_dict()

    assert value["source"] == "steam_workshop"
    assert value["id"] == "steam-workshop:6006"
    assert value["can_delete"] is False
    assert value["can_toggle"] is True
    assert value["dependencies"] == ["Base_Mod", "3625000000"]


def test_cache_invalidates_on_info_change_and_force_bypasses(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["7007"])
    info_path = write_item(steam, "7007") / "Info.json"
    service = SteamWorkshopService(steam)

    first = service.scan(force=True)
    assert service.scan() is first

    info_path.write_text(json.dumps(valid_info(ModName="Changed Mod")), encoding="utf-8")
    stat = info_path.stat()
    os.utime(info_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    changed = service.scan()
    assert changed is not first
    assert changed[0].mod_name == "Changed Mod"

    forced = service.scan(force=True)
    assert forced is not changed


def test_cache_revalidates_item_path_reparse_state(tmp_path, monkeypatch):
    from backend import steam_workshop

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["8005"])
    item = write_item(steam, "8005")
    original = steam_workshop._is_reparse
    replaced = False

    def simulated_reparse(path):
        return (replaced and path == item) or original(path)

    monkeypatch.setattr(steam_workshop, "_is_reparse", simulated_reparse)
    service = SteamWorkshopService(steam)
    first = service.scan(force=True)
    assert first[0].valid is True

    replaced = True
    rescanned = service.scan()

    assert rescanned is not first
    assert rescanned[0].valid is False
    assert "reparse" in rescanned[0].error.casefold()


def test_reparse_content_root_invalidates_manifest_items(tmp_path, monkeypatch):
    from backend import steam_workshop

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["8006"])
    write_item(steam, "8006")
    content = steam / "steamapps" / "workshop" / "content" / "1623730"
    original = steam_workshop._is_reparse
    monkeypatch.setattr(
        steam_workshop,
        "_is_reparse",
        lambda path: path == content or original(path),
    )

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert "reparse" in mod.error.casefold()


def test_content_root_symlink_is_rejected_when_supported(tmp_path):
    steam = tmp_path / "Steam"
    external = tmp_path / "ExternalContent"
    write_library_config(steam, [])
    write_acf(steam, ["8007"])
    external_item = external / "8007"
    external_item.mkdir(parents=True)
    (external_item / "Info.json").write_text(json.dumps(valid_info()), encoding="utf-8")
    content = steam / "steamapps" / "workshop" / "content" / "1623730"
    content.parent.mkdir(parents=True)
    try:
        content.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert "reparse" in mod.error.casefold() or "symbolic" in mod.error.casefold()


def test_item_symlink_is_rejected_when_supported(tmp_path):
    steam = tmp_path / "Steam"
    external = tmp_path / "External"
    write_library_config(steam, [])
    write_acf(steam, ["8008"])
    write_item(external, "real")
    content = steam / "steamapps" / "workshop" / "content" / "1623730"
    content.mkdir(parents=True)
    try:
        (content / "8008").symlink_to(
            external / "steamapps" / "workshop" / "content" / "1623730" / "real",
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert "reparse" in mod.error.casefold() or "symbolic" in mod.error.casefold()
