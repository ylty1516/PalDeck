import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts import vendor_ue4ss_palworld as vendor


ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "third_party" / "ue4ss-palworld"
EXPECTED_FILES = {
    "dwmapi.dll",
    "ue4ss/UE4SS.dll",
    "ue4ss/UE4SS-settings.ini",
    "ue4ss/MemberVariableLayout.ini",
    "ue4ss/LICENSE",
}


def test_vendored_asset_matches_manifest_and_required_contents():
    manifest = json.loads((VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
    archive_path = VENDOR_DIR / manifest["asset"]

    assert manifest == {
        "source": vendor.URL,
        "repo": "https://github.com/Okaetsu/RE-UE4SS",
        "tag": "experimental-palworld",
        "asset": "UE4SS-Palworld.zip",
        "size": 6_982_837,
        "sha256": "7c80b2f4a29baf0f384552c8517e58196e78c8a1b8530637b7179eddae1b54a9",
        "updated_at": "2026-07-09T23:56:19Z",
    }
    assert archive_path.stat().st_size == manifest["size"]
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == manifest["sha256"]
    with zipfile.ZipFile(archive_path) as archive:
        assert EXPECTED_FILES <= set(archive.namelist())
        archived_license = archive.read("ue4ss/LICENSE")
        assert len(archived_license) == 1085
        assert b"\r\n" in archived_license
        assert (VENDOR_DIR / "LICENSE").read_bytes() == archived_license
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "third_party/ue4ss-palworld/LICENSE -text -diff" in attributes.splitlines()


def test_notice_credits_sources_and_contains_fixed_links():
    notice = (VENDOR_DIR / "NOTICE.md").read_text(encoding="utf-8")
    for text in (
        "Okaetsu",
        "UE4SS-RE",
        "Narknon",
        "MIT",
        "https://github.com/Okaetsu/RE-UE4SS",
        vendor.URL,
    ):
        assert text in notice


class ChunkedResponse:
    def __init__(self, payload: bytes):
        self.stream = BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(min(size, 3))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_streams_with_timeout_and_atomically_replaces(tmp_path):
    payload = b"streamed payload"
    destination = tmp_path / "asset.zip"
    destination.write_bytes(b"old")
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return ChunkedResponse(payload)

    vendor.download_asset(
        destination,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        opener=opener,
    )

    assert calls[0][1] == 60
    assert destination.read_bytes() == payload
    assert not destination.with_suffix(".zip.tmp").exists()


@pytest.mark.parametrize("payload, expected_size", [(b"too long", 3), (b"bad hash", 8)])
def test_download_rejects_invalid_content_and_removes_temporary_file(
    tmp_path, payload, expected_size
):
    destination = tmp_path / "asset.zip"

    with pytest.raises(ValueError):
        vendor.download_asset(
            destination,
            expected_size=expected_size,
            expected_sha256="0" * 64,
            opener=lambda _, **__: ChunkedResponse(payload),
        )

    assert not destination.exists()
    assert not destination.with_suffix(".zip.tmp").exists()


def test_download_rejects_short_response_and_preserves_existing_target(tmp_path):
    destination = tmp_path / "asset.zip"
    destination.write_bytes(b"existing")

    with pytest.raises(ValueError, match="size mismatch"):
        vendor.download_asset(
            destination,
            expected_size=10,
            expected_sha256="0" * 64,
            opener=lambda _, **__: ChunkedResponse(b"short"),
        )

    assert destination.read_bytes() == b"existing"
    assert not destination.with_suffix(".zip.tmp").exists()


def test_download_read_error_preserves_existing_target_and_removes_temp(tmp_path):
    class FailingResponse(ChunkedResponse):
        def read(self, size=-1):
            if self.stream.tell() >= 3:
                raise OSError("connection lost")
            return super().read(size)

    destination = tmp_path / "asset.zip"
    destination.write_bytes(b"existing")

    with pytest.raises(OSError, match="connection lost"):
        vendor.download_asset(
            destination,
            expected_size=10,
            expected_sha256="0" * 64,
            opener=lambda _, **__: FailingResponse(b"partial"),
        )

    assert destination.read_bytes() == b"existing"
    assert not destination.with_suffix(".zip.tmp").exists()
