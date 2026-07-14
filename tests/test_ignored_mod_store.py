import json
from pathlib import Path

import pytest

from backend.ignored_mod_store import (
    IgnoredIdentity,
    IgnoredModStore,
    IgnoredStoreInvalid,
)


GAME_A = "a" * 64
GAME_B = "b" * 64


def test_identity_is_casefolded_and_scoped_to_game(tmp_path: Path):
    store = IgnoredModStore(tmp_path / "ignored-mods-v1.json")
    identity = IgnoredIdentity("ue4ss", "ue4ss_nested", "AnywhereFastTravel")

    store.add(GAME_A, identity)

    assert store.contains(
        GAME_A, IgnoredIdentity("UE4SS", "ue4ss_nested", "anywherefasttravel")
    )
    assert not store.contains(GAME_B, identity)
    assert store.list(GAME_A) == (
        IgnoredIdentity("ue4ss", "ue4ss_nested", "anywherefasttravel"),
    )


def test_add_is_idempotent_remove_and_reset_are_scoped(tmp_path: Path):
    store = IgnoredModStore(tmp_path / "ignored-mods-v1.json")
    pak = IgnoredIdentity("pak", "tilde_mods", "A.PAK")
    logic = IgnoredIdentity("logicpak", "logic_mods", "Logic.pak")
    store.add(GAME_A, pak)
    store.add(GAME_A, pak)
    store.add(GAME_A, logic)
    store.add(GAME_B, pak)

    assert len(store.list(GAME_A)) == 2
    store.remove(GAME_A, pak)
    assert store.list(GAME_A) == (logic,)
    assert store.reset(GAME_A) == 1
    assert store.list(GAME_A) == ()
    assert store.list(GAME_B) == (pak,)


@pytest.mark.parametrize(
    "identity",
    [
        ("other", "tilde_mods", "A.pak"),
        ("pak", "logic_mods", "A.pak"),
        ("logicpak", "tilde_mods", "A.pak"),
        ("ue4ss", "tilde_mods", "Folder"),
        ("pak", "tilde_mods", "../A.pak"),
        ("pak", "tilde_mods", "folder/A.pak"),
        ("pak", "tilde_mods", "safe.txt:stream"),
        ("pak", "tilde_mods", "CON.txt"),
    ],
)
def test_identity_rejects_invalid_kind_root_or_key(identity):
    with pytest.raises(ValueError):
        IgnoredIdentity(*identity)


def test_store_rejects_invalid_fingerprint(tmp_path: Path):
    store = IgnoredModStore(tmp_path / "ignored-mods-v1.json")
    with pytest.raises(ValueError):
        store.add("../bad", IgnoredIdentity("pak", "tilde_mods", "A.pak"))


def test_corrupt_or_unknown_schema_fails_closed_without_rewrite(tmp_path: Path):
    path = tmp_path / "ignored-mods-v1.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = IgnoredModStore(path)

    with pytest.raises(IgnoredStoreInvalid):
        store.list(GAME_A)
    with pytest.raises(IgnoredStoreInvalid):
        store.add(GAME_A, IgnoredIdentity("pak", "tilde_mods", "A.pak"))
    assert path.read_bytes() == before

    path.write_text(
        json.dumps({"schema_version": 2, "games": {}, "unexpected": True}),
        encoding="utf-8",
    )
    with pytest.raises(IgnoredStoreInvalid):
        store.list(GAME_A)


def test_store_rejects_unknown_entry_fields(tmp_path: Path):
    path = tmp_path / "ignored-mods-v1.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "games": {
                    GAME_A: [
                        {
                            "kind": "pak",
                            "root": "tilde_mods",
                            "key": "a.pak",
                            "unexpected": True,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(IgnoredStoreInvalid):
        IgnoredModStore(path).list(GAME_A)
