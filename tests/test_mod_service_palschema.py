import os
import zipfile
from pathlib import Path

import pytest

from backend.mod_service import ModService


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def _service(game: Path, tmp_path: Path) -> ModService:
    return ModService(game, tmp_path / "data", game_running=lambda: False)


def _palschema_layout(game: Path, nested: bool) -> tuple[Path, Path]:
    win64 = game / "Pal" / "Binaries" / "Win64"
    if nested:
        ue4ss = win64 / "ue4ss"
        (ue4ss / "UE4SS.dll").parent.mkdir(parents=True, exist_ok=True)
        (ue4ss / "UE4SS.dll").touch()
        ue4ss_mods = ue4ss / "Mods"
    else:
        (win64 / "UE4SS.dll").touch()
        ue4ss_mods = win64 / "Mods"
    framework = ue4ss_mods / "PalSchema"
    (framework / "dlls").mkdir(parents=True)
    (framework / "dlls" / "main.dll").write_bytes(b"framework")
    (framework / "enabled.txt").write_bytes(b"enabled")
    (ue4ss_mods / "mods.txt").write_text("PalSchema : 1\n", encoding="utf-8")
    content_root = framework / "mods"
    content_root.mkdir()
    return ue4ss_mods, content_root


@pytest.mark.parametrize("nested", [False, True], ids=["classic", "nested"])
def test_rescan_discovers_cpp_framework_and_each_palschema_content_pack(
    fake_game_root: Path, tmp_path: Path, nested: bool
) -> None:
    ue4ss_mods, content_root = _palschema_layout(fake_game_root, nested)
    first = content_root / "BetterOilPumps"
    second = content_root / "Hex Reworked Shop 3b"
    (first / "raw").mkdir(parents=True)
    (first / "raw" / "oil.json").write_bytes(b"{}")
    (second / "blueprints").mkdir(parents=True)
    (second / "blueprints" / "shop.json").write_bytes(b"{}")
    (second / "metadata.json").write_bytes(b'{"version":"3.b"}')

    found = _service(fake_game_root, tmp_path).rescan()
    by_name = {item["name"]: item for item in found}

    assert set(by_name) == {
        "PalSchema", "BetterOilPumps", "Hex Reworked Shop 3b"
    }
    assert by_name["PalSchema"]["kind"] == "ue4ss"
    assert by_name["BetterOilPumps"]["kind"] == "palschema"
    assert by_name["Hex Reworked Shop 3b"]["kind"] == "palschema"
    assert all(
        not relative.casefold().startswith("mods/")
        for relative in by_name["PalSchema"]["files"]
    )
    assert not (ue4ss_mods / "PalSchema" / "enabled.txt").exists()
    assert by_name["PalSchema"]["status"] == "enabled"


def test_rescan_archives_enabled_marker_without_direct_cross_volume_rename(
    fake_game_root: Path, tmp_path: Path, monkeypatch
) -> None:
    ue4ss_mods, _content_root = _palschema_layout(fake_game_root, True)
    marker = ue4ss_mods / "PalSchema" / "enabled.txt"
    original_replace = os.replace

    def reject_direct_marker_move(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == marker and "metadata" in destination_path.parts:
            raise OSError(17, "simulated cross-volume rename")
        return original_replace(source, destination)

    monkeypatch.setattr("backend.mod_service.os.replace", reject_direct_marker_move)

    service = _service(fake_game_root, tmp_path)
    found = service.rescan()
    framework = next(item for item in found if item["name"] == "PalSchema")

    assert framework["status"] == "enabled"
    assert not marker.exists()
    assert (
        tmp_path
        / "data"
        / "disabled"
        / framework["id"]
        / "metadata"
        / "enabled.txt"
    ).is_file()


def test_palschema_content_pack_can_toggle_unmanage_and_be_rediscovered(
    fake_game_root: Path, tmp_path: Path
) -> None:
    _ue4ss_mods, content_root = _palschema_layout(fake_game_root, True)
    payload = content_root / "ToggleMe" / "raw" / "change.json"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"original")
    service = _service(fake_game_root, tmp_path)
    found = next(item for item in service.rescan() if item["name"] == "ToggleMe")

    disabled = service.set_enabled(found["id"], False)
    disabled_payload = (
        tmp_path / "data" / "disabled" / found["id"] / "raw" / "change.json"
    )
    assert disabled["status"] == "disabled"
    assert not payload.exists()
    assert disabled_payload.read_bytes() == b"original"

    enabled = service.set_enabled(found["id"], True)
    assert enabled["status"] == "enabled"
    assert payload.read_bytes() == b"original"
    assert not disabled_payload.exists()

    service.unmanage(found["id"])
    assert payload.read_bytes() == b"original"
    assert all(item["name"] != "ToggleMe" for item in service.rescan())
    rediscovered = service.reset_ignored_and_rescan()
    assert any(item["name"] == "ToggleMe" for item in rediscovered)


def test_palschema_content_pack_delete_and_restore_is_scoped_to_its_folder(
    fake_game_root: Path, tmp_path: Path
) -> None:
    _ue4ss_mods, content_root = _palschema_layout(fake_game_root, True)
    target = content_root / "RecycleMe" / "raw" / "change.json"
    neighbor = content_root / "KeepMe" / "raw" / "neighbor.json"
    target.parent.mkdir(parents=True)
    neighbor.parent.mkdir(parents=True)
    target.write_bytes(b"recycle")
    neighbor.write_bytes(b"keep")
    service = _service(fake_game_root, tmp_path)
    found = next(item for item in service.rescan() if item["name"] == "RecycleMe")

    deleted = service.delete(found["id"])

    assert not target.exists()
    assert neighbor.read_bytes() == b"keep"
    restored = service.restore_trash(deleted["trash_id"])
    assert restored["kind"] == "palschema"
    assert target.read_bytes() == b"recycle"
    assert neighbor.read_bytes() == b"keep"


@pytest.mark.parametrize("nested", [False, True], ids=["classic", "nested"])
def test_install_palschema_archive_uses_framework_content_root(
    fake_game_root: Path, tmp_path: Path, nested: bool
) -> None:
    _ue4ss_mods, content_root = _palschema_layout(fake_game_root, nested)
    archive = _zip(
        tmp_path / "new-schema-mod.zip",
        {
            "New Schema Mod/metadata.json": b'{"name":"New Schema Mod"}',
            "New Schema Mod/raw/change.jsonc": b"{}",
        },
    )
    service = _service(fake_game_root, tmp_path)

    installed = service.install(archive)

    assert installed["kind"] == "palschema"
    assert installed["status"] == "enabled"
    assert (content_root / "New Schema Mod" / "raw" / "change.jsonc").read_bytes() == b"{}"


def test_install_palschema_archive_requires_framework(
    fake_game_root: Path, tmp_path: Path
) -> None:
    archive = _zip(tmp_path / "schema.zip", {"Schema/raw/change.json": b"{}"})

    with pytest.raises(ValueError, match="先安装并启用 PalSchema"):
        _service(fake_game_root, tmp_path).install(archive)


def test_install_ue4ss_cpp_mod_accepts_dll_entry_point(
    fake_game_root: Path, tmp_path: Path
) -> None:
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    (win64 / "ue4ss" / "UE4SS.dll").parent.mkdir(parents=True)
    (win64 / "ue4ss" / "UE4SS.dll").touch()
    archive = _zip(tmp_path / "cpp.zip", {"CppMod/dlls/main.dll": b"dll"})

    installed = _service(fake_game_root, tmp_path).install(archive)

    assert installed["kind"] == "ue4ss"
    assert (
        win64 / "ue4ss" / "Mods" / "CppMod" / "dlls" / "main.dll"
    ).read_bytes() == b"dll"
