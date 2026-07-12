import hashlib
import json
import sys
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
    def __init__(self, payload: bytes, url="https://github.com/Okaetsu/RE-UE4SS/releases/download/experimental-palworld/UE4SS-Palworld.zip"):
        self.stream = BytesIO(payload)
        self.url = url

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(min(size, 3))

    def geturl(self):
        return self.url

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


# Runtime provider trust-chain tests.
import backend.ue4ss_provider as ue4ss_provider
from backend.ue4ss_provider import Ue4ssAsset, Ue4ssProvider, _SafeRedirectHandler


def _make_bundle(root: Path, *, payloads=None, manifest_changes=None):
    directory = root / "third_party" / "ue4ss-palworld"
    directory.mkdir(parents=True)
    archive = directory / "UE4SS-Palworld.zip"
    payloads = payloads or {name: b"x" for name in EXPECTED_FILES}
    with zipfile.ZipFile(archive, "w") as output:
        for name, payload in payloads.items():
            output.writestr(name, payload)
    manifest = {
        "source": vendor.URL,
        "repo": vendor.REPO,
        "tag": vendor.TAG,
        "asset": vendor.ASSET,
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "updated_at": vendor.UPDATED_AT,
    }
    manifest.update(manifest_changes or {})
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return archive, manifest


def test_asset_is_immutable():
    asset = Ue4ssAsset("a", 1, "0" * 64, "now", "https://github.com/x")
    with pytest.raises((AttributeError, TypeError)):
        asset.size = 2


def test_bundled_zip_finds_development_resources_and_reports_status(tmp_path):
    archive, manifest = _make_bundle(tmp_path)
    provider = Ue4ssProvider(resource_root=tmp_path)
    assert provider.bundled_zip() == archive
    assert provider.bundled_status() == {
        "available": True,
        "asset": Ue4ssAsset(vendor.ASSET, manifest["size"], manifest["sha256"], vendor.UPDATED_AT, vendor.URL),
    }


def test_bundled_zip_finds_frozen_resources(tmp_path, monkeypatch):
    archive, _ = _make_bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert Ue4ssProvider().bundled_zip() == archive


@pytest.mark.parametrize(
    "change,error",
    [
        ({"extra": 1}, "schema"),
        ({"repo": "https://github.com/evil/repo"}, "repo"),
        ({"tag": "latest"}, "tag"),
        ({"asset": "other.zip"}, "asset"),
        ({"size": 8 * 1024 * 1024 + 1}, "size"),
        ({"sha256": "bad"}, "sha256"),
    ],
)
def test_bundled_zip_rejects_tampered_manifest(tmp_path, change, error):
    _make_bundle(tmp_path, manifest_changes=change)
    with pytest.raises(ValueError, match=error):
        Ue4ssProvider(resource_root=tmp_path).bundled_zip()


def test_bundled_zip_rejects_manifest_parse_and_read_failures(tmp_path, monkeypatch):
    _make_bundle(tmp_path)
    manifest_path = tmp_path / "third_party" / "ue4ss-palworld" / "manifest.json"
    manifest_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        Ue4ssProvider(resource_root=tmp_path).bundled_zip()

    original_read_text = Path.read_text
    def failing_read_text(path, *args, **kwargs):
        if path == manifest_path:
            raise OSError("manifest unreadable")
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", failing_read_text)
    with pytest.raises(ValueError, match="manifest"):
        Ue4ssProvider(resource_root=tmp_path).bundled_zip()


def test_bundled_zip_rejects_missing_unreadable_and_invalid_zip(tmp_path, monkeypatch):
    archive, manifest = _make_bundle(tmp_path)
    archive.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        Ue4ssProvider(resource_root=tmp_path).bundled_zip()

    archive, manifest = _make_bundle(tmp_path / "unreadable")
    original_open = Path.open
    def failing_open(path, *args, **kwargs):
        if path == archive:
            raise OSError("archive unreadable")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(ValueError, match="cannot read"):
        Ue4ssProvider(resource_root=tmp_path / "unreadable").bundled_zip()
    monkeypatch.setattr(Path, "open", original_open)

    bad_archive, bad_manifest = _make_bundle(tmp_path / "bad-zip")
    bad_archive.write_bytes(b"not a zip")
    bad_manifest["size"] = bad_archive.stat().st_size
    bad_manifest["sha256"] = hashlib.sha256(bad_archive.read_bytes()).hexdigest()
    (bad_archive.parent / "manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid bundled UE4SS ZIP"):
        Ue4ssProvider(resource_root=tmp_path / "bad-zip").bundled_zip()


def test_bundled_zip_rejects_changed_bytes_and_missing_required_entry(tmp_path):
    archive, _ = _make_bundle(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="size"):
        Ue4ssProvider(resource_root=tmp_path).bundled_zip()

    hash_root = tmp_path / "changed-hash"
    hash_archive, _ = _make_bundle(hash_root)
    changed = bytearray(hash_archive.read_bytes())
    changed[-1] ^= 1
    hash_archive.write_bytes(changed)
    with pytest.raises(ValueError, match="sha256"):
        Ue4ssProvider(resource_root=hash_root).bundled_zip()

    root2 = tmp_path / "second"
    _make_bundle(root2, payloads={"dwmapi.dll": b"x"})
    with pytest.raises(ValueError, match="required"):
        Ue4ssProvider(resource_root=root2).bundled_zip()


class JsonResponse(ChunkedResponse):
    pass


def _release(asset_overrides=None):
    asset = {
        "name": vendor.ASSET,
        "size": 20,
        "digest": "sha256:" + "a" * 64,
        "updated_at": "2026-07-10T00:00:00Z",
        "browser_download_url": vendor.URL,
    }
    asset.update(asset_overrides or {})
    return json.dumps({"assets": [asset]}).encode()


def test_check_upstream_uses_fixed_api_headers_timeout_and_compares_digest(tmp_path):
    _, manifest = _make_bundle(tmp_path)
    calls = []
    def opener(request, *, timeout):
        calls.append((request, timeout))
        return JsonResponse(_release())
    result = Ue4ssProvider(resource_root=tmp_path, opener=opener).check_upstream()
    assert result["asset"].sha256 == "a" * 64
    assert result["update_available"] is (manifest["sha256"] != "a" * 64)
    assert calls[0][0].full_url == "https://api.github.com/repos/Okaetsu/RE-UE4SS/releases/tags/experimental-palworld"
    assert calls[0][0].get_header("User-agent") == "PalDeck/2.1"
    assert calls[0][1] == 30


def test_check_upstream_reports_same_digest(tmp_path):
    _, manifest = _make_bundle(tmp_path)
    response = _release({"digest": "sha256:" + manifest["sha256"]})
    result = Ue4ssProvider(resource_root=tmp_path, opener=lambda *_a, **_k: JsonResponse(response)).check_upstream()
    assert result["update_available"] is False


@pytest.mark.parametrize("override,match", [
    ({"digest": None}, "digest"), ({"digest": "sha256:bad"}, "digest"),
    ({"name": "wrong.zip"}, "asset"), ({"size": 0}, "size"),
    ({"size": 8 * 1024 * 1024 + 1}, "size"),
    ({"browser_download_url": "https://evil.example/file.zip"}, "URL"),
    ({"browser_download_url": "http://github.com/file.zip"}, "URL"),
])
def test_check_upstream_rejects_untrusted_metadata(tmp_path, override, match):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(resource_root=tmp_path, opener=lambda *_a, **_k: JsonResponse(_release(override)))
    with pytest.raises(ValueError, match=match):
        provider.check_upstream()


@pytest.mark.parametrize("url", [
    "https://github.com/Okaetsu/RE-UE4SS/releases/download/wrong-tag/UE4SS-Palworld.zip",
    "https://github.com/Okaetsu/RE-UE4SS/releases/download/experimental-palworld/subdir/UE4SS-Palworld.zip",
    "https://github.com/Okaetsu/RE-UE4SS/releases/download/experimental-palworld/other.zip",
])
def test_check_upstream_rejects_same_host_wrong_release_url(tmp_path, url):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(
        resource_root=tmp_path,
        opener=lambda *_a, **_k: JsonResponse(_release({"browser_download_url": url})),
    )
    with pytest.raises(ValueError, match="URL"):
        provider.check_upstream()


def test_check_upstream_rejects_oversized_invalid_duplicate_and_missing_updated_at(tmp_path):
    _make_bundle(tmp_path)
    cases = [
        b"x" * (1024 * 1024 + 1),
        b"not-json",
        json.dumps({"assets": [json.loads(_release())["assets"][0]] * 2}).encode(),
        _release({"updated_at": ""}),
    ]
    matches = ["too large", "release response", "uniquely", "updated_at"]
    for payload, match in zip(cases, matches):
        provider = Ue4ssProvider(
            resource_root=tmp_path,
            opener=lambda *_a, payload=payload, **_k: JsonResponse(payload),
        )
        with pytest.raises(ValueError, match=match):
            provider.check_upstream()


def test_check_upstream_rejects_untrusted_final_response_host(tmp_path):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(
        resource_root=tmp_path,
        opener=lambda *_a, **_k: JsonResponse(_release(), "https://evil.example/release"),
    )
    with pytest.raises(ValueError, match="response URL"):
        provider.check_upstream()


def test_redirect_handler_rejects_http_and_unapproved_hosts():
    handler = _SafeRedirectHandler()
    with pytest.raises(ValueError, match="redirect"):
        handler.redirect_request(None, None, 302, "", {}, "http://github.com/x")
    with pytest.raises(ValueError, match="redirect"):
        handler.redirect_request(None, None, 302, "", {}, "https://evil.example/x")


def _checked_asset(provider, payload, *, url=vendor.URL):
    response = _release({"size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "browser_download_url": url})
    provider._opener = lambda *_a, **_k: JsonResponse(response)
    return provider.check_upstream()["asset"]


def test_download_verified_requires_provider_constructed_asset(tmp_path):
    provider = Ue4ssProvider(resource_root=tmp_path, opener=lambda *_a, **_k: None)
    asset = Ue4ssAsset(vendor.ASSET, 1, hashlib.sha256(b"x").hexdigest(), "now", vendor.URL)
    with pytest.raises(ValueError, match="check_upstream"):
        provider.download_verified(asset, tmp_path / "x.zip")


def test_download_verified_streams_and_replaces_atomically(tmp_path):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(resource_root=tmp_path)
    payload = b"downloaded"
    asset = _checked_asset(provider, payload)
    target = tmp_path / "target.zip"
    target.write_bytes(b"old")
    provider._opener = lambda *_a, **_k: ChunkedResponse(payload)
    assert provider.download_verified(asset, target) == target
    assert target.read_bytes() == payload
    assert not target.with_name(target.name + ".tmp").exists()


def test_download_verified_write_and_replace_failures_clean_temp_and_preserve_target(
    tmp_path, monkeypatch
):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(resource_root=tmp_path)
    payload = b"expected"
    asset = _checked_asset(provider, payload)
    target = tmp_path / "target.zip"
    temporary = target.with_name(target.name + ".tmp")
    target.write_bytes(b"old")
    provider._opener = lambda *_a, **_k: ChunkedResponse(payload)

    original_open = Path.open
    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, _chunk):
            raise OSError("disk full")

    def failing_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return FailingWriter(stream) if path == temporary else stream
    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="disk full"):
        provider.download_verified(asset, target)
    assert target.read_bytes() == b"old"
    assert not temporary.exists()

    monkeypatch.setattr(Path, "open", original_open)
    monkeypatch.setattr(ue4ss_provider.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        provider.download_verified(asset, target)
    assert target.read_bytes() == b"old"
    assert not temporary.exists()


@pytest.mark.parametrize("kind", ["short", "long", "digest", "read-error", "bad-host"])
def test_download_verified_failures_clean_temp_and_preserve_target(tmp_path, kind):
    _make_bundle(tmp_path)
    provider = Ue4ssProvider(resource_root=tmp_path)
    expected = b"expected"
    asset = _checked_asset(provider, expected)
    target = tmp_path / "target.zip"
    target.write_bytes(b"old")
    if kind == "short": response = ChunkedResponse(expected[:-1])
    elif kind == "long": response = ChunkedResponse(expected + b"x")
    elif kind == "digest": response = ChunkedResponse(b"X" * len(expected))
    elif kind == "bad-host": response = ChunkedResponse(expected, "https://evil.example/x")
    else:
        class Broken(ChunkedResponse):
            def read(self, size=-1):
                raise OSError("lost")
        response = Broken(expected)
    provider._opener = lambda *_a, **_k: response
    with pytest.raises((ValueError, OSError)):
        provider.download_verified(asset, target)
    assert target.read_bytes() == b"old"
    assert not target.with_name(target.name + ".tmp").exists()
