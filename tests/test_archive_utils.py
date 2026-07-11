from __future__ import annotations

import io
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from backend import archive_utils
from backend.archive_utils import inspect_and_extract
from backend.domain import ArchivePolicy, ModKind


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def assert_rejected(archive: Path, dest: Path, match: str | None = None) -> None:
    with pytest.raises(ValueError, match=match):
        inspect_and_extract(archive, dest)
    assert not dest.exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.pak", "wrapper/../../escape.pak", "/absolute.pak", r"C:\escape.pak"],
)
def test_rejects_paths_that_can_escape_destination(tmp_path: Path, name: str) -> None:
    archive = make_zip(tmp_path / "bad.zip", {name: b"pak"})

    assert_rejected(archive, tmp_path / "out", "路径")
    assert not (tmp_path / "escape.pak").exists()


@pytest.mark.parametrize("name", ["", ".", "folder/./file.pak", "folder//file.pak", "bad\x00name.pak"])
def test_rejects_empty_or_abnormal_paths(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "bad.zip"
    if "\x00" in name:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("safe.pak", b"pak")
        data = archive_path.read_bytes().replace(b"safe.pak", b"bad\x00name")
        archive_path.write_bytes(data)
    else:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(name, b"pak")

    assert_rejected(archive_path, tmp_path / "out")


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644])
def test_rejects_symlink_and_special_file_external_attributes(tmp_path: Path, mode: int) -> None:
    archive_path = tmp_path / f"bad-{mode}.zip"
    info = zipfile.ZipInfo("link.pak")
    info.create_system = 3
    info.external_attr = mode << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, b"target")

    assert_rejected(archive_path, tmp_path / "out", "特殊文件")


def test_rejects_encrypted_entry_flag(tmp_path: Path) -> None:
    archive_path = make_zip(tmp_path / "encrypted.zip", {"mod.pak": b"pak"})
    data = bytearray(archive_path.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + 6, struct.unpack_from("<H", data, local + 6)[0] | 1)
    struct.pack_into("<H", data, central + 8, struct.unpack_from("<H", data, central + 8)[0] | 1)
    archive_path.write_bytes(data)

    assert_rejected(archive_path, tmp_path / "out", "加密")


def test_rejects_file_count_limit(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "many.zip", {"a.pak": b"a", "b.txt": b"b"})

    with pytest.raises(ValueError, match="文件数"):
        inspect_and_extract(archive, tmp_path / "out", ArchivePolicy(max_files=1))
    assert not (tmp_path / "out").exists()


def test_file_count_limit_includes_directory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "directories.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("one/", b"")
        archive.writestr("one/two/", b"")
        archive.writestr("one/two/mod.pak", b"pak")
    dest = tmp_path / "out"

    def must_not_create_staging(*args, **kwargs):
        raise AssertionError("entry limit must be checked before staging creation")

    monkeypatch.setattr(archive_utils.tempfile, "mkdtemp", must_not_create_staging)

    with pytest.raises(ValueError, match="文件数|条目数"):
        inspect_and_extract(archive_path, dest, ArchivePolicy(max_files=1))

    assert not dest.exists()
    assert not list(tmp_path.glob(".out.*.tmp"))


def test_rejects_declared_single_file_size_limit(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "large.zip", {"large.pak": b"12345"})

    with pytest.raises(ValueError, match="单个文件"):
        inspect_and_extract(archive, tmp_path / "out", ArchivePolicy(max_single_bytes=4))
    assert not (tmp_path / "out").exists()


def test_rejects_declared_total_size_limit(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "large.zip", {"a.pak": b"123", "a.utoc": b"456"})

    with pytest.raises(ValueError, match="总大小"):
        inspect_and_extract(archive, tmp_path / "out", ArchivePolicy(max_total_bytes=5))
    assert not (tmp_path / "out").exists()


def test_actual_streamed_total_limit_removes_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = make_zip(tmp_path / "bomb.zip", {"bomb.pak": b"x", "bomb.utoc": b"y"})
    original_open = zipfile.ZipFile.open

    def oversized_open(self, name, mode="r", pwd=None, *, force_zip64=False):
        filename = getattr(name, "filename", name)
        if mode == "r" and filename in {"bomb.pak", "bomb.utoc"}:
            return io.BytesIO(b"123")
        return original_open(self, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", oversized_open)
    dest = tmp_path / "out"

    with pytest.raises(ValueError, match="实际总大小"):
        inspect_and_extract(
            archive,
            dest,
            ArchivePolicy(max_single_bytes=4, max_total_bytes=5),
        )

    assert not dest.exists()


def test_reports_damaged_zip_in_actionable_chinese(tmp_path: Path) -> None:
    archive = tmp_path / "damaged.zip"
    archive.write_bytes(b"this is not a zip")

    assert_rejected(archive, tmp_path / "out", "ZIP.*损坏|损坏.*ZIP")


def test_rejects_unknown_archive_instead_of_guessing_loose_mod(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "unknown.zip", {"SomeFolder/readme.txt": b"hello"})

    assert_rejected(archive, tmp_path / "out", "无法识别")


def test_logicmods_groups_pak_with_same_stem_sidecars(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "logic.zip",
        {
            "Wrapper/Pal/Content/Paks/LogicMods/CoolMod.pak": b"pak",
            "Wrapper/Pal/Content/Paks/LogicMods/CoolMod.utoc": b"utoc",
            "Wrapper/Pal/Content/Paks/LogicMods/CoolMod.ucas": b"ucas",
            "Wrapper/Pal/Content/Paks/LogicMods/Other.pak": b"other",
        },
    )

    result = inspect_and_extract(archive, tmp_path / "out")

    assert result.kind is ModKind.LOGICPAK
    assert result.content_root.name == "LogicMods"
    assert [[path.suffix.lower() for path in group] for group in result.groups] == [
        [".pak", ".utoc", ".ucas"],
        [".pak"],
    ]


def test_regular_pak_groups_sidecars_and_uses_innermost_mod_directory(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "pak.zip",
        {"Wrapper/MyMod.pak": b"pak", "Wrapper/MyMod.utoc": b"utoc"},
    )

    result = inspect_and_extract(archive, tmp_path / "out")

    assert result.kind is ModKind.PAK
    assert result.display_name == "Wrapper"
    assert result.content_root.name == "Wrapper"
    assert len(result.groups) == 1
    assert {path.suffix.lower() for path in result.groups[0]} == {".pak", ".utoc"}


def test_pak_at_archive_root_uses_first_pak_stem_and_cleans_name(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "pak.zip", {"My Mod .pak": b"pak"})

    result = inspect_and_extract(archive, tmp_path / "out")

    assert result.display_name == "My Mod"


def test_main_lua_must_be_the_file_directly_inside_scripts(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "not-ue4ss.zip", {"Mod/Scripts/main.lua/readme.txt": b"no"})

    assert_rejected(archive, tmp_path / "out", "无法识别")


def test_ue4ss_scripts_main_lua_has_priority_over_logicmods(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "ue4ss.zip",
        {
            "My Lua Mod/sCrIpTs/MaIn.LuA": b"print('ok')",
            "My Lua Mod/LogicMods/decoy.pak": b"pak",
        },
    )

    result = inspect_and_extract(archive, tmp_path / "out")

    assert result.kind is ModKind.UE4SS
    assert result.display_name == "My Lua Mod"
    assert result.content_root == (tmp_path / "out" / "My Lua Mod").resolve()
    assert result.groups == ()


def test_successful_extraction_stays_under_resolved_destination(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"Wrapper/Safe.pak": b"pak"})
    dest = tmp_path / "parent" / ".." / "out"

    result = inspect_and_extract(archive, dest)

    resolved_dest = dest.resolve()
    assert result.content_root.is_relative_to(resolved_dest)
    assert (resolved_dest / "Wrapper" / "Safe.pak").read_bytes() == b"pak"
    assert all(path.is_relative_to(resolved_dest) for group in result.groups for path in group)


@pytest.mark.parametrize(
    "name",
    [
        "mod:stream.pak",
        "bad<name.pak",
        'bad\"name.pak',
        "bad|name.pak",
        "bad?name.pak",
        "bad*name.pak",
        "Folder./mod.pak",
        "Folder /mod.pak",
        "CON.pak",
        "prn/readme.pak",
        "AUX.txt.pak",
        "nul/mod.pak",
        "COM1.pak",
        "com9.txt/mod.pak",
        "LPT1/mod.pak",
        "lpt9.bin.pak",
    ],
)
def test_rejects_win32_unsafe_path_components(tmp_path: Path, name: str) -> None:
    archive = make_zip(tmp_path / "unsafe.zip", {name: b"pak"})

    assert_rejected(archive, tmp_path / "out", "Windows|非法|路径")


def test_rejects_case_insensitive_normalized_path_collision(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "collision.zip", {"Mods/Cool.pak": b"a", "mods/cool.PAK": b"b"})

    assert_rejected(archive, tmp_path / "out", "碰撞|冲突")


def test_success_atomically_publishes_and_removes_private_staging(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"Safe.pak": b"pak"})
    dest = tmp_path / "out"

    result = inspect_and_extract(archive, dest)

    assert (dest / "Safe.pak").read_bytes() == b"pak"
    assert result.groups[0][0] == dest / "Safe.pak"
    assert not list(tmp_path.glob(".out.*.tmp"))


def test_detected_reparse_component_cleans_staging_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"Wrapper/Safe.pak": b"pak"})
    dest = tmp_path / "out"
    original = archive_utils._is_reparse_path

    def injected_reparse(path: Path) -> bool:
        if path.name == "Wrapper":
            return True
        return original(path)

    monkeypatch.setattr(archive_utils, "_is_reparse_path", injected_reparse)

    with pytest.raises(ValueError, match="重解析|链接|目标"):
        inspect_and_extract(archive, dest)

    assert not dest.exists()
    assert not list(tmp_path.glob(".out.*.tmp"))


def test_target_permission_error_is_not_reported_as_damaged_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"Safe.pak": b"pak"})
    original_open = Path.open

    def denied_open(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "xb" and self.name == "Safe.pak":
            raise PermissionError("denied by test")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)

    with pytest.raises(ValueError, match="目标") as captured:
        inspect_and_extract(archive, tmp_path / "out")

    assert "损坏" not in str(captured.value)
    assert isinstance(captured.value.__cause__, PermissionError)
    assert not list(tmp_path.glob(".out.*.tmp"))


def test_failure_after_first_real_write_removes_staging_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "broken.zip", {"first.pak": b"first", "second.txt": b"second"})
    original_open = zipfile.ZipFile.open
    observed_first_write = False

    class FailingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise RuntimeError("stream failed")

    def fail_second(self, name, mode="r", pwd=None, *, force_zip64=False):
        nonlocal observed_first_write
        filename = getattr(name, "filename", name)
        if mode == "r" and filename == "second.txt":
            staging = next(tmp_path.glob(".out.*.tmp"))
            observed_first_write = (staging / "first.pak").read_bytes() == b"first"
            return FailingStream(b"second")
        return original_open(self, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_second)
    dest = tmp_path / "out"

    with pytest.raises(RuntimeError, match="stream failed"):
        inspect_and_extract(archive, dest)

    assert observed_first_write
    assert not dest.exists()
    assert not list(tmp_path.glob(".out.*.tmp"))


def test_existing_destination_collision_is_reported_as_target_error(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"Safe.pak": b"pak"})
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(ValueError, match="目标目录已存在") as captured:
        inspect_and_extract(archive, dest)

    assert "损坏" not in str(captured.value)
