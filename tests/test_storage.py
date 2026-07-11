import json

import pytest

from backend.storage import JsonStore


def test_write_round_trip_is_atomic_and_removes_temporary_file(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    store = JsonStore(path)
    value = {"name": "帕鲁", "enabled": True}

    store.write(value)

    assert store.read({}) == value
    assert json.loads(path.read_text(encoding="utf-8")) == value
    assert not path.with_name(f"{path.name}.tmp").exists()


def test_serialization_failure_removes_temporary_file_and_preserves_target(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"existing": true}', encoding="utf-8")
    temporary_path = path.with_name(f"{path.name}.tmp")

    with pytest.raises(TypeError):
        JsonStore(path).write({"invalid": object()})

    assert json.loads(path.read_text(encoding="utf-8")) == {"existing": True}
    assert not temporary_path.exists()


def test_write_failure_removes_temporary_file_and_preserves_target(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"existing": true}', encoding="utf-8")
    temporary_path = path.with_name(f"{path.name}.tmp")

    def fail_during_write(value, file, **kwargs):
        file.write("{")
        raise OSError("write failed")

    monkeypatch.setattr("backend.storage.json.dump", fail_during_write)
    with pytest.raises(OSError, match="write failed"):
        JsonStore(path).write({"replacement": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"existing": True}
    assert not temporary_path.exists()


def test_replace_failure_removes_temporary_file_and_preserves_target(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"existing": true}', encoding="utf-8")
    temporary_path = path.with_name(f"{path.name}.tmp")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("backend.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        JsonStore(path).write({"replacement": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"existing": True}
    assert not temporary_path.exists()


def test_corrupt_json_returns_deep_copy_without_mutating_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    default = {"mods": [{"enabled": False}]}

    result = JsonStore(path).read(default)
    result["mods"][0]["enabled"] = True

    assert result is not default
    assert default == {"mods": [{"enabled": False}]}


def test_invalid_utf8_returns_deep_copy_of_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff")
    default = {"items": []}

    result = JsonStore(path).read(default)
    result["items"].append("changed")

    assert default == {"items": []}


@pytest.mark.parametrize("error", [TypeError("bad type"), OSError("read failed")])
def test_read_errors_return_deep_copy_of_default(tmp_path, monkeypatch, error):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    default = {"items": []}

    def fail_open(*args, **kwargs):
        raise error

    monkeypatch.setattr("pathlib.Path.open", fail_open)
    result = JsonStore(path).read(default)
    result["items"].append("changed")

    assert default == {"items": []}


def test_missing_file_returns_deep_copy_of_default(tmp_path):
    default = {"items": []}

    result = JsonStore(tmp_path / "missing.json").read(default)
    result["items"].append("changed")

    assert default == {"items": []}
