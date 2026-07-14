from pathlib import Path

import pytest

from backend.ue4ss_ownership import (
    OwnedFrameworkFile,
    OwnershipRecord,
    OwnershipStore,
    OwnershipStoreInvalid,
)


FINGERPRINT = "a" * 64


def create_record(win64: Path, target: Path, *, mutable: bool = False) -> OwnershipRecord:
    return OwnershipRecord.create(
        game_fingerprint=FINGERPRINT,
        source="bundled",
        asset_name="UE4SS-Palworld.zip",
        asset_sha256="b" * 64,
        files=[OwnedFrameworkFile.from_path(win64, target, mutable=mutable)],
        framework_mods=("BPModLoaderMod",),
    )


def test_ownership_round_trip_and_core_audit(tmp_path, fake_game_root):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    target = win64 / "ue4ss/UE4SS.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dll")
    record = create_record(win64, target)
    store = OwnershipStore(tmp_path / "ue4ss-framework-v1.json")

    store.save(record)

    assert store.get(FINGERPRINT) == record
    assert store.audit(record, win64).integrity == "healthy"
    target.write_bytes(b"changed")
    audit = store.audit(record, win64)
    assert audit.integrity == "modified"
    assert audit.modified == ("ue4ss/UE4SS.dll",)


def test_audit_reports_missing_and_conflict(tmp_path, fake_game_root, monkeypatch):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    target = win64 / "ue4ss/UE4SS.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dll")
    record = create_record(win64, target)
    store = OwnershipStore(tmp_path / "ownership.json")

    target.unlink()
    assert store.audit(record, win64).integrity == "missing"
    target.mkdir()
    assert store.audit(record, win64).integrity == "conflict"


def test_mutable_content_changes_are_expected_but_missing_is_reported(
    tmp_path, fake_game_root
):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    settings = win64 / "ue4ss/UE4SS-settings.ini"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"original")
    record = create_record(win64, settings, mutable=True)
    store = OwnershipStore(tmp_path / "ownership.json")

    settings.write_bytes(b"user changed config")
    assert store.audit(record, win64).integrity == "healthy"
    settings.unlink()
    assert store.audit(record, win64).integrity == "missing"


def test_owned_file_rejects_paths_outside_win64(fake_game_root):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    outside = fake_game_root / "outside.dll"
    outside.write_bytes(b"bad")
    with pytest.raises(ValueError):
        OwnedFrameworkFile.from_path(win64, outside, mutable=False)


@pytest.mark.parametrize("relative", ["../escape.dll", "C:/absolute.dll", "safe:stream", "CON.dll"])
def test_owned_file_rejects_unsafe_relative_path(relative):
    with pytest.raises(ValueError):
        OwnedFrameworkFile(relative, 1, "a" * 64, False)


def test_record_rejects_wrong_source_digest_duplicates_and_framework_names(
    fake_game_root
):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    target = win64 / "ue4ss/UE4SS.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dll")
    owned = OwnedFrameworkFile.from_path(win64, target, mutable=False)
    with pytest.raises(ValueError):
        OwnershipRecord.create(FINGERPRINT, "remote", "asset.zip", "b" * 64, [owned], ())
    with pytest.raises(ValueError):
        OwnershipRecord.create(FINGERPRINT, "bundled", "asset.zip", "bad", [owned], ())
    with pytest.raises(ValueError):
        OwnershipRecord.create(FINGERPRINT, "bundled", "asset.zip", "b" * 64, [owned, owned], ())
    with pytest.raises(ValueError):
        OwnershipRecord.create(FINGERPRINT, "bundled", "asset.zip", "b" * 64, [owned], ("../bad",))


def test_corrupt_store_fails_closed_and_is_not_rewritten(tmp_path):
    path = tmp_path / "ownership.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = OwnershipStore(path)

    with pytest.raises(OwnershipStoreInvalid):
        store.get(FINGERPRINT)
    with pytest.raises(OwnershipStoreInvalid):
        store.list()
    assert path.read_bytes() == before


def test_delete_is_scoped_to_one_game(tmp_path, fake_game_root):
    win64 = fake_game_root / "Pal/Binaries/Win64"
    target = win64 / "ue4ss/UE4SS.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dll")
    first = create_record(win64, target)
    second = OwnershipRecord.create(
        game_fingerprint="c" * 64,
        source="local_zip",
        asset_name="local.zip",
        asset_sha256="d" * 64,
        files=first.files,
        framework_mods=(),
    )
    store = OwnershipStore(tmp_path / "ownership.json")
    store.save(first)
    store.save(second)

    store.delete(FINGERPRINT)

    with pytest.raises(KeyError):
        store.get(FINGERPRINT)
    assert store.get("c" * 64) == second
