import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from backend.domain import AuditStatus, ModKind
from backend.mod_service import GameRunningError, ModConflictError, ModService
from backend.process_utils import (
    is_directory_writable,
    is_palworld_running,
    restart_as_admin,
)


def test_process_detection_supports_safe_dependency_injection():
    assert is_palworld_running(lambda: ["other.exe", "PALWORLD.EXE"])
    assert is_palworld_running(lambda: ["Palworld-Win64-Shipping.exe"])
    assert not is_palworld_running(lambda: ["steam.exe"])


def test_public_directory_writable_probe_cleans_up(tmp_path):
    assert is_directory_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_restart_as_admin_uses_runas_and_raises_for_shell_error(monkeypatch):
    from backend import process_utils

    calls = []
    class Shell:
        def ShellExecuteW(self, *args):
            calls.append(args)
            return 33
    monkeypatch.setattr(process_utils.os, "name", "nt")
    monkeypatch.setattr(process_utils.ctypes, "windll", type("Windll", (), {"shell32": Shell()})(), raising=False)
    assert restart_as_admin(["manager.exe", "--path", "two words"]) is None
    assert calls[0][1:4] == ("runas", "manager.exe", '--path "two words"')

    monkeypatch.setattr(Shell, "ShellExecuteW", lambda self, *args: 5)
    with pytest.raises(OSError, match="5"):
        restart_as_admin(["manager.exe"])


def test_restart_as_admin_rejects_non_windows(monkeypatch):
    from backend import process_utils
    monkeypatch.setattr(process_utils.os, "name", "posix")
    with pytest.raises(RuntimeError, match="Windows"):
        restart_as_admin(["manager"])


def _service(game, tmp_path, *, running=False):
    return ModService(game, tmp_path / "data", game_running=lambda: running)


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def _live(game: Path, kind=ModKind.PAK) -> Path:
    name = "LogicMods" if kind is ModKind.LOGICPAK else "~mods"
    return game / "Pal" / "Content" / "Paks" / name


def test_zip_pak_group_install_disable_enable_delete(fake_game_root, tmp_path):
    source = _zip(tmp_path / "group.zip", {
        "Cool.pak": b"pak", "Cool.utoc": b"utoc", "Cool.ucas": b"ucas",
    })
    service = _service(fake_game_root, tmp_path)

    installed = service.install(source)
    manifest_id = installed["id"]
    assert {p.name for p in _live(fake_game_root).iterdir()} == {
        "Cool.pak", "Cool.utoc", "Cool.ucas"
    }
    assert installed["audit"]["status"] == "enabled"

    disabled = service.set_enabled(manifest_id, False)
    assert disabled["audit"]["status"] == "disabled"
    assert not any(_live(fake_game_root).iterdir())
    assert {p.name for p in (tmp_path / "data" / "disabled" / manifest_id).iterdir()} == {
        "Cool.pak", "Cool.utoc", "Cool.ucas"
    }

    enabled = service.set_enabled(manifest_id, True)
    assert enabled["audit"]["status"] == "enabled"
    assert not (tmp_path / "data" / "disabled" / manifest_id).exists()
    assert service.delete(manifest_id) == {"ok": True, "deleted": manifest_id}
    assert service.list_mods() == []
    assert not any(_live(fake_game_root).iterdir())


def test_identical_target_hash_is_deduplicated_across_sources(fake_game_root, tmp_path):
    first = tmp_path / "one" / "Duplicate.pak"
    second = tmp_path / "two" / "Duplicate.pak"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    service = _service(fake_game_root, tmp_path)

    installed = service.install(first)
    repeated = service.install(first)
    other_source = service.install(second)

    assert repeated["id"] == installed["id"]
    assert other_source["id"] == installed["id"]
    assert len(service.list_mods()) == 1


def test_logic_zip_installs_to_logicmods(fake_game_root, tmp_path):
    source = _zip(tmp_path / "logic.zip", {"LogicMods/Logic.pak": b"logic"})
    result = _service(fake_game_root, tmp_path).install(source)
    assert result["kind"] == "logicpak"
    assert (_live(fake_game_root, ModKind.LOGICPAK) / "Logic.pak").read_bytes() == b"logic"


def test_direct_pak_collects_same_stem_sidecars(fake_game_root, tmp_path):
    pak = tmp_path / "Direct.pak"
    pak.write_bytes(b"pak")
    pak.with_suffix(".utoc").write_bytes(b"utoc")
    pak.with_suffix(".ucas").write_bytes(b"ucas")
    result = _service(fake_game_root, tmp_path).install(pak, preferred_kind="logicpak")
    assert result["kind"] == "logicpak"
    assert [item["relative_path"] for item in result["files"]] == [
        "Direct.pak", "Direct.ucas", "Direct.utoc"
    ]


def test_conflict_cancel_replace_and_keep_both(fake_game_root, tmp_path):
    first = tmp_path / "first" / "Same.pak"
    first.parent.mkdir()
    first.write_bytes(b"old")
    service = _service(fake_game_root, tmp_path)
    old = service.install(first)
    second = tmp_path / "second" / "Same.pak"
    second.parent.mkdir()
    second.write_bytes(b"new")

    with pytest.raises(ModConflictError) as error:
        service.install(second)
    assert error.value.details["choices"] == ["replace", "keep_both", "cancel"]
    assert (_live(fake_game_root) / "Same.pak").read_bytes() == b"old"

    kept = service.install(second, display_name="Second Mod", decision="keep_both")
    assert (_live(fake_game_root) / "Same (Second Mod).pak").read_bytes() == b"new"
    service.delete(kept["id"])

    replaced = service.install(second, decision="replace")
    assert (_live(fake_game_root) / "Same.pak").read_bytes() == b"new"
    assert replaced["id"] != old["id"]
    assert {item["id"] for item in service.list_mods()} == {replaced["id"]}


def test_replace_refuses_unmanaged_conflict(fake_game_root, tmp_path):
    target = _live(fake_game_root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "Same.pak").write_bytes(b"unmanaged")
    source = tmp_path / "Same.pak"
    source.write_bytes(b"new")
    with pytest.raises(ModConflictError):
        _service(fake_game_root, tmp_path).install(source, decision="replace")
    assert (target / "Same.pak").read_bytes() == b"unmanaged"


@pytest.mark.parametrize("operation", ["install", "disable", "enable", "delete", "rescan"])
def test_running_game_rejects_all_mutations(fake_game_root, tmp_path, operation):
    source = tmp_path / "Run.pak"
    source.write_bytes(b"pak")
    stopped = _service(fake_game_root, tmp_path)
    manifest_id = stopped.install(source)["id"]
    if operation == "enable":
        stopped.set_enabled(manifest_id, False)
    running = _service(fake_game_root, tmp_path, running=True)
    with pytest.raises(GameRunningError, match="运行"):
        if operation == "install":
            running.install(source, display_name="Other", decision="keep_both")
        elif operation == "disable":
            running.set_enabled(manifest_id, False)
        elif operation == "enable":
            running.set_enabled(manifest_id, True)
        elif operation == "delete":
            running.delete(manifest_id)
        else:
            running.rescan()


def test_copy_failure_leaves_no_adjacent_temporary_files(fake_game_root, tmp_path, monkeypatch):
    source = tmp_path / "NoOrphan.pak"
    source.write_bytes(b"payload")
    def partial_copy(source_path, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("copy failed")
    monkeypatch.setattr("backend.mod_service.shutil.copy2", partial_copy)
    with pytest.raises(OSError, match="copy failed"):
        _service(fake_game_root, tmp_path).install(source)
    assert list(_live(fake_game_root).glob(".*.tmp")) == []


def test_install_checks_writability_and_space(fake_game_root, tmp_path, monkeypatch):
    source = tmp_path / "Check.pak"
    source.write_bytes(b"payload")
    service = _service(fake_game_root, tmp_path)
    monkeypatch.setattr("backend.mod_service.check_directory_writable", lambda path: False)
    with pytest.raises(PermissionError):
        service.install(source)
    monkeypatch.setattr("backend.mod_service.check_directory_writable", lambda path: True)
    monkeypatch.setattr("backend.mod_service.shutil.disk_usage", lambda path: (10, 10, 0))
    with pytest.raises(OSError, match="空间"):
        service.install(source)


def test_manifest_failure_rolls_back_files_and_replaced_manifest(fake_game_root, tmp_path, monkeypatch):
    service = _service(fake_game_root, tmp_path)
    old_source = tmp_path / "old" / "Roll.pak"
    old_source.parent.mkdir()
    old_source.write_bytes(b"old")
    old = service.install(old_source)
    new_source = tmp_path / "new" / "Roll.pak"
    new_source.parent.mkdir()
    new_source.write_bytes(b"new")
    monkeypatch.setattr(service.store, "create", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("manifest failed")))

    with pytest.raises(OSError, match="manifest failed"):
        service.install(new_source, decision="replace")
    assert (_live(fake_game_root) / "Roll.pak").read_bytes() == b"old"
    assert service.store.get(old["id"]).id == old["id"]
    assert not list((tmp_path / "data" / "staging").iterdir())


def test_modified_delete_requires_force_and_disabled_is_supported(fake_game_root, tmp_path):
    source = tmp_path / "Changed.pak"
    source.write_bytes(b"original")
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    (_live(fake_game_root) / "Changed.pak").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="修改"):
        service.delete(item["id"])
    service.set_enabled(item["id"], False)
    disabled = tmp_path / "data" / "disabled" / item["id"] / "Changed.pak"
    disabled.write_bytes(b"changed again")
    assert service.delete(item["id"], force_modified=True)["ok"] is True
    assert not disabled.exists()


@pytest.mark.parametrize("initial_enabled", [True, False], ids=["disable", "enable"])
@pytest.mark.parametrize("failure", ["save", "audit"])
def test_toggle_failure_restores_files_and_manifest(fake_game_root, tmp_path, monkeypatch, initial_enabled, failure):
    source = tmp_path / "Restore.pak"
    source.write_bytes(b"managed")
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    if not initial_enabled:
        service.set_enabled(item["id"], False)
    manifest = service.store.get(item["id"])
    original_save = service.store.save
    original_audit = service.store.audit

    if failure == "save":
        def fail_changed(value):
            if value.enabled != initial_enabled:
                raise OSError("save fault")
            return original_save(value)
        monkeypatch.setattr(service.store, "save", fail_changed)
    else:
        def fail_changed_audit(value):
            candidate = value if not isinstance(value, str) else service.store.get(value)
            if candidate.enabled != initial_enabled:
                raise OSError("audit fault")
            return original_audit(value)
        monkeypatch.setattr(service.store, "audit", fail_changed_audit)

    with pytest.raises(OSError, match=f"{failure} fault"):
        service.set_enabled(item["id"], not initial_enabled)

    restored = service.store.get(item["id"])
    assert restored.enabled is initial_enabled
    live = _live(fake_game_root) / "Restore.pak"
    disabled = tmp_path / "data" / "disabled" / item["id"] / "Restore.pak"
    assert live.is_file() is initial_enabled
    assert disabled.is_file() is (not initial_enabled)
    if initial_enabled:
        assert not disabled.parent.exists()
    assert original_audit(restored).status == (AuditStatus.ENABLED if initial_enabled else AuditStatus.DISABLED)


def test_enable_checks_conflict_before_restoring_group(fake_game_root, tmp_path):
    source = tmp_path / "Blocked.pak"
    source.write_bytes(b"managed")
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    service.set_enabled(item["id"], False)
    (_live(fake_game_root) / "Blocked.pak").write_bytes(b"foreign")
    with pytest.raises(ModConflictError):
        service.set_enabled(item["id"], True)
    assert (tmp_path / "data" / "disabled" / item["id"] / "Blocked.pak").is_file()


def test_rescan_groups_sidecars_is_idempotent_and_ignores_orphans(fake_game_root, tmp_path):
    live = _live(fake_game_root)
    live.mkdir(parents=True, exist_ok=True)
    (live / "Found.pak").write_bytes(b"pak")
    (live / "Found.utoc").write_bytes(b"utoc")
    (live / "Orphan.ucas").write_bytes(b"orphan")
    service = _service(fake_game_root, tmp_path)
    first = service.rescan()
    second = service.rescan()
    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert {f["relative_path"] for f in first[0]["files"]} == {"Found.pak", "Found.utoc"}


def test_list_mods_returns_real_audit(fake_game_root, tmp_path):
    source = tmp_path / "Audit.pak"
    source.write_bytes(b"original")
    service = _service(fake_game_root, tmp_path)
    item = service.install(source)
    (_live(fake_game_root) / "Audit.pak").write_bytes(b"modified")
    listed = service.list_mods()
    assert listed[0]["id"] == item["id"]
    assert listed[0]["audit"] == {"manifest_id": item["id"], "status": AuditStatus.MODIFIED.value}


def test_mod_manager_facade_delegates_lazily(fake_game_root, tmp_path, monkeypatch):
    from backend import mod_manager

    calls = []
    class FakeService:
        def install(self, *args, **kwargs): calls.append(("install", args, kwargs)); return {"ok": True}
        def set_enabled(self, *args, **kwargs): calls.append(("enabled", args, kwargs)); return {"ok": True}
        def delete(self, *args, **kwargs): calls.append(("delete", args, kwargs)); return {"ok": True}
        def rescan(self): calls.append(("rescan", (), {})); return []
        def list_mods(self): calls.append(("list", (), {})); return []
    monkeypatch.setattr(mod_manager, "_mod_service_instance", FakeService())

    assert mod_manager.import_mod_file("x.pak", preferred_type="pak") == {"ok": True}
    mod_manager.set_mod_enabled("id", False)
    mod_manager.delete_mod("id")
    mod_manager.resync_from_disk()
    mod_manager.list_mods()
    assert [call[0] for call in calls] == ["install", "enabled", "delete", "rescan", "list"]
