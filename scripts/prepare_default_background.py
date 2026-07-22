from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def prepare(source: Path, target: Path) -> None:
    """Crop the monitor overlay from the source and create the bundled WebP."""
    with Image.open(source) as image:
        image = image.convert("RGB")
        crop_top = max(64, round(image.height * 0.055))
        image = image.crop((0, crop_top, image.width, image.height))
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=90, method=6)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare PalDeck's bundled background")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.target)


if __name__ == "__main__":
    main()
