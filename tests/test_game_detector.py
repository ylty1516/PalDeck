from pathlib import Path

import pytest

from backend.game_detector import (
    ensure_mod_folders,
    find_palworld_installs,
    resolve_ue4ss_mods_dir,
    validate_game_path,
)


def make_shipping_game(root: Path) -> Path:
    (root / "Pal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").touch()
    (root / "Pal" / "Content" / "Paks").mkdir(parents=True)
    return root


def write_manifest(library: Path, installdir: str) -> None:
    steamapps = library / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / "appmanifest_1623730.acf").write_text(
        f'"AppState"\n{{\n    "appid" "1623730"\n    "installdir" "{installdir}"\n}}\n',
        encoding="utf-8",
    )


def test_find_install_uses_library_vdf_and_manifest_installdir(tmp_path):
    steam_root = tmp_path / "Steam"
    library = tmp_path / "Games" / "SteamLibrary"
    (steam_root / "steamapps").mkdir(parents=True)
    escaped_library = str(library).replace("\\", "\\\\")
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n "1" {{ "path" "{escaped_library}" }}\n}}',
        encoding="utf-8",
    )
    write_manifest(library, "Palworld Custom")
    game = make_shipping_game(library / "steamapps" / "common" / "Palworld Custom")

    installs = find_palworld_installs(steam_roots=[steam_root])

    assert [entry["path"] for entry in installs] == [str(game)]


def test_find_install_accepts_forward_slashes_in_library_vdf(tmp_path):
    steam_root = tmp_path / "Steam"
    library = tmp_path / "Secondary Library"
    (steam_root / "steamapps").mkdir(parents=True)
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "1" {{ "path" "{library.as_posix()}" }} }}',
        encoding="utf-8",
    )
    write_manifest(library, "Palworld")
    game = make_shipping_game(library / "steamapps" / "common" / "Palworld")

    assert find_palworld_installs(steam_roots=[steam_root])[0]["path"] == str(game)


def test_find_install_does_not_guess_without_manifest(tmp_path):
    steam_root = tmp_path / "Steam"
    guessed_game = make_shipping_game(steam_root / "steamapps" / "common" / "Palworld")

    assert guessed_game.is_dir()
    assert find_palworld_installs(steam_roots=[steam_root]) == []


def test_validate_without_create_has_no_directory_side_effects(tmp_path):
    game = make_shipping_game(tmp_path / "Palworld")
    expected = [
        game / "Pal" / "Content" / "Paks" / "~mods",
        game / "Pal" / "Content" / "Paks" / "LogicMods",
        game / "Pal" / "Binaries" / "Win64" / "Mods",
    ]

    result = validate_game_path(game)

    assert result["valid"] is True
    assert all(not path.exists() for path in expected)
    assert "ensure" not in result


def test_only_paks_is_not_a_valid_game_root(tmp_path):
    root = tmp_path / "Palworld"
    (root / "Pal" / "Content" / "Paks").mkdir(parents=True)

    assert validate_game_path(root)["valid"] is False
    with pytest.raises(ValueError, match="无效"):
        ensure_mod_folders(root)


def test_palworld_exe_alone_is_a_valid_game_root(tmp_path):
    root = tmp_path / "Palworld"
    root.mkdir()
    (root / "Palworld.exe").touch()

    assert validate_game_path(root)["valid"] is True


def test_shipping_exe_and_paks_are_a_valid_game_root(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")

    assert validate_game_path(root)["valid"] is True


def test_shipping_exe_without_paks_is_not_a_valid_game_root(tmp_path):
    root = tmp_path / "Palworld"
    (root / "Pal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").touch()

    assert validate_game_path(root)["valid"] is False


def test_resolve_ue4ss_mods_dir_prefers_nested_layout(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")
    nested = root / "Pal" / "Binaries" / "Win64" / "ue4ss" / "Mods"
    nested.mkdir(parents=True)
    classic = root / "Pal" / "Binaries" / "Win64" / "Mods"
    classic.mkdir()

    assert resolve_ue4ss_mods_dir(root) == nested


def test_resolve_ue4ss_mods_dir_uses_classic_layout_and_default(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")
    classic = root / "Pal" / "Binaries" / "Win64" / "Mods"

    assert resolve_ue4ss_mods_dir(root) == classic
    classic.mkdir()
    (classic / "mods.txt").touch()
    assert resolve_ue4ss_mods_dir(root) == classic


def test_validate_with_create_creates_only_client_mod_folders(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")

    result = validate_game_path(root, create=True)

    assert result["ensure"]["ok"] is True
    assert (root / "Pal" / "Content" / "Paks" / "~mods").is_dir()
    assert (root / "Pal" / "Content" / "Paks" / "LogicMods").is_dir()
    assert (root / "Pal" / "Binaries" / "Win64" / "Mods").is_dir()
    assert not (root / "Mods" / "Workshop").exists()
    assert not (root / "Mods" / "PalModSettings.ini").exists()
    assert not (root / "Mods").exists()
