"""Trusted access to the bundled and upstream Palworld UE4SS archive."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

API_URL = "https://api.github.com/repos/Okaetsu/RE-UE4SS/releases/tags/experimental-palworld"
REPO = "https://github.com/Okaetsu/RE-UE4SS"
TAG = "experimental-palworld"
ASSET_NAME = "UE4SS-Palworld.zip"
DOWNLOAD_PREFIX = f"{REPO}/releases/download/"
MAX_ASSET_SIZE = 8 * 1024 * 1024
# urllib applies this socket timeout to both connection establishment and reads.
HTTP_CONNECT_READ_TIMEOUT = 30
USER_AGENT = "PalDeck/2.1"
ALLOWED_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
REQUIRED_FILES = {
    "dwmapi.dll",
    "ue4ss/UE4SS.dll",
    "ue4ss/UE4SS-settings.ini",
    "ue4ss/MemberVariableLayout.ini",
    "ue4ss/LICENSE",
}
_MANIFEST_KEYS = {"source", "repo", "tag", "asset", "size", "sha256", "updated_at"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class Ue4ssAsset:
    name: str
    size: int
    sha256: str
    updated_at: str
    download_url: str


def _validate_https_url(url: str, *, context: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"untrusted {context} URL")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError(f"untrusted {context} URL")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib sends a request to an untrusted origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_https_url(newurl, context="redirect")
        except ValueError as error:
            raise ValueError("untrusted redirect URL") from error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(request, *, timeout):
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


class Ue4ssProvider:
    def __init__(
        self,
        *,
        resource_root: str | Path | None = None,
        opener: Callable | None = None,
    ) -> None:
        self._resource_root = Path(resource_root) if resource_root is not None else None
        self._opener = opener or _default_opener
        self._issued_assets: list[Ue4ssAsset] = []

    def _root(self) -> Path:
        if self._resource_root is not None:
            return self._resource_root
        if getattr(sys, "frozen", False):
            bundle = getattr(sys, "_MEIPASS", None)
            return Path(bundle) if bundle else Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[1]

    def _bundle_paths(self) -> tuple[Path, Path]:
        directory = self._root() / "third_party" / "ue4ss-palworld"
        return directory / "manifest.json", directory / ASSET_NAME

    def _read_manifest(self) -> dict[str, object]:
        manifest_path, _ = self._bundle_paths()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid UE4SS manifest") from error
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
            raise ValueError("invalid UE4SS manifest schema")
        fixed = {
            "source": f"{DOWNLOAD_PREFIX}{TAG}/{ASSET_NAME}",
            "repo": REPO,
            "tag": TAG,
            "asset": ASSET_NAME,
        }
        for key, expected in fixed.items():
            if manifest[key] != expected:
                raise ValueError(f"invalid manifest {key}")
        size = manifest["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ASSET_SIZE:
            raise ValueError("invalid manifest size")
        digest = manifest["sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("invalid manifest sha256")
        if not isinstance(manifest["updated_at"], str) or not manifest["updated_at"]:
            raise ValueError("invalid manifest updated_at")
        return manifest

    def bundled_zip(self) -> Path:
        manifest = self._read_manifest()
        _, archive_path = self._bundle_paths()
        try:
            actual_size = archive_path.stat().st_size
        except OSError as error:
            raise ValueError("bundled UE4SS archive is unavailable") from error
        if actual_size != manifest["size"]:
            raise ValueError("bundled UE4SS size mismatch")
        digest = hashlib.sha256()
        try:
            with archive_path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise ValueError("cannot read bundled UE4SS archive") from error
        if not hmac.compare_digest(digest.hexdigest(), str(manifest["sha256"])):
            raise ValueError("bundled UE4SS sha256 mismatch")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                missing = REQUIRED_FILES - set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("invalid bundled UE4SS ZIP") from error
        if missing:
            raise ValueError(f"bundled UE4SS ZIP is missing required entries: {sorted(missing)}")
        return archive_path

    def bundled_status(self) -> dict[str, object]:
        try:
            self.bundled_zip()
            manifest = self._read_manifest()
        except ValueError as error:
            return {"available": False, "error": str(error)}
        return {
            "available": True,
            "asset": Ue4ssAsset(
                ASSET_NAME,
                int(manifest["size"]),
                str(manifest["sha256"]),
                str(manifest["updated_at"]),
                str(manifest["source"]),
            ),
        }

    def _request(self, url: str):
        _validate_https_url(url, context="request")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        response = self._opener(request, timeout=HTTP_CONNECT_READ_TIMEOUT)
        final_url = response.geturl() if hasattr(response, "geturl") else url
        try:
            _validate_https_url(final_url, context="response")
        except Exception:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise
        return response

    def check_upstream(self) -> dict[str, object]:
        chunks: list[bytes] = []
        total = 0
        with self._request(API_URL) as response:
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > 1024 * 1024:
                    raise ValueError("GitHub release response is too large")
                chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            release = json.loads(raw)
            assets = release["assets"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError("invalid GitHub release response") from error
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == ASSET_NAME]
        if len(matches) != 1:
            raise ValueError("fixed UE4SS asset was not found uniquely")
        metadata = matches[0]
        digest_value = metadata.get("digest")
        if not isinstance(digest_value, str) or not digest_value.startswith("sha256:"):
            raise ValueError("invalid GitHub asset digest")
        digest = digest_value.removeprefix("sha256:")
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("invalid GitHub asset digest")
        size = metadata.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ASSET_SIZE:
            raise ValueError("invalid GitHub asset size")
        url = metadata.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX):
            raise ValueError("invalid GitHub asset URL")
        _validate_https_url(url, context="asset")
        updated_at = metadata.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("invalid GitHub asset updated_at")
        asset = Ue4ssAsset(ASSET_NAME, size, digest, updated_at, url)
        self._issued_assets.append(asset)
        bundled_digest = str(self._read_manifest()["sha256"])
        return {"asset": asset, "update_available": not hmac.compare_digest(digest, bundled_digest)}

    def download_verified(self, asset: Ue4ssAsset, destination: str | Path) -> Path:
        if not any(asset is issued for issued in self._issued_assets):
            raise ValueError("asset must be returned by check_upstream")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            with self._request(asset.download_url) as response, temporary.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > asset.size:
                        raise ValueError("download exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
            if total != asset.size:
                raise ValueError(f"download size mismatch: expected {asset.size}, got {total}")
            if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
                raise ValueError("download sha256 mismatch")
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination
