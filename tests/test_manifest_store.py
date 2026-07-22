import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from backend.domain import ManifestFile, ModKind
from backend.manifest_store import ManifestStore


def _create(store: ManifestStore, install_root: Path, files: list[Path], **kwargs):
    return store.create(
        name=kwargs.pop("name", "测试模组"),
        kind=kwargs.pop("kind", ModKind.PAK),
        install_root=install_root,
        files=files,
        source_name=kwargs.pop("source_name", "来源.zip"),
        **kwargs,
    )


def test_fixed_contract_accepts_positional_create_and_audits_manifest(tmp_path):
    install_root = tmp_path / "game" / "mods"
    install_root.mkdir(parents=True)
    payload = install_root / "a.pak"
    payload.write_bytes(b"a")
    manifests = tmp_path / "data" / "manifests"
    store = ManifestStore(manifests)

    manifest = store.create(
        "Positional", ModKind.PAK, install_root, [payload], "source.zip", 9
    )

    assert manifest.source_name == "source.zip"
    assert manifest.nexus_id == 9
    assert store.root == manifests
    assert store.manifests_dir == manifests
    assert (manifests / f"{manifest.id}.json").is_file()
    assert store.audit(manifest).status == "enabled"


def test_create_roundtrips_unicode_and_ue4ss_metadata(tmp_path):
    root = tmp_path / "游戏" / "Mods" / "模组"
    root.mkdir(parents=True)
    payload = root / "脚本.lua"
    payload.write_bytes("你好".encode())
    enabled_txt = root / "enabled.txt"
    enabled_txt.write_text("1\n", encoding="utf-8")
    store = ManifestStore(tmp_path / "data" / "manifests")

    created = _create(
        store,
        root,
        [payload, enabled_txt],
        kind=ModKind.UE4SS,
        nexus_id=42,
        ue4ss_enabled_txt=enabled_txt,
    )

    loaded = store.get(created.id)
    assert loaded == created
    assert loaded.kind is ModKind.UE4SS
    assert loaded.name == "测试模组"
    assert loaded.files[0].relative_path == "enabled.txt"
    assert loaded.ue4ss_enabled_txt is not None
    assert loaded.ue4ss_enabled_txt.relative_path == "enabled.txt"
    assert [item.id for item in store.list()] == [created.id]
    assert (tmp_path / "data" / "manifests" / f"{created.id}.json").is_file()


def test_list_is_stably_sorted_by_installed_at_then_id(tmp_path, monkeypatch):
    root = tmp_path / "mods"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    values = iter(["0" * 32, "f" * 32, "1" * 32])
    monkeypatch.setattr("backend.manifest_store.uuid.uuid4", lambda: type("U", (), {"hex": next(values)})())
    store = ManifestStore(tmp_path / "data" / "manifests")
    _create(store, root, [payload], installed_at="2026-01-02T00:00:00+00:00")
    _create(store, root, [payload], installed_at="2026-01-01T00:00:00+00:00")
    _create(store, root, [payload], installed_at="2026-01-01T00:00:00+00:00")
    assert [item.id for item in store.list()] == ["1" * 32, "f" * 32, "0" * 32]


def test_get_missing_raises_key_error_and_delete_is_idempotent(tmp_path):
    store = ManifestStore(tmp_path / "manifests")
    with pytest.raises(KeyError):
        store.get("missing")
    store.delete("missing")


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(id="not-a-uuid"),
        lambda value: value.update(name=3),
        lambda value: value.update(kind="loose"),
        lambda value: value.update(install_root=3),
        lambda value: value.update(source_name=[]),
        lambda value: value.update(nexus_id=True),
        lambda value: value.update(installed_at="not-a-date"),
        lambda value: value.update(enabled=1),
        lambda value: value.update(files="not-a-list"),
        lambda value: value["files"][0].update(relative_path="../outside.pak"),
        lambda value: value["files"][0].update(relative_path="C:\\outside.pak"),
        lambda value: value["files"][0].update(size=-1),
        lambda value: value["files"][0].update(size=True),
        lambda value: value["files"][0].update(sha256="not-a-sha256"),
        lambda value: value.update(files=[value["files"][0], {**value["files"][0], "relative_path": "A.PAK"}]),
    ],
    ids=[
        "uuid", "name-type", "kind", "install-root-type", "source-name-type", "nexus-id-type",
        "installed-at", "enabled-type", "files-type", "parent-traversal", "windows-absolute",
        "negative-size", "bool-size", "sha256", "windows-normalized-duplicate",
    ],
)
def test_invalid_persisted_manifest_is_rejected_by_get_and_skipped_by_list(tmp_path, change):
    root = tmp_path / "mods"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    path = store.manifests_dir / f"{manifest.id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match=f"invalid manifest: {manifest.id}"):
        store.get(manifest.id)
    assert store.list() == []


def test_audit_detects_modified_missing_disabled_and_conflict(tmp_path):
    root = tmp_path / "live"
    disabled = tmp_path / "data" / "disabled"
    root.mkdir()
    disabled.mkdir(parents=True)
    payload = root / "a.pak"
    payload.write_bytes(b"original")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])

    assert store.audit(manifest.id).status == "enabled"
    payload.write_bytes(b"changed")
    assert store.audit(manifest.id).status == "modified"
    payload.unlink()
    assert store.audit(manifest.id).status == "missing"
    disabled_payload = disabled / manifest.id / "a.pak"
    disabled_payload.parent.mkdir()
    disabled_payload.write_bytes(b"original")
    assert store.audit(manifest.id).status == "disabled"
    payload.write_bytes(b"original")
    assert store.audit(manifest.id).status == "conflict"


def test_audit_does_not_accept_a_caller_supplied_disabled_root(tmp_path):
    store = ManifestStore(tmp_path / "data" / "manifests")
    with pytest.raises(TypeError):
        store.audit("0" * 32, tmp_path / "other-disabled")


def test_audit_uses_data_dir_as_default_disabled_root(tmp_path):
    live = tmp_path / "game" / "live"
    live.mkdir(parents=True)
    payload = live / "a.pak"
    payload.write_bytes(b"original")
    store = ManifestStore(tmp_path / "state" / "data" / "manifests")
    manifest = _create(store, live, [payload])
    payload.unlink()
    disabled_payload = tmp_path / "state" / "data" / "disabled" / manifest.id / "a.pak"
    disabled_payload.parent.mkdir(parents=True)
    disabled_payload.write_bytes(b"original")

    assert store.audit(manifest.id).status == "disabled"


def test_audit_rejects_traversal_without_hashing_external_file(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    payload = live / "a.pak"
    payload.write_bytes(b"original")
    outside = tmp_path / "outside.pak"
    outside.write_bytes(b"outside")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, live, [payload])
    unsafe = replace(
        manifest,
        files=(ManifestFile("../outside.pak", outside.stat().st_size, "0" * 64),),
    )
    monkeypatch.setattr(store, "get", lambda manifest_id: unsafe)
    monkeypatch.setattr("backend.manifest_store._sha256", lambda path: pytest.fail("external file was hashed"))

    with pytest.raises(ValueError, match="relative_path"):
        store.audit(manifest.id)


def test_audit_rejects_reparse_before_hashing(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    payload = live / "a.pak"
    payload.write_bytes(b"original")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, live, [payload])
    monkeypatch.setattr("backend.manifest_store._is_reparse", lambda path: Path(path) == payload)
    monkeypatch.setattr("backend.manifest_store._sha256", lambda path: pytest.fail("reparse point was hashed"))

    with pytest.raises(ValueError, match="symlink/reparse"):
        store.audit(manifest.id)


def test_create_rejects_reparse_in_install_root_ancestor(tmp_path, monkeypatch):
    ancestor = tmp_path / "junction"
    root = ancestor / "mods"
    root.mkdir(parents=True)
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    original = __import__("backend.manifest_store", fromlist=["_is_reparse"])._is_reparse
    monkeypatch.setattr(
        "backend.manifest_store._is_reparse",
        lambda path: Path(path) == ancestor or original(Path(path)),
    )

    with pytest.raises(ValueError, match="symlink/reparse"):
        _create(ManifestStore(tmp_path / "data" / "manifests"), root, [payload])


@pytest.mark.parametrize(
    "relative_path",
    ["file.txt:stream", "file.", "file ", "CON", "aux.txt", "bad<name.pak"],
    ids=["ads", "trailing-dot", "trailing-space", "device", "device-extension", "invalid-char"],
)
def test_persisted_manifest_rejects_unsafe_windows_components(tmp_path, relative_path):
    root = tmp_path / "mods"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    path = store.manifests_dir / f"{manifest.id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["files"][0]["relative_path"] = relative_path
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="relative_path"):
        store.get(manifest.id)


def test_create_rejects_outside_duplicate_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "a.pak"
    inside.write_bytes(b"a")
    outside = tmp_path / "outside.pak"
    outside.write_bytes(b"x")
    store = ManifestStore(tmp_path / "data" / "manifests")

    with pytest.raises(ValueError):
        _create(store, root, [outside])
    with pytest.raises(ValueError):
        _create(store, root, [inside, root / "." / "a.pak"])

    link = root / "link.pak"
    try:
        link.symlink_to(inside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        _create(store, root, [link])


def test_create_rejects_directory_and_missing_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    store = ManifestStore(tmp_path / "data" / "manifests")
    with pytest.raises(ValueError):
        _create(store, root, [root])
    with pytest.raises(ValueError):
        _create(store, root, [root / "missing.pak"])


def test_save_rejects_invalid_schema_before_replacing_manifest(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    path = store.manifests_dir / f"{manifest.id}.json"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="invalid manifest"):
        store.save(replace(manifest, enabled=1))
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["files", "ue4ss_enabled_txt"])
def test_save_rejects_every_path_outside_install_root(tmp_path, field):
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    unsafe = ManifestFile("../outside.pak", 1, "0" * 64)
    changed = replace(
        manifest,
        files=(unsafe,) if field == "files" else manifest.files,
        ue4ss_enabled_txt=unsafe if field == "ue4ss_enabled_txt" else None,
    )

    with pytest.raises(ValueError, match="relative_path"):
        store.save(changed)


def test_get_rejects_manifest_whose_json_id_does_not_match_requested_id(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    path = store.manifests_dir / f"{manifest.id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["id"] = "ffffffffffffffffffffffffffffffff"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match requested id"):
        store.get(manifest.id)
    assert store.list() == []


def test_atomic_save_failure_preserves_previous_manifest(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "a.pak"
    payload.write_bytes(b"a")
    store = ManifestStore(tmp_path / "data" / "manifests")
    manifest = _create(store, root, [payload])
    path = tmp_path / "data" / "manifests" / f"{manifest.id}.json"
    before = path.read_bytes()
    changed = manifest.__class__(**{**manifest.__dict__, "name": "changed"})

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.save(changed)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


def test_migrate_uses_constructor_known_roots_with_single_argument(tmp_path):
    known = tmp_path / "known"
    known.mkdir()
    payload = known / "a.pak"
    payload.write_bytes(b"a")
    legacy = tmp_path / "registry.json"
    legacy.write_text(json.dumps([{
        "id": "cccccccccccccccccccccccccccccccc", "name": "legacy",
        "mod_type": "pak", "install_path": str(payload), "files": [payload.name],
    }]), encoding="utf-8")
    store = ManifestStore(tmp_path / "data" / "manifests", known_roots=[known])

    assert [item.id for item in store.migrate_legacy_registry(legacy)] == ["c" * 32]
    assert ManifestStore(tmp_path / "other" / "manifests").migrate_legacy_registry(legacy) == []


def test_migration_save_failure_leaves_no_random_manifest(tmp_path, monkeypatch):
    known = tmp_path / "known"
    known.mkdir()
    payload = known / "a.pak"
    payload.write_bytes(b"a")
    legacy = tmp_path / "registry.json"
    legacy.write_text(json.dumps([{
        "id": "dddddddddddddddddddddddddddddddd", "name": "legacy",
        "mod_type": "pak", "install_path": str(payload), "files": [payload.name],
    }]), encoding="utf-8")
    manifests = tmp_path / "data" / "manifests"
    store = ManifestStore(manifests, known_roots=[known])
    monkeypatch.setattr("backend.manifest_store.uuid.uuid4", lambda: pytest.fail("random UUID generated"))
    monkeypatch.setattr(store, "save", lambda manifest: (_ for _ in ()).throw(OSError("save failed")))

    assert store.migrate_legacy_registry(legacy) == []
    assert not manifests.exists() or list(manifests.iterdir()) == []


def test_migrate_legacy_registry_skips_unsafe_entries_and_is_idempotent(tmp_path):
    known = tmp_path / "known"
    known.mkdir()
    good = known / "good.pak"
    good.write_bytes(b"ok")
    outside = tmp_path / "outside.pak"
    outside.write_bytes(b"no")
    legacy = tmp_path / "mods_registry.json"
    legacy.write_text(json.dumps([
        {"id": "12345678123456781234567812345678", "name": "旧模组", "mod_type": "pak", "enabled": False,
         "install_path": str(good), "source_name": "旧.zip", "files": [good.name],
         "installed_at": "2025-01-01T00:00:00+00:00", "nexus_id": 7},
        {"id": "legacy-out", "name": "bad", "mod_type": "pak", "install_path": str(outside), "files": [outside.name]},
        {"name": "malformed"},
    ]), encoding="utf-8")
    store = ManifestStore(tmp_path / "data" / "manifests")

    first = store.migrate_legacy_registry(legacy, [known])
    second = store.migrate_legacy_registry(legacy, [known])

    assert [item.id for item in first] == ["12345678123456781234567812345678"]
    assert second == []
    assert store.get("12345678123456781234567812345678").enabled is False
    assert legacy.is_file()
    assert len(store.list()) == 1


def test_migrate_skips_symlinked_legacy_path(tmp_path):
    known = tmp_path / "known"
    known.mkdir()
    target = known / "target.pak"
    target.write_bytes(b"payload")
    link = known / "link.pak"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    legacy = tmp_path / "mods_registry.json"
    legacy.write_text(json.dumps([{
        "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "link",
        "mod_type": "pak", "install_path": str(link), "files": ["link.pak"],
    }]), encoding="utf-8")
    assert ManifestStore(tmp_path / "data" / "manifests").migrate_legacy_registry(legacy, [known]) == []


def test_migrate_checks_legacy_path_for_reparse_before_resolving(tmp_path, monkeypatch):
    known = tmp_path / "known"
    known.mkdir()
    flagged = known / "flagged.pak"
    flagged.write_bytes(b"payload")
    legacy = tmp_path / "mods_registry.json"
    legacy.write_text(json.dumps([{
        "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "flagged",
        "mod_type": "pak", "install_path": str(flagged), "files": [flagged.name],
    }]), encoding="utf-8")
    from backend import manifest_store
    original = manifest_store._is_reparse
    monkeypatch.setattr(
        manifest_store, "_is_reparse",
        lambda path: Path(path) == flagged or original(Path(path)),
    )
    assert ManifestStore(tmp_path / "data" / "manifests").migrate_legacy_registry(legacy, [known]) == []


def test_migrate_corrupt_json_is_safe(tmp_path):
    legacy = tmp_path / "mods_registry.json"
    legacy.write_text("{broken", encoding="utf-8")
    assert ManifestStore(tmp_path / "data" / "manifests").migrate_legacy_registry(legacy, [tmp_path]) == []


def test_mod_manager_lazily_exposes_manifest_store_without_mod_service(tmp_path, monkeypatch):
    from backend import mod_manager

    monkeypatch.setattr(mod_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod_manager, "_manifest_store_instance", None)
    first = mod_manager.get_manifest_store()
    assert isinstance(first, ManifestStore)
    assert first.root == tmp_path / "manifests"
    assert first is mod_manager.get_manifest_store()
    assert mod_manager._get_mod_service(required=False) is None
