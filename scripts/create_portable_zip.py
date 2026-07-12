"""Create a deterministic portable ZIP from a prepared directory."""

from __future__ import annotations

import argparse
import os
import time
import zipfile
from pathlib import Path


def create_zip(source: Path, destination: Path, epoch: int) -> None:
    timestamp = time.gmtime(max(epoch, 315532800))[:6]
    files = sorted(path for path in source.rglob("*") if path.is_file())
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(f"{source.name}/{relative}", date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.flag_bits = 0x800
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args()
    create_zip(args.source.resolve(), args.destination.resolve(), args.epoch)


if __name__ == "__main__":
    main()
