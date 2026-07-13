from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from backend.import_selection import SelectionExpiredError, SelectionRegistry, UnsafeSelectionError, list_supported_top_level


def test_lists_only_top_level_regular_zip_and_pak_in_stable_order(tmp_path):
    (tmp_path / "b.PAK").write_bytes(b"pak")
    (tmp_path / "A.zip").write_bytes(b"zip")
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hidden.zip").write_bytes(b"nested")
    assert [path.name for path in list_supported_top_level(tmp_path)] == ["A.zip", "b.PAK"]


def test_rejects_symlink_or_reparse_selected_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("current account cannot create directory links")
    with pytest.raises(UnsafeSelectionError):
        list_supported_top_level(link)


def test_registry_issues_random_detached_metadata_and_consumes_once(tmp_path):
    archive = tmp_path / "Mod.zip"
    archive.write_bytes(b"zip")
    registry = SelectionRegistry(ttl=60, max_items=5)
    issued = registry.issue([archive])
    assert len(issued) == 1
    assert set(issued[0]) == {"selection_token", "name", "size", "kind"}
    token = issued[0]["selection_token"]
    assert token != str(archive)
    assert registry.resolve(token) == archive.resolve()
    registry.consume(token)
    with pytest.raises(SelectionExpiredError):
        registry.resolve(token)


def test_registry_expires_and_bounds_grants(tmp_path):
    first = tmp_path / "a.zip"; first.write_bytes(b"a")
    second = tmp_path / "b.pak"; second.write_bytes(b"b")
    now = [100.0]
    registry = SelectionRegistry(ttl=2, max_items=1, clock=lambda: now[0])
    registry.issue([first])
    with pytest.raises(ValueError, match="too many"):
        registry.issue([second])
    now[0] = 103.0
    issued = registry.issue([second])
    assert registry.resolve(issued[0]["selection_token"]) == second.resolve()
