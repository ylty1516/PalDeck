"""Download and audit the pinned Palworld UE4SS release asset."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

URL = "https://github.com/Okaetsu/RE-UE4SS/releases/download/experimental-palworld/UE4SS-Palworld.zip"
REPO = "https://github.com/Okaetsu/RE-UE4SS"
TAG = "experimental-palworld"
ASSET = "UE4SS-Palworld.zip"
SIZE = 6_982_837
SHA256 = "7c80b2f4a29baf0f384552c8517e58196e78c8a1b8530637b7179eddae1b54a9"
UPDATED_AT = "2026-07-09T23:56:19Z"
REQUIRED_FILES = {
    "dwmapi.dll",
    "ue4ss/UE4SS.dll",
    "ue4ss/UE4SS-settings.ini",
    "ue4ss/MemberVariableLayout.ini",
    "ue4ss/LICENSE",
}
ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "third_party" / "ue4ss-palworld"


def download_asset(
    destination: Path,
    *,
    expected_size: int = SIZE,
    expected_sha256: str = SHA256,
    opener: Callable = urllib.request.urlopen,
) -> None:
    """Stream a fixed-size asset to a temporary file and replace atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "PalDeck-vendor-script"})
        with opener(request, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > expected_size:
                    raise ValueError(f"asset exceeds fixed size {expected_size}")
                digest.update(chunk)
                output.write(chunk)
        if total != expected_size:
            raise ValueError(f"asset size mismatch: expected {expected_size}, got {total}")
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"asset SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def vendor() -> None:
    archive_path = VENDOR_DIR / ASSET
    download_asset(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        missing = REQUIRED_FILES - set(archive.namelist())
        if missing:
            archive_path.unlink(missing_ok=True)
            raise ValueError(f"asset is missing required files: {sorted(missing)}")
        license_bytes = archive.read("ue4ss/LICENSE")

    manifest = {
        "source": URL,
        "repo": REPO,
        "tag": TAG,
        "asset": ASSET,
        "size": SIZE,
        "sha256": SHA256,
        "updated_at": UPDATED_AT,
    }
    (VENDOR_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (VENDOR_DIR / "LICENSE").write_bytes(license_bytes)
    notice = (
        "# Third-party notice\n\n"
        "Thanks to Okaetsu for maintaining the Palworld release, and to the "
        "UE4SS-RE upstream contributors. UE4SS is distributed under the MIT "
        "License; the copyright holder named by the bundled license is Narknon.\n\n"
        f"- Repository: {REPO}\n"
        f"- Pinned Palworld release asset: {URL}\n"
    )
    (VENDOR_DIR / "NOTICE.md").write_text(notice, encoding="utf-8")


if __name__ == "__main__":
    vendor()
