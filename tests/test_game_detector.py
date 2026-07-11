from pathlib import Path

import pytest

from backend import game_detector
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


def test_manifest_rejects_wrong_appid(tmp_path):
    steam_root = tmp_path / "Steam"
    write_manifest(steam_root, "Palworld")
    manifest = steam_root / "steamapps" / "appmanifest_1623730.acf"
    manifest.write_text(
        '"AppState" { "appid" "999999" "installdir" "Palworld" }',
        encoding="utf-8",
    )
    make_shipping_game(steam_root / "steamapps" / "common" / "Palworld")

    assert find_palworld_installs(steam_roots=[steam_root]) == []


def test_libraryfolders_ignores_commented_path(tmp_path):
    steam_root = tmp_path / "Steam"
    commented_library = tmp_path / "CommentedLibrary"
    (steam_root / "steamapps").mkdir(parents=True)
    escaped = str(commented_library).replace("\\", "\\\\")
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ // "0" {{ "path" "{escaped}" }}\n }}',
        encoding="utf-8",
    )
    write_manifest(commented_library, "Palworld")
    make_shipping_game(commented_library / "steamapps" / "common" / "Palworld")

    assert find_palworld_installs(steam_roots=[steam_root]) == []


def test_manifest_ignores_commented_installdir(tmp_path):
    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_1623730.acf").write_text(
        '"AppState" { "appid" "1623730"\n'
        '// "installdir" "Commented"\n "installdir" "Real" }',
        encoding="utf-8",
    )
    make_shipping_game(steamapps / "common" / "Commented")
    real = make_shipping_game(steamapps / "common" / "Real")

    installs = find_palworld_installs(steam_roots=[steam_root])

    assert [item["path"] for item in installs] == [str(real)]


@pytest.mark.parametrize(
    "installdir",
    ["", ".", "..", "../Escaped", "..\\Escaped", "Pal/world", "Pal\\world"],
)
def test_manifest_rejects_non_single_relative_installdir(tmp_path, installdir):
    steam_root = tmp_path / "Steam"
    write_manifest(steam_root, installdir)
    candidate = steam_root / "steamapps" / "common" / installdir
    make_shipping_game(candidate)

    assert find_palworld_installs(steam_roots=[steam_root]) == []


def test_manifest_rejects_absolute_installdir(tmp_path):
    steam_root = tmp_path / "Steam"
    absolute_game = make_shipping_game(tmp_path / "AbsolutePalworld")
    write_manifest(steam_root, str(absolute_game).replace("\\", "\\\\"))

    assert find_palworld_installs(steam_roots=[steam_root]) == []


def test_default_fallback_is_not_scanned_when_manifest_finds_valid_install(
    tmp_path, monkeypatch
):
    steam_root = tmp_path / "Steam"
    write_manifest(steam_root, "Palworld")
    game = make_shipping_game(steam_root / "steamapps" / "common" / "Palworld")
    monkeypatch.setattr(game_detector, "_read_steam_path_from_registry", lambda: steam_root)

    def fail_if_called():
        raise AssertionError("fallback must not be scanned")

    monkeypatch.setattr(game_detector, "_common_steam_candidates", fail_if_called)

    assert find_palworld_installs()[0]["path"] == str(game)


def test_default_fallback_is_scanned_when_manifest_has_no_valid_install(
    tmp_path, monkeypatch
):
    steam_root = tmp_path / "Steam"
    write_manifest(steam_root, "Missing")
    fallback = make_shipping_game(tmp_path / "Fallback" / "Palworld")
    monkeypatch.setattr(game_detector, "_read_steam_path_from_registry", lambda: steam_root)
    monkeypatch.setattr(game_detector, "_common_steam_candidates", lambda: [fallback])

    assert [item["path"] for item in find_palworld_installs()] == [str(fallback)]


def test_common_fallback_candidates_are_steam_only():
    candidates = game_detector._common_steam_candidates()

    assert candidates
    assert all("xboxgames" not in str(path).casefold() for path in candidates)
    assert all("steamapps\\common" in str(path).casefold() for path in candidates)


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


@pytest.mark.parametrize(
    "marker",
    [
        Path("ue4ss/Mods"),
        Path("ue4ss/Mods/mods.txt"),
        Path("ue4ss/UE4SS-settings.ini"),
        Path("ue4ss/UE4SS.dll"),
        Path("ue4ss/dwmapi.dll"),
    ],
)
def test_resolve_ue4ss_mods_dir_prefers_explicit_nested_markers(tmp_path, marker):
    root = make_shipping_game(tmp_path / "Palworld")
    win64 = root / "Pal" / "Binaries" / "Win64"
    marked_path = win64 / marker
    if marker.name == "Mods":
        marked_path.mkdir(parents=True)
    else:
        marked_path.parent.mkdir(parents=True, exist_ok=True)
        marked_path.touch()
    (win64 / "Mods").mkdir(exist_ok=True)
    (win64 / "UE4SS.dll").touch()

    assert resolve_ue4ss_mods_dir(root) == win64 / "ue4ss" / "Mods"


@pytest.mark.parametrize(
    "marker",
    [
        Path("Mods"),
        Path("Mods/mods.txt"),
        Path("UE4SS-settings.ini"),
        Path("UE4SS.dll"),
        Path("dwmapi.dll"),
    ],
)
def test_resolve_ue4ss_mods_dir_recognizes_classic_markers(tmp_path, marker):
    root = make_shipping_game(tmp_path / "Palworld")
    win64 = root / "Pal" / "Binaries" / "Win64"
    marked_path = win64 / marker
    if marker.name == "Mods":
        marked_path.mkdir(parents=True)
    else:
        marked_path.parent.mkdir(parents=True, exist_ok=True)
        marked_path.touch()

    assert resolve_ue4ss_mods_dir(root) == win64 / "Mods"


def test_empty_nested_parent_does_not_override_classic_marker(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")
    win64 = root / "Pal" / "Binaries" / "Win64"
    (win64 / "ue4ss").mkdir()
    (win64 / "UE4SS-settings.ini").touch()

    assert resolve_ue4ss_mods_dir(root) == win64 / "Mods"


def test_resolve_ue4ss_mods_dir_defaults_to_classic(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")
    win64 = root / "Pal" / "Binaries" / "Win64"

    assert resolve_ue4ss_mods_dir(root) == win64 / "Mods"


def test_ensure_mod_folders_preflights_all_targets_before_creating(tmp_path):
    root = make_shipping_game(tmp_path / "Palworld")
    tilde_mods = root / "Pal" / "Content" / "Paks" / "~mods"
    logic_mods = root / "Pal" / "Content" / "Paks" / "LogicMods"
    logic_mods.touch()

    with pytest.raises(ValueError, match="不是文件夹"):
        ensure_mod_folders(root)

    assert not tilde_mods.exists()
    assert logic_mods.is_file()
    assert not (root / "Pal" / "Binaries" / "Win64" / "Mods").exists()


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
