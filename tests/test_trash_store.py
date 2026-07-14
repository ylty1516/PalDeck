import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.trash_store import TrashFile, TrashRecord, TrashStore


def valid_record() -> TrashRecord:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    return TrashRecord(
        id="12345678123456781234567812345678",
        schema_version=1,
        entry_type="local_mod",
        name="Example",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=30)).isoformat(),
        game_fingerprint="a" * 64,
        files=(
            TrashFile(
                "tilde_mods",
                "Example.pak",
                "game",
                "Example.pak",
                3,
                "b" * 64,
            ),
        ),
        manifest={"id": "12345678123456781234567812345678"},
        ue4ss_state=None,
        framework_ownership=None,
    )


def test_store_round_trips_strict_record(tmp_path: Path):
    store = TrashStore(tmp_path / "records")
    record = valid_record()

    store.save(record)

    assert store.get(record.id) == record
    valid, invalid = store.list()
    assert valid == [record]
    assert invalid == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("entry_type", "other"),
        ("game_fingerprint", "../bad"),
        ("created_at", "2026-01-01"),
        ("expires_at", "2026-01-01"),
        ("name", ""),
        ("id", "not-a-uuid"),
    ],
)
def test_record_rejects_invalid_fields(field: str, value):
    raw = TrashStore.to_dict(valid_record())
    raw[field] = value

    with pytest.raises(ValueError):
        TrashStore.from_dict(raw)


def test_record_and_file_reject_unknown_fields():
    raw = TrashStore.to_dict(valid_record())
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="fields"):
        TrashStore.from_dict(raw)

    raw = TrashStore.to_dict(valid_record())
    raw["files"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="file fields"):
        TrashStore.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("original_root", "other"),
        ("relative_path", "../escape.pak"),
        ("relative_path", "C:/absolute.pak"),
        ("relative_path", "safe.txt:stream"),
        ("relative_path", "CON.txt"),
        ("payload_root", "other"),
        ("payload_path", "../escape.pak"),
        ("size", -1),
        ("sha256", "bad"),
    ],
)
def test_record_rejects_unsafe_file_fields(field: str, value):
    raw = TrashStore.to_dict(valid_record())
    raw["files"][0][field] = value

    with pytest.raises(ValueError):
        TrashStore.from_dict(raw)


def test_record_rejects_duplicate_normalized_file_identity():
    raw = TrashStore.to_dict(valid_record())
    duplicate = dict(raw["files"][0])
    duplicate["relative_path"] = "example.PAK"
    duplicate["payload_path"] = "second.pak"
    raw["files"].append(duplicate)

    with pytest.raises(ValueError, match="duplicate"):
        TrashStore.from_dict(raw)


def test_list_reports_but_does_not_delete_corrupt_records(tmp_path: Path):
    records = tmp_path / "records"
    records.mkdir()
    corrupt = records / "12345678123456781234567812345678.json"
    corrupt.write_text("{broken", encoding="utf-8")
    unexpected = records / "not-a-record.json"
    unexpected.write_text("{}", encoding="utf-8")
    store = TrashStore(records)

    valid, invalid = store.list()

    assert valid == []
    assert invalid == ["12345678123456781234567812345678", "not-a-record"]
    assert corrupt.exists() and unexpected.exists()


def test_save_is_atomic_and_delete_requires_uuid(tmp_path: Path, monkeypatch):
    store = TrashStore(tmp_path / "records")
    record = valid_record()
    store.save(record)
    path = tmp_path / "records" / f"{record.id}.json"
    before = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("publish failed")

    monkeypatch.setattr("backend.trash_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="publish failed"):
        store.save(replace(record, name="Changed"))

    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(ValueError):
        store.delete("../bad")


def test_delete_and_missing_get_are_explicit(tmp_path: Path):
    store = TrashStore(tmp_path / "records")
    record = valid_record()
    store.save(record)
    store.delete(record.id)
    with pytest.raises(KeyError):
        store.get(record.id)
