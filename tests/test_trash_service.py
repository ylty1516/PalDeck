from pathlib import Path

import pytest

from backend.trash_service import (
    TrashPayloadConflict,
    TrashPayloadMissing,
    TrashPayloadModified,
    TrashService,
)


TRASH_ID = "12345678123456781234567812345678"


def make_service(fake_game_root: Path, tmp_path: Path) -> TrashService:
    return TrashService(fake_game_root, tmp_path / "data", game_running=lambda: False)


def test_game_and_disabled_sources_choose_same_volume_payloads(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    game_file = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    disabled_file = tmp_path / "data/disabled/id/A.pak"

    assert service.payload_root_for(game_file, TRASH_ID) == (
        fake_game_root / ".paldeck/trash" / TRASH_ID
    )
    assert service.payload_root_for(disabled_file, TRASH_ID) == (
        tmp_path / "data/trash/payload" / TRASH_ID
    )


def test_payload_root_rejects_path_outside_trusted_game_and_data(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    with pytest.raises(ValueError, match="trusted roots"):
        service.payload_root_for(tmp_path / "outside.pak", TRASH_ID)
    with pytest.raises(ValueError, match="UUID"):
        service.payload_root_for(fake_game_root / "file.pak", "../bad")


def test_resolve_record_path_rejects_parent_absolute_ads_and_reserved_paths(
    fake_game_root, tmp_path
):
    service = make_service(fake_game_root, tmp_path)
    for unsafe in ("../Pal-Windows.pak", "C:/absolute.pak", "safe.txt:stream", "CON.txt"):
        with pytest.raises(ValueError):
            service.resolve_original("tilde_mods", unsafe)
    with pytest.raises(ValueError, match="root"):
        service.resolve_original("unknown", "safe.pak")


def test_move_verify_and_restore_game_file(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    source = fake_game_root / "Pal/Content/Paks/~mods/Folder/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")

    item = service.move_to_payload(TRASH_ID, "tilde_mods", "Folder/A.pak")

    assert not source.exists()
    assert item.original_root == "tilde_mods"
    assert item.relative_path == "Folder/A.pak"
    assert item.payload_root == "game"
    assert item.payload_path == "tilde_mods/Folder/A.pak"
    payload = service.verify_payload(TRASH_ID, item)
    assert payload.read_bytes() == b"payload"

    moved = service.restore_file(TRASH_ID, item)
    assert moved == (source, payload)
    assert source.read_bytes() == b"payload"
    assert not payload.exists()


def test_move_and_restore_disabled_file_use_data_payload(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    source = tmp_path / "data/disabled/mod-id/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"disabled")

    item = service.move_to_payload(TRASH_ID, "disabled", "mod-id/A.pak")

    assert item.payload_root == "data"
    service.restore_file(TRASH_ID, item)
    assert source.read_bytes() == b"disabled"


def test_move_fails_before_mutation_when_payload_is_not_same_volume(
    fake_game_root, tmp_path, monkeypatch
):
    service = make_service(fake_game_root, tmp_path)
    source = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    monkeypatch.setattr(service, "_same_volume", lambda *_args: False)

    with pytest.raises(OSError, match="same volume"):
        service.move_to_payload(TRASH_ID, "tilde_mods", "A.pak")

    assert source.read_bytes() == b"payload"


def test_verify_rejects_missing_or_modified_payload(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    source = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    item = service.move_to_payload(TRASH_ID, "tilde_mods", "A.pak")
    payload = service.verify_payload(TRASH_ID, item)

    payload.write_bytes(b"changed")
    with pytest.raises(TrashPayloadModified):
        service.verify_payload(TRASH_ID, item)
    payload.unlink()
    with pytest.raises(TrashPayloadMissing):
        service.verify_payload(TRASH_ID, item)


def test_restore_never_overwrites_existing_target(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    source = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    item = service.move_to_payload(TRASH_ID, "tilde_mods", "A.pak")
    source.write_bytes(b"foreign")

    with pytest.raises(TrashPayloadConflict) as conflict:
        service.restore_file(TRASH_ID, item)

    assert conflict.value.details == {"files": [str(source)]}
    assert source.read_bytes() == b"foreign"
    assert service.verify_payload(TRASH_ID, item).read_bytes() == b"payload"


def test_move_rejects_reparse_source(fake_game_root, tmp_path, monkeypatch):
    service = make_service(fake_game_root, tmp_path)
    source = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    monkeypatch.setattr(
        "backend.trash_service._is_reparse",
        lambda path: Path(path) == source,
    )

    with pytest.raises(ValueError, match="reparse"):
        service.move_to_payload(TRASH_ID, "tilde_mods", "A.pak")


def test_rollback_moves_in_reverse_and_preserves_original_bytes(fake_game_root, tmp_path):
    service = make_service(fake_game_root, tmp_path)
    first = fake_game_root / "Pal/Content/Paks/~mods/A.pak"
    second = fake_game_root / "Pal/Content/Paks/~mods/B.pak"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    one = service.move_to_payload(TRASH_ID, "tilde_mods", "A.pak")
    two = service.move_to_payload(TRASH_ID, "tilde_mods", "B.pak")
    current_one = service.verify_payload(TRASH_ID, one)
    current_two = service.verify_payload(TRASH_ID, two)

    service.rollback_moves([(current_one, first), (current_two, second)])

    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"
