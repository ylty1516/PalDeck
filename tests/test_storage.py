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


def test_corrupt_json_returns_deep_copy_without_mutating_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    default = {"mods": [{"enabled": False}]}

    result = JsonStore(path).read(default)
    result["mods"][0]["enabled"] = True

    assert result is not default
    assert default == {"mods": [{"enabled": False}]}


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
