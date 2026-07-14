import io
import json
import zipfile
from pathlib import Path

import pytest

from backend.domain import ArchivePolicy, ModKind
from backend.game_detector import resolve_ue4ss_mods_dir
from backend.mod_service import ModifiedFilesError, ModService
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

    removed = service.delete(item["id"])

    assert removed["trash_id"]
    assert (mods / "mods.txt").read_text(encoding="utf-8") == "Other : 1\n# tail\n"
    assert not (mods / "NewMod").exists()

    restored = service.restore_trash(removed["trash_id"])
    assert restored["id"] == item["id"]
    assert restored["status"] == "enabled"
    assert (mods / "NewMod" / "Scripts" / "main.lua").read_bytes() == b"lua"
    assert (mods / "mods.txt").read_text(encoding="utf-8") == (
        "Other : 1\n# tail\nNewMod : 1\n"
    )


def test_ue4ss_recycle_record_failure_restores_files_config_and_manifest(
    fake_game_root, tmp_path, monkeypatch
):
    mods = _layout(fake_game_root, False)
    (mods / "mods.txt").write_text("Other : 1\n", encoding="utf-8")
    source = _zip(tmp_path / "trash-rollback.zip", {"TrashRollback/Scripts/main.lua": b"lua"})
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    before_config = (mods / "mods.txt").read_bytes()
    monkeypatch.setattr(
        service.trash_service.store,
        "save",
        lambda _record: (_ for _ in ()).throw(OSError("record failed")),
    )

    with pytest.raises(OSError, match="record failed"):
        service.delete(item["id"])

    assert (mods / "TrashRollback/Scripts/main.lua").read_bytes() == b"lua"
    assert (mods / "mods.txt").read_bytes() == before_config
    assert service.store.get(item["id"]).id == item["id"]
    assert service.list_trash()["items"] == []


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
    framework = (
        "BPML_GenericFunctions", "BPModLoaderMod", "CheatManagerEnablerMod",
        "ConsoleCommandsMod", "ConsoleEnablerMod", "Keybinds", "LineTraceMod",
        "shared", "SplitScreenMod",
    )
    for name in ("FoundLua", *framework):
        script = mods / name / "Scripts" / "main.lua"
        script.parent.mkdir(parents=True)
        script.write_bytes(name.encode())
    entries = "FoundLua : 0\n" + "".join(f"{name} : 1\n" for name in framework)
    (mods / "mods.txt").write_text(entries, encoding="utf-8")
    service = _service(fake_game_root, tmp_path)

    first = service.rescan()
    second = service.rescan()

    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["name"] == "FoundLua"
    assert first[0]["enabled"] is False
    assert first[0]["kind"] == "ue4ss"


def test_rescan_discovers_user_mods_from_classic_and_nested_layouts(fake_game_root, tmp_path):
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    classic = win64 / "Mods"
    nested = win64 / "ue4ss" / "Mods"
    for root, name in ((classic, "ClassicUserMod"), (nested, "NestedUserMod")):
        script = root / name / "Scripts" / "main.lua"
        script.parent.mkdir(parents=True)
        script.write_bytes(name.encode())
        (root / "mods.txt").write_text(f"{name} : 1\n", encoding="utf-8")
    (win64 / "ue4ss" / "UE4SS.dll").touch()
    service = _service(fake_game_root, tmp_path)

    found = service.rescan()

    assert {item["name"] for item in found} == {"ClassicUserMod", "NestedUserMod"}
    assert {Path(item["install_root"]).parent for item in found} == {classic, nested}


def test_rescan_preserves_existing_enabled_marker_as_enabled(fake_game_root, tmp_path):
    mods = _layout(fake_game_root, True)
    candidate = mods / "MarkerEnabled"
    script = candidate / "Scripts" / "main.lua"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"lua")
    (candidate / "enabled.txt").write_bytes(b"marker")
    service = _service(fake_game_root, tmp_path)

    [item] = service.rescan()

    assert item["enabled"] is True
    assert item["status"] == "enabled"
    assert "MarkerEnabled : 1" in (mods / "mods.txt").read_text(encoding="utf-8")


def test_external_ue4ss_unmanage_restores_enabled_marker_and_stays_ignored(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, True)
    candidate = mods / "ExternalLua"
    script = candidate / "Scripts/main.lua"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"lua")
    marker = candidate / "enabled.txt"
    marker.write_bytes(b"marker")
    service = _service(fake_game_root, tmp_path)
    [found] = service.rescan()
    assert found["externally_discovered"] is True
    assert not marker.exists()
    assert "ExternalLua : 1" in (mods / "mods.txt").read_text(encoding="utf-8")

    service.unmanage(found["id"])

    assert script.read_bytes() == b"lua"
    assert marker.read_bytes() == b"marker"
    assert "ExternalLua : 1" in (mods / "mods.txt").read_text(encoding="utf-8")
    assert service.list_mods() == []
    assert service.rescan() == []


def test_external_ue4ss_unmanage_failure_rolls_back_marker_ignore_and_manifest(
    fake_game_root, tmp_path, monkeypatch
):
    mods = _layout(fake_game_root, True)
    candidate = mods / "RollbackExternal"
    script = candidate / "Scripts/main.lua"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"lua")
    marker = candidate / "enabled.txt"
    marker.write_bytes(b"marker")
    service = _service(fake_game_root, tmp_path)
    [found] = service.rescan()
    metadata = (
        tmp_path / "data/disabled" / found["id"] / "metadata/enabled.txt"
    )
    assert metadata.read_bytes() == b"marker"
    monkeypatch.setattr(
        service.store,
        "delete",
        lambda _manifest_id: (_ for _ in ()).throw(OSError("manifest failed")),
    )

    with pytest.raises(OSError, match="manifest failed"):
        service.unmanage(found["id"])

    assert not marker.exists()
    assert metadata.read_bytes() == b"marker"
    assert service.store.get(found["id"]).id == found["id"]
    assert service.ignored_summary()["count"] == 0


def test_delete_ue4ss_removes_only_owned_files_and_preserves_user_additions(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    source = _zip(tmp_path / "owned.zip", {
        "Owned/Scripts/main.lua": b"lua",
        "Owned/config/managed.ini": b"managed",
        "Owned/enabled.txt": b"marker",
    })
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    user_file = mods / "Owned" / "config" / "user.ini"
    user_file.write_bytes(b"user")
    metadata_note = tmp_path / "data" / "disabled" / item["id"] / "metadata" / "user-note.txt"
    metadata_note.write_bytes(b"keep")

    service.delete(item["id"])

    assert user_file.read_bytes() == b"user"
    assert not (mods / "Owned" / "Scripts").exists()
    assert metadata_note.read_bytes() == b"keep"
    assert not (metadata_note.parent / "enabled.txt").exists()


def test_rescan_missing_mods_entry_adds_disabled_entry_then_can_enable(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    script = mods / "Discovered" / "Scripts" / "main.lua"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"lua")
    (mods / "mods.txt").write_text("Other : 1 ; keep\n", encoding="utf-8")
    service = _service(fake_game_root, tmp_path)

    [item] = service.rescan()

    assert item["enabled"] is False
    assert item["status"] == "disabled"
    assert (mods / "mods.txt").read_text(encoding="utf-8") == (
        "Other : 1 ; keep\nDiscovered : 0\n"
    )
    enabled = service.set_enabled(item["id"], True)
    assert enabled["status"] == "enabled"
    assert "Discovered : 1" in (mods / "mods.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_ue4ss_enabled_metadata_participates_in_audit(
    fake_game_root, tmp_path, damage
):
    mods = _layout(fake_game_root, False)
    source = _zip(tmp_path / "metadata.zip", {
        "Metadata/Scripts/main.lua": b"lua",
        "Metadata/enabled.txt": b"marker",
    })
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    metadata = tmp_path / "data" / "disabled" / item["id"] / "metadata" / "enabled.txt"
    if damage == "missing":
        metadata.unlink()
    else:
        metadata.write_bytes(b"changed")

    [listed] = service.list_mods()

    assert listed["status"] == damage


def test_payload_modified_outranks_missing_ue4ss_metadata_and_blocks_delete(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    source = _zip(tmp_path / "priority.zip", {
        "Priority/Scripts/main.lua": b"original",
        "Priority/enabled.txt": b"marker",
    })
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    (mods / "Priority" / "Scripts" / "main.lua").write_bytes(b"changed")
    metadata = tmp_path / "data" / "disabled" / item["id"] / "metadata" / "enabled.txt"
    metadata.unlink()

    [listed] = service.list_mods()

    assert listed["status"] == "modified"
    with pytest.raises(ModifiedFilesError) as error:
        service.delete(item["id"])
    assert error.value.details["files"] == [str(mods / "Priority" / "Scripts" / "main.lua")]
    assert (mods / "Priority" / "Scripts" / "main.lua").read_bytes() == b"changed"


def test_ue4ss_enabled_metadata_record_is_strictly_deserialized(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    source = _zip(tmp_path / "strict.zip", {
        "Strict/Scripts/main.lua": b"lua",
        "Strict/enabled.txt": b"marker",
    })
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    manifest_path = service.store.manifests_dir / f"{item['id']}.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["ue4ss_enabled_txt"]["unexpected"] = True
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid manifest"):
        service.store.get(item["id"])


def test_mods_txt_bom_is_preserved_and_target_is_not_duplicated(
    fake_game_root, tmp_path
):
    mods = _layout(fake_game_root, False)
    mods_txt = mods / "mods.txt"
    mods_txt.write_bytes(b"\xef\xbb\xbfBomMod : 0\r\nOther : 1\r\n")
    source = _zip(tmp_path / "bom.zip", {"BomMod/Scripts/main.lua": b"lua"})

    _service(fake_game_root, tmp_path).install(source)

    result = mods_txt.read_bytes()
    assert result == b"\xef\xbb\xbfBomMod : 1\r\nOther : 1\r\n"
    assert result.lower().count(b"bommod :") == 1


def test_mods_txt_append_preserves_cr_line_endings(fake_game_root, tmp_path):
    mods = _layout(fake_game_root, False)
    mods_txt = mods / "mods.txt"
    mods_txt.write_bytes(b"Other : 1\r")
    source = _zip(tmp_path / "cr.zip", {"CrMod/Scripts/main.lua": b"lua"})

    _service(fake_game_root, tmp_path).install(source)

    assert mods_txt.read_bytes() == b"Other : 1\rCrMod : 1\r"


def test_invalid_utf8_mods_txt_is_rejected_without_changes(fake_game_root, tmp_path):
    mods = _layout(fake_game_root, False)
    mods_txt = mods / "mods.txt"
    original = b"Other : 1\n\xffinvalid\n"
    mods_txt.write_bytes(original)
    source = _zip(tmp_path / "invalid.zip", {"Invalid/Scripts/main.lua": b"lua"})
    service = _service(fake_game_root, tmp_path)

    with pytest.raises(UnicodeDecodeError):
        service.install(source)

    assert mods_txt.read_bytes() == original
    assert not (mods / "Invalid").exists()
    assert service.list_mods() == []


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
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
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
        ue4ss_installer.install_from_zip(fake_game_root, package, confirm_replace=True)

    assert (win64 / "UE4SS.dll").read_bytes() == b"old"
    assert not (win64 / "dwmapi.dll").exists()
    assert (mods / "mods.txt").read_text(encoding="utf-8") == "BPModLoaderMod : 0\n"


def test_installer_uses_archive_policy_and_rejects_zip_bomb_before_publish(
    fake_game_root, tmp_path, monkeypatch
):
    package = tmp_path / "UE4SS_bomb.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("UE4SS.dll", b"dll")
        archive.writestr("dwmapi.dll", b"proxy")
        archive.writestr("UE4SS-settings.ini", b"x" * 128)
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)

    with pytest.raises(ValueError, match="总大小"):
        ue4ss_installer.install_from_zip(
            fake_game_root,
            package,
            policy=ArchivePolicy(max_files=10, max_single_bytes=256, max_total_bytes=32),
        )

    assert not (win64 / "UE4SS.dll").exists()
    assert not (win64 / "dwmapi.dll").exists()


def test_installer_rejects_traversal_in_otherwise_valid_package(
    fake_game_root, tmp_path, monkeypatch
):
    package = _zip(tmp_path / "UE4SS_traversal.zip", {
        "UE4SS.dll": b"dll",
        "dwmapi.dll": b"proxy",
        "UE4SS-settings.ini": b"settings",
        "../escape.txt": b"escape",
    })
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)

    with pytest.raises(ValueError, match="路径穿越"):
        ue4ss_installer.install_from_zip(fake_game_root, package)

    assert not (tmp_path / "escape.txt").exists()
    assert not (fake_game_root / "Pal" / "Binaries" / "Win64" / "UE4SS.dll").exists()


def _palworld_archive_bytes(*, missing: str | None = None) -> bytes:
    payload = io.BytesIO()
    files = {
        "dwmapi.dll": b"proxy",
        "ue4ss/UE4SS.dll": b"dll",
        "ue4ss/UE4SS-settings.ini": b"[General]\n",
        "ue4ss/MemberVariableLayout.ini": b"layout",
    }
    with zipfile.ZipFile(payload, "w") as archive:
        for name, content in files.items():
            if name != missing:
                archive.writestr(name, content)
    return payload.getvalue()


@pytest.mark.parametrize("missing", [
    "dwmapi.dll", "ue4ss/UE4SS.dll", "ue4ss/UE4SS-settings.ini",
    "ue4ss/MemberVariableLayout.ini",
])
def test_bundled_bytes_require_complete_palworld_layout(fake_game_root, monkeypatch, missing):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)

    with pytest.raises(ValueError, match="Palworld"):
        ue4ss_installer.install_from_bytes(
            fake_game_root, _palworld_archive_bytes(missing=missing),
            require_palworld_layout=True,
        )


def test_installer_stages_transaction_on_game_volume_before_atomic_publish(
    fake_game_root, monkeypatch
):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    observed = []
    original_mkdtemp = ue4ss_installer.tempfile.mkdtemp

    def same_volume_mkdtemp(*args, **kwargs):
        prefix = kwargs.get("prefix", "")
        directory = kwargs.get("dir")
        if prefix == ".paldeck-ue4ss-":
            observed.append((prefix, Path(directory) if directory is not None else None))
            assert Path(directory) == win64
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(ue4ss_installer.tempfile, "mkdtemp", same_volume_mkdtemp)
    result = ue4ss_installer.install_from_bytes(
        fake_game_root, _palworld_archive_bytes(), require_palworld_layout=True
    )

    assert result["ok"] is True
    assert observed == [(".paldeck-ue4ss-", win64)]
    assert not any(path.name.startswith(".paldeck-ue4ss-") for path in win64.iterdir())


def test_bundled_bytes_use_same_in_memory_snapshot_and_strict_policy(
    fake_game_root, monkeypatch
):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    observed = []
    original_extract = ue4ss_installer.extract_archive_safely

    def observing_extract(source, *args, **kwargs):
        observed.append(source)
        assert isinstance(source, io.BytesIO)
        assert source.getvalue() == _palworld_archive_bytes()
        policy = kwargs["policy"]
        assert policy.max_files == 256
        assert policy.max_single_bytes == 32 * 1024**2
        assert policy.max_total_bytes == 128 * 1024**2
        assert policy.max_compression_ratio == 200
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(ue4ss_installer, "extract_archive_safely", observing_extract)
    result = ue4ss_installer.install_from_bytes(
        fake_game_root, _palworld_archive_bytes(), require_palworld_layout=True
    )

    assert result["ok"] is True
    assert len(observed) == 1


def test_ue4ss_installer_has_no_independent_write_lock():
    assert not hasattr(ue4ss_installer, "install_lock")
    assert not hasattr(ue4ss_installer, "_INSTALL_LOCKS")


def test_xinput_marker_requires_confirmation_before_legacy_removal(fake_game_root, monkeypatch):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    legacy = fake_game_root / "Pal" / "Binaries" / "Win64" / "xinput1_3.dll"
    legacy.write_bytes(b"manual")

    with pytest.raises(ue4ss_installer.Ue4ssConflictError) as conflict:
        ue4ss_installer.install_from_bytes(fake_game_root, _palworld_archive_bytes())

    assert conflict.value.details["markers"]["xinput1_3"] is True
    assert legacy.read_bytes() == b"manual"


def test_existing_ue4ss_requires_confirmation_and_reports_markers(fake_game_root, monkeypatch):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    (win64 / "dwmapi.dll").write_bytes(b"manual")
    (win64 / "UE4SS.dll").write_bytes(b"manual-dll")

    with pytest.raises(ue4ss_installer.Ue4ssConflictError) as conflict:
        ue4ss_installer.install_from_bytes(fake_game_root, _palworld_archive_bytes())

    assert conflict.value.details["markers"]["dwmapi"] is True
    assert (win64 / "dwmapi.dll").read_bytes() == b"manual"
    result = ue4ss_installer.install_from_bytes(
        fake_game_root, _palworld_archive_bytes(), confirm_replace=True
    )
    assert result["ok"] is True
    assert (win64 / "dwmapi.dll").read_bytes() == b"proxy"


def test_confirmed_existing_install_rolls_back_on_publish_failure(fake_game_root, monkeypatch):
    monkeypatch.setattr(ue4ss_installer, "is_palworld_running", lambda: False)
    win64 = fake_game_root / "Pal" / "Binaries" / "Win64"
    (win64 / "dwmapi.dll").write_bytes(b"old-proxy")
    original_replace = ue4ss_installer.os.replace

    def fail_new_dll(source, destination):
        if Path(destination) == win64 / "ue4ss" / "UE4SS.dll":
            raise OSError("publish failed")
        return original_replace(source, destination)

    monkeypatch.setattr(ue4ss_installer.os, "replace", fail_new_dll)
    with pytest.raises(OSError, match="publish failed"):
        ue4ss_installer.install_from_bytes(
            fake_game_root, _palworld_archive_bytes(), confirm_replace=True
        )
    assert (win64 / "dwmapi.dll").read_bytes() == b"old-proxy"
    assert not (win64 / "ue4ss" / "UE4SS.dll").exists()


def test_installer_exposes_no_generic_online_release_path():
    for name in ("download_latest_zip", "install_latest", "GITHUB_API", "USER_AGENT"):
        assert not hasattr(ue4ss_installer, name)
    source = Path(ue4ss_installer.__file__).read_text(encoding="utf-8")
    assert "UE4SS-RE/RE-UE4SS/releases/latest" not in source
    assert "urllib" not in source
