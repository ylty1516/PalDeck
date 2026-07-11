import json
import zipfile
from pathlib import Path

import pytest

from backend.domain import ModKind
from backend.game_detector import resolve_ue4ss_mods_dir
from backend.mod_service import ModService
from backend import mod_manager, ue4ss_installer


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def _service(game: Path, tmp_path: Path) -> ModService:
    return ModService(game, tmp_path / "data", game_running=lambda: False)


def _layout(game: Path, nested: bool) -> Path:
    win64 = game / "Pal" / "Binaries" / "Win64"
    if nested:
        (win64 / "ue4ss" / "UE4SS.dll").parent.mkdir(parents=True, exist_ok=True)
        (win64 / "ue4ss" / "UE4SS.dll").touch()
    else:
        (win64 / "UE4SS.dll").touch()
    mods = resolve_ue4ss_mods_dir(game)
    mods.mkdir(parents=True, exist_ok=True)
    return mods


@pytest.mark.parametrize("nested", [False, True], ids=["classic", "nested"])
def test_install_and_toggle_ue4ss_uses_resolved_layout_and_preserves_mods_txt(
    fake_game_root, tmp_path, nested
):
    mods = _layout(fake_game_root, nested)
    mods_txt = mods / "mods.txt"
    mods_txt.write_text(
        "; header\n\nUnknown setting\notherMod : 1 ; keep\nCOOLMOD : 0 # old\ncoolmod : 0\n",
        encoding="utf-8",
    )
    source = _zip(tmp_path / "cool.zip", {
        "CoolMod/Scripts/main.lua": b"print('ok')",
        "CoolMod/config/default.ini": b"x=1",
        "CoolMod/enabled.txt": b"legacy marker",
    })
    service = _service(fake_game_root, tmp_path)

    installed = service.install(source)

    assert installed["kind"] == "ue4ss"
    assert (mods / "CoolMod" / "Scripts" / "main.lua").read_bytes() == b"print('ok')"
    assert not (mods / "CoolMod" / "enabled.txt").exists()
    metadata = tmp_path / "data" / "disabled" / installed["id"] / "metadata" / "enabled.txt"
    assert metadata.read_bytes() == b"legacy marker"
    assert installed["ue4ss_enabled_txt"]["relative_path"] == "metadata/enabled.txt"
    text = mods_txt.read_text(encoding="utf-8")
    assert "; header\n\nUnknown setting\notherMod : 1 ; keep\n" in text
    assert text.casefold().count("coolmod :") == 1
    assert "COOLMOD : 1 # old" in text

    disabled = service.set_enabled(installed["id"], False)
    assert disabled["status"] == "disabled"
    assert (mods / "CoolMod" / "Scripts" / "main.lua").is_file()
    assert "COOLMOD : 0 # old" in mods_txt.read_text(encoding="utf-8")

    enabled = service.set_enabled(installed["id"], True)
    assert enabled["status"] == "enabled"
    assert "COOLMOD : 1 # old" in mods_txt.read_text(encoding="utf-8")


def test_ue4ss_missing_mods_entry_is_appended_and_delete_removes_only_target(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    (mods / "mods.txt").write_text("Other : 1\n# tail\n", encoding="utf-8")
    source = _zip(tmp_path / "new.zip", {"NewMod/Scripts/main.lua": b"lua"})
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    assert (mods / "mods.txt").read_text(encoding="utf-8") == "Other : 1\n# tail\nNewMod : 1\n"

    service.delete(item["id"])

    assert (mods / "mods.txt").read_text(encoding="utf-8") == "Other : 1\n# tail\n"
    assert not (mods / "NewMod").exists()


@pytest.mark.parametrize("failure", ["config", "save"])
def test_ue4ss_toggle_is_transactional_on_config_or_manifest_failure(
    fake_game_root, tmp_path, monkeypatch, failure
):
    mods = _layout(fake_game_root, False)
    source = _zip(tmp_path / "roll.zip", {"RollMod/Scripts/main.lua": b"lua"})
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    before_config = (mods / "mods.txt").read_bytes()
    before_manifest = (service.store.manifests_dir / f"{item['id']}.json").read_bytes()
    if failure == "config":
        monkeypatch.setattr(service, "_write_mods_txt", lambda *args: (_ for _ in ()).throw(OSError("config failed")))
    else:
        monkeypatch.setattr(service.store, "save", lambda *args: (_ for _ in ()).throw(OSError("save failed")))

    with pytest.raises(OSError, match=failure):
        service.set_enabled(item["id"], False)

    assert (mods / "mods.txt").read_bytes() == before_config
    assert (service.store.manifests_dir / f"{item['id']}.json").read_bytes() == before_manifest
    assert service.store.get(item["id"]).enabled is True


def test_rescan_ue4ss_is_idempotent_and_skips_framework_builtins(fake_game_root, tmp_path):
    mods = _layout(fake_game_root, True)
    for name in ("FoundLua", "BPModLoaderMod"):
        script = mods / name / "Scripts" / "main.lua"
        script.parent.mkdir(parents=True)
        script.write_bytes(name.encode())
    (mods / "mods.txt").write_text("FoundLua : 0\nBPModLoaderMod : 1\n", encoding="utf-8")
    service = _service(fake_game_root, tmp_path)

    first = service.rescan()
    second = service.rescan()

    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["name"] == "FoundLua"
    assert first[0]["enabled"] is False
    assert first[0]["kind"] == "ue4ss"


def test_mod_manager_facade_accepts_ue4ss_preferred_type(monkeypatch):
    calls = []
    class FakeService:
        def install(self, *args, **kwargs):
            calls.append(kwargs)
            return {"kind": "ue4ss"}
    monkeypatch.setattr(mod_manager, "_mod_service_instance", FakeService())

    result = mod_manager.import_mod_file("lua.zip", preferred_type="ue4ss")

    assert calls == [{"preferred_kind": "ue4ss", "display_name": None, "nexus_id": None, "decision": "cancel"}]
    assert result["mod"]["kind"] == "ue4ss"


def test_installer_preflights_running_permissions_and_reparse_before_writing(
    fake_game_root, tmp_path, monkeypatch
):
    package = _zip(tmp_path / "UE4SS_v3.zip", {
        "UE4SS.dll": b"dll", "dwmapi.dll": b"proxy", "UE4SS-settings.ini": b"[General]\n",
    })
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: True)
    with pytest.raises(RuntimeError, match="运行"):
        ue4ss_installer.install_from_zip(fake_game_root, package)
    assert not (win64 / "UE4SS.dll").exists()

    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    monkeypatch.setattr(ue4ss_installer, "check_directory_writable", lambda path: False)
    with pytest.raises(PermissionError):
        ue4ss_installer.install_from_zip(fake_game_root, package)
    assert not (win64 / "UE4SS.dll").exists()

    monkeypatch.setattr(ue4ss_installer, "check_directory_writable", lambda path: True)
    monkeypatch.setattr(ue4ss_installer, "validate_no_reparse_ancestors", lambda path: (_ for _ in ()).throw(ValueError("reparse")))
    with pytest.raises(ValueError, match="reparse"):
        ue4ss_installer.install_from_zip(fake_game_root, package)
    assert not (win64 / "UE4SS.dll").exists()


def test_installer_rolls_back_partial_publish_and_does_not_fake_logicmods_enablement(
    fake_game_root, tmp_path, monkeypatch
):
    package = _zip(tmp_path / "UE4SS_v3.zip", {
        "UE4SS.dll": b"new", "dwmapi.dll": b"proxy", "UE4SS-settings.ini": b"[General]\n",
    })
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    (win64 / "UE4SS.dll").write_bytes(b"old")
    mods = win64 / "Mods"
    mods.mkdir()
    (mods / "mods.txt").write_text("BPModLoaderMod : 0\n", encoding="utf-8")
    original_replace = ue4ss_installer.os.replace
    count = 0
    def fail_replace(source, destination):
        nonlocal count
        if Path(destination).parent == win64:
            count += 1
            if count == 2:
                raise OSError("publish failed")
        return original_replace(source, destination)
    monkeypatch.setattr(ue4ss_installer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="publish failed"):
        ue4ss_installer.install_from_zip(fake_game_root, package)

    assert (win64 / "UE4SS.dll").read_bytes() == b"old"
    assert not (win64 / "dwmapi.dll").exists()
    assert (mods / "mods.txt").read_text(encoding="utf-8") == "BPModLoaderMod : 0\n"


def test_download_rejects_non_official_asset_url(tmp_path, monkeypatch):
    payload = json.dumps({"assets": [{
        "name": "UE4SS_v3.0.0.zip",
        "browser_download_url": "https://evil.example/UE4SS.zip",
    }]}).encode()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return payload
    monkeypatch.setattr(ue4ss_installer.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="官方"):
        ue4ss_installer.download_latest_zip(tmp_path)
