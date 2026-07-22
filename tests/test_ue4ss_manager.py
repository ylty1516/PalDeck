import hashlib
import io
import zipfile

import pytest

from backend.ue4ss_manager import (
    Ue4ssFrameworkManager,
    Ue4ssLifecycleError,
    Ue4ssModifiedFiles,
    Ue4ssRepairConflict,
)
from backend.ue4ss_provider import Ue4ssAsset


def archive_bytes(core: bytes = b"dll") -> bytes:
    payload = io.BytesIO()
    files = {
        "dwmapi.dll": b"proxy",
        "ue4ss/UE4SS.dll": core,
        "ue4ss/UE4SS-settings.ini": b"[General]\nbUseUObjectArrayCache = true\n",
        "ue4ss/MemberVariableLayout.ini": b"layout",
        "ue4ss/Mods/mods.txt": b"BPModLoaderMod : 1\n",
        "ue4ss/Mods/BPModLoaderMod/Scripts/main.lua": b"framework",
    }
    with zipfile.ZipFile(payload, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return payload.getvalue()


class FakeProvider:
    def __init__(self, payload: bytes):
        self.payload = payload

    def bundled_archive(self):
        return self.payload

    def bundled_status(self):
        return {
            "available": True,
            "asset": Ue4ssAsset(
                "UE4SS-Palworld.zip",
                len(self.payload),
                hashlib.sha256(self.payload).hexdigest(),
                "2026-07-14T00:00:00Z",
                "https://example.invalid/archive.zip",
            ),
        }


def manager(fake_game_root, tmp_path, payload=None):
    return Ue4ssFrameworkManager(
        fake_game_root,
        tmp_path / "data",
        provider=FakeProvider(payload or archive_bytes()),
        game_running=lambda: False,
    )


def test_external_install_is_unmanaged_and_can_be_explicitly_repaired(
    fake_game_root, tmp_path
):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    (win64 / "dwmapi.dll").write_bytes(b"external")
    service = manager(fake_game_root, tmp_path)

    state = service.state()
    assert state["ownership"] == "external"
    assert state["managed"] is False
    assert state["integrity"] == "unmanaged"
    assert state["repair_available"] is True
    assert state["uninstall_available"] is True
    with pytest.raises(Ue4ssRepairConflict):
        service.repair()

    service.repair(confirm_replace=True)

    assert service.state()["managed"] is True
    assert service.state()["integrity"] == "healthy"


def test_unmanaged_uninstall_uses_only_bundled_known_paths_and_keeps_unknown_files(
    fake_game_root, tmp_path
):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    with zipfile.ZipFile(io.BytesIO(archive_bytes())) as archive:
        archive.extractall(win64)
    unknown = win64 / "ue4ss/UserUnknown.dll"
    unknown.write_bytes(b"user")
    service = manager(fake_game_root, tmp_path)

    removed = service.uninstall()

    assert removed["trash_id"]
    assert unknown.read_bytes() == b"user"
    assert not (win64 / "ue4ss/UE4SS.dll").exists()
    assert (win64 / "ue4ss/UE4SS-settings.ini").exists()


def test_bundled_install_creates_owned_auditable_record(fake_game_root, tmp_path):
    service = manager(fake_game_root, tmp_path)

    result = service.install_bundled()

    state = service.state()
    assert result["ok"] is True
    assert state["status"] == "managed"
    assert state["ownership"] == "PalDeck"
    assert state["integrity"] == "healthy"
    assert state["source"] == "bundled"
    assert state["owned_files"] == 6


def test_upstream_install_records_server_verified_asset_source(
    fake_game_root, tmp_path
):
    payload = archive_bytes()
    archive = tmp_path / "UE4SS-Palworld.zip"
    archive.write_bytes(payload)
    asset = Ue4ssAsset(
        archive.name,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "2026-07-14T00:00:00Z",
        "https://example.invalid/archive.zip",
    )
    service = manager(fake_game_root, tmp_path)

    service.install_upstream(asset, archive)

    state = service.state()
    assert state["source"] == "upstream"
    assert state["asset_name"] == archive.name
    assert state["asset_sha256"] == asset.sha256


def test_local_zip_install_is_owned_but_not_claimed_as_bundled(
    fake_game_root, tmp_path
):
    archive = tmp_path / "custom-ue4ss.zip"
    archive.write_bytes(archive_bytes())
    service = manager(fake_game_root, tmp_path)

    service.install_local_zip(archive)

    state = service.state()
    assert state["ownership"] == "PalDeck"
    assert state["source"] == "local_zip"
    assert state["asset_name"] == "custom-ue4ss.zip"
    assert state["asset_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_modified_core_requires_repair_and_repair_preserves_mutable_config(
    fake_game_root, tmp_path
):
    service = manager(fake_game_root, tmp_path, archive_bytes(b"original"))
    service.install_bundled()
    win64 = fake_game_root / "Pal/Binaries/Win64"
    settings = win64 / "ue4ss/UE4SS-settings.ini"
    dll = win64 / "ue4ss/UE4SS.dll"
    settings.write_text("[General]\nUserValue = 9\n", encoding="utf-8")
    dll.write_bytes(b"modified")
    assert service.state()["integrity"] == "modified"

    with pytest.raises(Ue4ssRepairConflict):
        service.repair()
    service.repair(confirm_replace=True)

    assert dll.read_bytes() == b"original"
    assert "UserValue = 9" in settings.read_text(encoding="utf-8")
    assert service.state()["integrity"] == "healthy"


def test_uninstall_recycles_only_owned_immutable_files_and_restore_recovers_them(
    fake_game_root, tmp_path
):
    service = manager(fake_game_root, tmp_path)
    service.install_bundled()
    win64 = fake_game_root / "Pal/Binaries/Win64"
    settings = win64 / "ue4ss/UE4SS-settings.ini"
    mods_txt = win64 / "ue4ss/Mods/mods.txt"
    settings.write_text("user config", encoding="utf-8")
    mods_txt.write_bytes(mods_txt.read_bytes() + b"UserMod : 1\n")

    result = service.uninstall()

    assert settings.read_text(encoding="utf-8") == "user config"
    assert b"UserMod : 1" in mods_txt.read_bytes()
    assert b"BPModLoaderMod" not in mods_txt.read_bytes()
    assert not (win64 / "ue4ss/UE4SS.dll").exists()
    assert service.state()["status"] == "absent"
    trash_id = result["trash_id"]
    assert service.trash.store.get(trash_id).entry_type == "ue4ss_framework"

    restored = service.restore(trash_id)

    assert restored["ok"] is True
    assert (win64 / "ue4ss/UE4SS.dll").read_bytes() == b"dll"
    assert b"UserMod : 1" in mods_txt.read_bytes()
    assert b"BPModLoaderMod : 1" in mods_txt.read_bytes()
    assert service.state()["integrity"] == "healthy"


def test_modified_owned_core_requires_confirmation_before_uninstall(
    fake_game_root, tmp_path
):
    service = manager(fake_game_root, tmp_path)
    service.install_bundled()
    dll = fake_game_root / "Pal/Binaries/Win64/ue4ss/UE4SS.dll"
    dll.write_bytes(b"tampered")

    with pytest.raises(Ue4ssModifiedFiles):
        service.uninstall()

    result = service.uninstall(confirm_modified=True)
    assert result["trash_id"]
    assert not dll.exists()


def test_ownership_save_failure_rolls_repair_files_and_config_back(
    fake_game_root, tmp_path, monkeypatch
):
    service = manager(fake_game_root, tmp_path, archive_bytes(b"original"))
    service.install_bundled()
    win64 = fake_game_root / "Pal/Binaries/Win64"
    dll = win64 / "ue4ss/UE4SS.dll"
    settings = win64 / "ue4ss/UE4SS-settings.ini"
    settings.write_text("[General]\nUser = 7\n", encoding="utf-8")
    dll.write_bytes(b"modified-user-core")
    original_record = service.ownership.get(service.game_fingerprint)
    service.provider = FakeProvider(archive_bytes(b"replacement"))
    monkeypatch.setattr(
        service.ownership,
        "save",
        lambda _record: (_ for _ in ()).throw(OSError("ownership disk full")),
    )

    with pytest.raises(OSError, match="ownership disk full"):
        service.repair(confirm_replace=True)

    assert dll.read_bytes() == b"modified-user-core"
    assert settings.read_text(encoding="utf-8") == "[General]\nUser = 7\n"
    assert service.ownership.get(service.game_fingerprint) == original_record


def test_uninstall_record_failure_rolls_all_files_back(
    fake_game_root, tmp_path, monkeypatch
):
    service = manager(fake_game_root, tmp_path)
    service.install_bundled()
    dll = fake_game_root / "Pal/Binaries/Win64/ue4ss/UE4SS.dll"
    monkeypatch.setattr(service.trash.store, "save", lambda _record: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.uninstall()

    assert dll.read_bytes() == b"dll"
    assert service.state()["integrity"] == "healthy"
    assert service.trash.store.list() == ([], [])


def test_restore_conflict_rolls_partial_restore_back(fake_game_root, tmp_path):
    service = manager(fake_game_root, tmp_path)
    service.install_bundled()
    result = service.uninstall()
    record = service.trash.store.get(result["trash_id"])
    conflict_item = record.files[-1]
    conflict = service.trash.resolve_original(
        conflict_item.original_root, conflict_item.relative_path
    )
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"external conflict")

    with pytest.raises(Exception):
        service.restore(result["trash_id"])

    assert conflict.read_bytes() == b"external conflict"
    assert service.trash.store.get(result["trash_id"]).entry_type == "ue4ss_framework"
    assert service.state()["ownership"] == "none"
