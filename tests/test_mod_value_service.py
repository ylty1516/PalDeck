from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.domain import ModKind
from backend.manifest_store import ManifestStore
from backend.mod_service import GameRunningError, ModService
from backend.mod_value_service import (
    ModValueConflict,
    ModValueInvalid,
    ModValueNotSupported,
    ModValueService,
    ModValueStale,
)


def make_manifest(tmp_path: Path, config: object, schema: object | None = None, *, kind=ModKind.UE4SS):
    root = tmp_path / "ConfigurableChestSlots"
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "main.lua").write_text("return true\n", encoding="utf-8")
    (root / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    files = [scripts / "main.lua", root / "config.json"]
    if schema is not None:
        (root / "palmod_config.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        files.append(root / "palmod_config.json")
    store = ManifestStore(tmp_path / "manifests", known_roots=(root.parent,))
    return store.create("ConfigurableChestSlots", kind, root, files), store


def explicit_schema():
    return {
        "schema_version": 1,
        "mod_id": "ConfigurableChestSlots",
        "display_name": "普通箱子格数调整",
        "description": "调整箱子容量。",
        "version": "1.1.0",
        "fields": [
            {
                "key": "chest_slots",
                "label": "箱子目标格数",
                "type": "int",
                "min": 10,
                "max": 500,
                "default": 50,
                "step": 1,
                "description": "普通箱子槽位数。",
            },
            {
                "key": "scale",
                "label": "缩放倍率",
                "type": "float",
                "min": 0.1,
                "max": 5.0,
                "default": 1.0,
                "step": 0.1,
                "description": "显示倍率。",
            },
            {
                "key": "also_foodbox",
                "label": "同时修改饲料箱",
                "type": "bool",
                "default": True,
                "description": "布尔字段暂不公开。",
            },
        ],
    }


def test_explicit_schema_exposes_only_valid_numeric_fields(tmp_path):
    manifest, _store = make_manifest(
        tmp_path,
        {"chest_slots": 50, "scale": 1.5, "also_foodbox": True, "internal": "keep"},
        explicit_schema(),
    )
    service = ModValueService()

    result = service.read_values(manifest)

    assert result["display_name"] == "普通箱子格数调整"
    assert result["description"] == "调整箱子容量。"
    assert result["revision"].startswith("sha256:")
    assert [field["key"] for field in result["fields"]] == ["chest_slots", "scale"]
    assert result["fields"][0] == {
        "key": "chest_slots",
        "label": "箱子目标格数",
        "type": "int",
        "value": 50,
        "min": 10,
        "max": 500,
        "step": 1,
        "description": "普通箱子槽位数。",
    }
    assert result["fields"][1]["value"] == 1.5


def test_generic_config_exposes_only_top_level_finite_numbers(tmp_path):
    manifest, _store = make_manifest(
        tmp_path,
        {
            "count": 12,
            "ratio": 0.75,
            "enabled": True,
            "name": "demo",
            "nested": {"value": 9},
            "items": [1, 2],
        },
    )

    result = ModValueService().read_values(manifest)

    assert [field["key"] for field in result["fields"]] == ["count", "ratio"]
    assert result["fields"][0]["type"] == "int"
    assert result["fields"][0]["min"] == -1_000_000_000
    assert result["fields"][1]["type"] == "float"
    assert result["display_name"] == manifest.name


def test_invalid_schema_fails_closed_without_generic_fallback(tmp_path):
    schema = explicit_schema()
    schema["unknown"] = "not allowed"
    manifest, _store = make_manifest(tmp_path, {"chest_slots": 50}, schema)
    service = ModValueService()

    assert service.inspect_manifest(manifest) is None
    with pytest.raises(ModValueInvalid):
        service.read_values(manifest)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_config_values_are_rejected(tmp_path, bad):
    manifest, _store = make_manifest(tmp_path, {"value": 1})
    (manifest.install_root / "config.json").write_text(
        '{"value": ' + ("NaN" if bad != bad else ("Infinity" if bad > 0 else "-Infinity")) + "}\n",
        encoding="utf-8",
    )
    with pytest.raises(ModValueInvalid):
        ModValueService().read_values(manifest)


def test_pak_manifest_and_config_without_numeric_fields_are_not_supported(tmp_path):
    pak_manifest, _store = make_manifest(tmp_path / "pak", {"value": 1}, kind=ModKind.PAK)
    text_manifest, _store = make_manifest(tmp_path / "text", {"enabled": True, "name": "x"})

    for manifest in (pak_manifest, text_manifest):
        assert ModValueService().inspect_manifest(manifest) is None
        with pytest.raises(ModValueNotSupported):
            ModValueService().read_values(manifest)


def test_schema_rejects_wrong_mod_id_duplicate_keys_bad_ranges_and_bool_constraints(tmp_path):
    mutations = []
    wrong_id = explicit_schema(); wrong_id["mod_id"] = "Other"; mutations.append(wrong_id)
    duplicate = explicit_schema(); duplicate["fields"][1]["key"] = "chest_slots"; mutations.append(duplicate)
    bad_range = explicit_schema(); bad_range["fields"][0]["min"] = 600; mutations.append(bad_range)
    bad_step = explicit_schema(); bad_step["fields"][0]["step"] = 0; mutations.append(bad_step)
    bad_bool = explicit_schema(); bad_bool["fields"][2]["min"] = 0; mutations.append(bad_bool)

    for index, schema in enumerate(mutations):
        manifest, _store = make_manifest(
            tmp_path / str(index),
            {"chest_slots": 50, "scale": 1.5, "also_foodbox": True},
            schema,
        )
        with pytest.raises(ModValueInvalid):
            ModValueService().read_values(manifest)


def install_value_mod(fake_game_root: Path, tmp_path: Path):
    import zipfile

    win64 = fake_game_root / "Pal/Binaries/Win64"
    (win64 / "UE4SS.dll").touch()
    mods = win64 / "Mods"
    mods.mkdir(exist_ok=True)
    archive = tmp_path / "value-mod.zip"
    config = {"chest_slots": 50, "scale": 1.5, "also_foodbox": True, "keep": "yes"}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("ConfigurableChestSlots/Scripts/main.lua", "return true\n")
        bundle.writestr("ConfigurableChestSlots/config.json", json.dumps(config))
        bundle.writestr("ConfigurableChestSlots/palmod_config.json", json.dumps(explicit_schema()))
    service = ModService(fake_game_root, tmp_path / "data", game_running=lambda: False)
    installed = service.install(archive)
    return service, installed, mods / "ConfigurableChestSlots" / "config.json"


def test_mod_service_lists_reads_and_updates_values_transactionally(fake_game_root, tmp_path):
    service, installed, config = install_value_mod(fake_game_root, tmp_path)
    listed = next(item for item in service.list_mods() if item["id"] == installed["id"])
    assert listed["adjustable_values"] is True
    assert listed["adjustable_value_count"] == 2
    loaded = service.get_mod_values(installed["id"])

    saved = service.update_mod_values(
        installed["id"],
        {"chest_slots": 60, "scale": 2.0},
        loaded["revision"],
    )

    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload == {
        "chest_slots": 60,
        "scale": 2.0,
        "also_foodbox": True,
        "keep": "yes",
    }
    assert {field["key"]: field["value"] for field in saved["fields"]} == {
        "chest_slots": 60,
        "scale": 2.0,
    }
    assert saved["revision"] != loaded["revision"]
    assert service.list_mods()[0]["status"] == "enabled"


def test_value_update_works_while_ue4ss_mod_is_disabled(fake_game_root, tmp_path):
    service, installed, config = install_value_mod(fake_game_root, tmp_path)
    service.set_enabled(installed["id"], False)
    loaded = service.get_mod_values(installed["id"])

    service.update_mod_values(installed["id"], {"chest_slots": 70}, loaded["revision"])

    assert json.loads(config.read_text(encoding="utf-8"))["chest_slots"] == 70
    assert service.list_mods()[0]["status"] == "disabled"


def test_value_update_rejects_stale_revision_invalid_values_and_running_game(
    fake_game_root, tmp_path
):
    service, installed, config = install_value_mod(fake_game_root, tmp_path)
    loaded = service.get_mod_values(installed["id"])
    config.write_text(config.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ModValueStale):
        service.update_mod_values(installed["id"], {"chest_slots": 60}, loaded["revision"])

    fresh = service.get_mod_values(installed["id"])
    for values in (
        {"unknown": 1},
        {"chest_slots": 9},
        {"chest_slots": 10.5},
        {"scale": float("inf")},
        {},
    ):
        with pytest.raises(ModValueInvalid):
            service.update_mod_values(installed["id"], values, fresh["revision"])

    service.game_running = lambda: True
    with pytest.raises(GameRunningError):
        service.update_mod_values(installed["id"], {"chest_slots": 60}, fresh["revision"])


def test_manifest_save_failure_restores_config_and_manifest(
    fake_game_root, tmp_path, monkeypatch
):
    service, installed, config = install_value_mod(fake_game_root, tmp_path)
    loaded = service.get_mod_values(installed["id"])
    config_before = config.read_bytes()
    manifest_path = service.store.manifests_dir / f"{installed['id']}.json"
    manifest_before = manifest_path.read_bytes()
    original_save = service.store.save

    def save_then_fail(manifest):
        original_save(manifest)
        raise OSError("manifest failed")

    monkeypatch.setattr(service.store, "save", save_then_fail)
    with pytest.raises(OSError, match="manifest failed"):
        service.update_mod_values(installed["id"], {"chest_slots": 80}, loaded["revision"])

    assert config.read_bytes() == config_before
    assert manifest_path.read_bytes() == manifest_before


def test_non_config_file_conflict_blocks_value_update(fake_game_root, tmp_path):
    service, installed, _config = install_value_mod(fake_game_root, tmp_path)
    loaded = service.get_mod_values(installed["id"])
    script = service.store.get(installed["id"]).install_root / "Scripts/main.lua"
    script.write_text("modified\n", encoding="utf-8")

    with pytest.raises(ModValueConflict):
        service.update_mod_values(installed["id"], {"chest_slots": 60}, loaded["revision"])


def test_duplicate_json_keys_oversized_config_and_reparse_are_rejected(tmp_path):
    duplicate, _store = make_manifest(tmp_path / "duplicate", {"value": 1})
    (duplicate.install_root / "config.json").write_text(
        '{"value": 1, "value": 2}\n', encoding="utf-8"
    )
    with pytest.raises(ModValueInvalid):
        ModValueService().read_values(duplicate)

    oversized, _store = make_manifest(tmp_path / "oversized", {"value": 1})
    (oversized.install_root / "config.json").write_bytes(
        b'{"value":1,"padding":"' + b"x" * (256 * 1024) + b'"}'
    )
    with pytest.raises(ModValueInvalid):
        ModValueService().read_values(oversized)

    target, _store = make_manifest(tmp_path / "link", {"value": 1})
    config = target.install_root / "config.json"
    original = target.install_root / "real.json"
    config.replace(original)
    try:
        config.symlink_to(original)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ModValueInvalid):
        ModValueService().read_values(target)
