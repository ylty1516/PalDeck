import json
import os
from pathlib import Path

import pytest

from backend.mod_service import GameRunningError
from backend.steam_workshop import SteamWorkshopService, WorkshopDependencyError


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
    assert mod.error == "invalid_info: Workshop metadata is invalid"
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
    assert service.scan() == first
    assert service.scan() is not first

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
    assert rescanned[0].error == "unsafe_path: Workshop path is unsafe"


def test_cache_revalidates_info_file_reparse_state(tmp_path, monkeypatch):
    from backend import steam_workshop

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["8004"])
    info = write_item(steam, "8004") / "Info.json"
    original = steam_workshop._is_reparse
    replaced = False

    monkeypatch.setattr(
        steam_workshop,
        "_is_reparse",
        lambda path: (replaced and path == info) or original(path),
    )
    service = SteamWorkshopService(steam)
    assert service.scan(force=True)[0].valid is True

    replaced = True
    rescanned = service.scan()

    assert rescanned[0].valid is False
    assert rescanned[0].error == "unsafe_path: Workshop path is unsafe"


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
    assert mod.error == "unsafe_path: Workshop path is unsafe"


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
    assert mod.error == "unsafe_path: Workshop path is unsafe"


def test_force_scan_refreshes_library_configuration(tmp_path):
    steam = tmp_path / "Steam"
    second = tmp_path / "AddedLibrary"
    write_library_config(steam, [])
    service = SteamWorkshopService(steam)
    assert service.scan(force=True) == []

    write_library_config(steam, [second])
    write_acf(second, ["9001"])
    write_item(second, "9001")

    assert [mod.workshop_id for mod in service.scan(force=True)] == ["9001"]


def test_cached_results_are_not_mutable_by_caller(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9002"])
    write_item(steam, "9002")
    service = SteamWorkshopService(steam)

    returned = service.scan(force=True)
    returned.clear()

    assert [mod.workshop_id for mod in service.scan()] == ["9002"]


def test_oversized_info_is_invalid_with_sanitized_error(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9003"])
    info = write_item(steam, "9003") / "Info.json"
    info.write_bytes(b"{" + b" " * (1024 * 1024))

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert mod.error == "invalid_info: Workshop metadata is invalid"
    assert str(info) not in mod.error


def test_overly_deep_info_json_is_invalid_without_crashing_scan(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9009"])
    info = write_item(steam, "9009") / "Info.json"
    payload = "[" * 5000 + "0" + "]" * 5000
    assert len(payload.encode("utf-8")) < 1024 * 1024
    info.write_text(payload, encoding="utf-8")

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert mod.error == "invalid_info: Workshop metadata is invalid"


def test_manifest_with_more_than_4096_items_fails_closed(tmp_path):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, [str(index) for index in range(1, 4098)])

    assert SteamWorkshopService(steam).scan(force=True) == []


@pytest.mark.parametrize(
    "target",
    [
        "CON/file.pak",
        "mods/NUL.txt",
        "mods/file.pak:stream",
        "mods/folder. /file.pak",
        "mods/file.pak.",
        "mods//file.pak",
        "mods/./file.pak",
    ],
)
def test_install_target_rejects_windows_ambiguous_paths(tmp_path, target):
    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9004"])
    write_item(
        steam,
        "9004",
        valid_info(InstallRule=[{"Type": "Paks", "Target": target}]),
    )

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert mod.error == "invalid_info: Workshop metadata is invalid"


def test_info_descriptor_identity_change_fails_closed(tmp_path, monkeypatch):
    import os

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9008"])
    info = write_item(steam, "9008") / "Info.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(valid_info()), encoding="utf-8")
    real_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if Path(path) == info:
            return real_open(replacement, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced_open)

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert mod.error == "invalid_info: Workshop metadata is invalid"


def test_simulated_reparse_ancestor_is_rejected_without_symlink_permission(
    tmp_path, monkeypatch
):
    from backend import steam_workshop

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9005"])
    write_item(steam, "9005")
    unsafe = steam / "steamapps" / "workshop" / "content"
    original = steam_workshop._is_reparse
    monkeypatch.setattr(
        steam_workshop,
        "_is_reparse",
        lambda path: path == unsafe or original(path),
    )

    mod = SteamWorkshopService(steam).scan(force=True)[0]

    assert mod.valid is False
    assert mod.error == "unsafe_path: Workshop path is unsafe"


def test_simulated_reparse_acf_is_not_opened(tmp_path, monkeypatch):
    from backend import steam_workshop

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    manifest = write_acf(steam, ["9006"])
    write_item(steam, "9006")
    original = steam_workshop._is_reparse
    monkeypatch.setattr(
        steam_workshop,
        "_is_reparse",
        lambda path: path == manifest or original(path),
    )

    assert SteamWorkshopService(steam).scan(force=True) == []


def test_scan_never_opens_or_iterates_other_appids_or_common(tmp_path, monkeypatch):
    import os

    steam = tmp_path / "Steam"
    write_library_config(steam, [])
    write_acf(steam, ["9007"])
    write_item(steam, "9007")
    forbidden = [
        steam / "steamapps" / "common",
        steam / "steamapps" / "workshop" / "content" / "999999",
    ]
    for path in forbidden:
        path.mkdir(parents=True)

    real_open = os.open
    real_iterdir = Path.iterdir

    def guarded_open(path, *args, **kwargs):
        text = str(path).replace("\\", "/").casefold()
        assert "/common" not in text
        assert "/content/999999" not in text
        return real_open(path, *args, **kwargs)

    def guarded_iterdir(path):
        assert path not in forbidden
        return real_iterdir(path)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    assert [mod.workshop_id for mod in SteamWorkshopService(steam).scan(force=True)] == [
        "9007"
    ]


def workshop_service(
    tmp_path: Path,
    specs: dict[str, dict[str, object]],
    *,
    game_running=lambda: False,
) -> tuple[SteamWorkshopService, Path]:
    steam = tmp_path / "Steam"
    game = tmp_path / "Game"
    write_library_config(steam, [])
    write_acf(steam, list(specs))
    for workshop_id, overrides in specs.items():
        write_item(steam, workshop_id, valid_info(**overrides))
    settings = game / "Palworld" / "Mods" / "PalModSettings.ini"
    settings.parent.mkdir(parents=True)
    service = SteamWorkshopService(steam, game, game_running=game_running)
    service.scan(force=True)
    return service, settings


def active_values(settings: Path, encoding: str = "utf-8") -> list[str]:
    text = settings.read_bytes().decode(encoding)
    in_settings = False
    values = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_settings = stripped.casefold() == "[palmodsettings]"
        elif in_settings and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().casefold() == "activemodlist":
                values.append(value.strip())
    return values


@pytest.mark.parametrize(
    ("prefix", "codec", "newline", "decode"),
    [
        (b"\xef\xbb\xbf", "utf-8", "\r\n", "utf-8-sig"),
        (b"\xff\xfe", "utf-16-le", "\n", "utf-16"),
        (b"", "utf-8", "\n", "utf-8"),
    ],
)
def test_enable_preserves_encoding_newlines_comments_unknown_fields_and_order(
    tmp_path, prefix, codec, newline, decode
):
    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "Dependency", "Dependencies": []},
            "1002": {"PackageName": "Feature", "Dependencies": ["1001"]},
        },
    )
    text = newline.join(
        [
            "; note",
            "[PalModSettings]",
            "UnknownBefore=keep",
            "bGlobalEnableMod=False",
            "ActiveModList=Old",
            "; middle",
            "ActiveModList=Old",
            "UnknownAfter=stay",
            "[Other]",
            "Keep=值",
            "",
        ]
    )
    settings.write_bytes(prefix + text.encode(codec))

    result = service.set_enabled("1002", True)
    raw = settings.read_bytes()
    decoded = raw.decode(decode)

    assert raw.startswith(prefix)
    assert (newline.encode(codec)) in raw
    assert decoded.index("UnknownBefore=keep") < decoded.index("UnknownAfter=stay")
    assert "; note" in decoded and "; middle" in decoded and "[Other]" in decoded
    assert "Keep=值" in decoded
    assert "bGlobalEnableMod=True" in decoded
    assert active_values(settings, decode) == ["Old", "Dependency", "Feature"]
    assert result["enabled"] is True
    assert result["needs_restart"] is True


def test_enable_and_disable_are_deduplicated_idempotent_and_disable_only_target(tmp_path):
    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "Base", "Dependencies": []},
            "1002": {"PackageName": "Feature", "Dependencies": ["Base"]},
        },
    )
    settings.write_bytes(
        b"[PalModSettings]\r\nbGlobalEnableMod=True\r\n"
        b"ActiveModList=Base\r\nActiveModList=Base\r\n"
        b"ActiveModList=Feature\r\n"
    )

    first = service.set_enabled("1002", True)
    after_first = settings.read_bytes()
    second = service.set_enabled("1002", True)
    assert settings.read_bytes() == after_first
    assert first["enabled"] is second["enabled"] is True
    assert active_values(settings) == ["Base", "Feature"]

    result = service.set_enabled("1002", False)
    assert active_values(settings) == ["Base"]
    assert "bGlobalEnableMod=True" in settings.read_text(encoding="utf-8")
    after_disable = settings.read_bytes()
    assert service.set_enabled("1002", False)["enabled"] is False
    assert settings.read_bytes() == after_disable
    assert result["needs_restart"] is True


def test_enable_adds_transitive_dependencies_in_topological_order_by_name_or_id(tmp_path):
    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "Core", "Dependencies": []},
            "1002": {"PackageName": "Middle", "Dependencies": ["1001"]},
            "1003": {"PackageName": "Feature", "Dependencies": ["Middle", "1001"]},
        },
    )
    settings.write_text("[PalModSettings]\nbGlobalEnableMod=False\n", encoding="utf-8")

    service.set_enabled("1003", True)

    assert active_values(settings) == ["Core", "Middle", "Feature"]


@pytest.mark.parametrize(
    ("specs", "workshop_id", "reason"),
    [
        ({"1001": {"PackageName": "Feature", "Dependencies": ["Missing"]}}, "1001", "missing_dependencies"),
        (
            {
                "1001": {"PackageName": "First", "Dependencies": ["Second"]},
                "1002": {"PackageName": "Second", "Dependencies": ["First"]},
            },
            "1001",
            "dependency_cycle",
        ),
    ],
)
def test_enable_reports_structured_missing_dependency_or_cycle(
    tmp_path, specs, workshop_id, reason
):
    service, settings = workshop_service(tmp_path, specs)
    original = b"[PalModSettings]\nbGlobalEnableMod=False\n"
    settings.write_bytes(original)

    with pytest.raises(WorkshopDependencyError) as caught:
        service.set_enabled(workshop_id, True)

    assert caught.value.details["reason"] == reason
    assert settings.read_bytes() == original


def test_disable_requires_confirmation_for_enabled_transitive_dependents(tmp_path):
    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "Core", "Dependencies": []},
            "1002": {"PackageName": "Middle", "Dependencies": ["Core"]},
            "1003": {"PackageName": "Feature", "Dependencies": ["Middle"]},
        },
    )
    original = (
        b"[PalModSettings]\nbGlobalEnableMod=True\n"
        b"ActiveModList=Core\nActiveModList=Middle\nActiveModList=Feature\n"
    )
    settings.write_bytes(original)

    with pytest.raises(WorkshopDependencyError) as caught:
        service.set_enabled("1001", False)
    assert caught.value.details == {
        "reason": "enabled_dependents",
        "dependents": ["1002", "1003"],
    }
    assert settings.read_bytes() == original

    result = service.set_enabled("1001", False, confirm_dependents=True)
    assert active_values(settings) == ["Middle", "Feature"]
    assert result["enabled"] is False
    assert result["affected_dependents"] == ["1002", "1003"]


def test_disable_still_detects_target_when_dependent_has_another_missing_dependency(tmp_path):
    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "Core", "Dependencies": []},
            "1002": {
                "PackageName": "BrokenFeature",
                "Dependencies": ["Core", "Missing"],
            },
        },
    )
    settings.write_bytes(
        b"[PalModSettings]\nbGlobalEnableMod=True\n"
        b"ActiveModList=Core\nActiveModList=BrokenFeature\n"
    )

    with pytest.raises(WorkshopDependencyError) as caught:
        service.set_enabled("1001", False)

    assert caught.value.details["dependents"] == ["1002"]


def test_set_enabled_rejects_unscanned_unknown_invalid_and_non_numeric_records(tmp_path):
    steam = tmp_path / "Steam"
    game = tmp_path / "Game"
    service = SteamWorkshopService(steam, game)
    for value in ("../1001", "steam-workshop:1001", "1001"):
        with pytest.raises(ValueError):
            service.set_enabled(value, True)

    write_library_config(steam, [])
    write_acf(steam, ["1001", "1002"])
    write_item(steam, "1001")
    write_item(steam, "1002", valid_info(PackageName="../bad"))
    service.scan(force=True)
    with pytest.raises(ValueError):
        service.set_enabled("9999", True)
    with pytest.raises(ValueError):
        service.set_enabled("1002", True)


def test_game_running_rejects_change_without_touching_settings(tmp_path):
    service, settings = workshop_service(
        tmp_path,
        {"1001": {"PackageName": "Feature", "Dependencies": []}},
        game_running=lambda: True,
    )
    original = b"[PalModSettings]\nbGlobalEnableMod=False\n"
    settings.write_bytes(original)

    with pytest.raises(GameRunningError):
        service.set_enabled("1001", True)

    assert settings.read_bytes() == original


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_atomic_write_failure_preserves_original_and_removes_temp_files(
    tmp_path, monkeypatch, failure
):
    from backend import steam_workshop

    service, settings = workshop_service(
        tmp_path, {"1001": {"PackageName": "Feature", "Dependencies": []}}
    )
    original = b"[PalModSettings]\nbGlobalEnableMod=False\n"
    settings.write_bytes(original)
    if failure == "replace":
        monkeypatch.setattr(
            steam_workshop.os,
            "replace",
            lambda *_: (_ for _ in ()).throw(OSError("replace")),
        )
    else:
        real_write = steam_workshop.os.write
        monkeypatch.setattr(
            steam_workshop.os,
            "write",
            lambda fd, data: (
                (_ for _ in ()).throw(OSError("write"))
                if data
                else real_write(fd, data)
            ),
        )

    with pytest.raises(OSError):
        service.set_enabled("1001", True)

    assert settings.read_bytes() == original
    assert list(settings.parent.glob(".PalModSettings.ini.*.tmp")) == []


def test_set_enabled_uses_shared_game_lock_for_concurrent_services(tmp_path, monkeypatch):
    import threading
    import time
    from backend import steam_workshop

    service, settings = workshop_service(
        tmp_path,
        {
            "1001": {"PackageName": "First", "Dependencies": []},
            "1002": {"PackageName": "Second", "Dependencies": []},
        },
    )
    other = SteamWorkshopService(
        service._steam_roots[0], service.game_root, game_running=lambda: False
    )
    other.scan(force=True)
    settings.write_bytes(b"[PalModSettings]\nbGlobalEnableMod=False\n")
    real_replace = steam_workshop.os.replace
    entered = threading.Event()

    def slow_replace(source, destination):
        entered.set()
        time.sleep(0.05)
        real_replace(source, destination)

    monkeypatch.setattr(steam_workshop.os, "replace", slow_replace)
    first = threading.Thread(target=service.set_enabled, args=("1001", True))
    first.start()
    assert entered.wait(1)
    second = threading.Thread(target=other.set_enabled, args=("1002", True))
    second.start()
    first.join()
    second.join()

    assert active_values(settings) == ["First", "Second"]


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
    assert mod.error == "unsafe_path: Workshop path is unsafe"
