"""Compare captured UI screenshots with manually approved PNG baselines."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

# Pillow is reproducibly pinned by requirements-lock.txt (currently 12.3.0).
VIEWS = ("mods", "import", "nexus", "settings", "credits")
SIZES = ("1600x1000", "1280x820", "960x640")
IMAGE_NAME = re.compile(r"^(mods|import|nexus|settings|credits)-(1600x1000|1280x820|960x640)\.png$")


def normalized(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    return background.convert("RGB")


def expected_size(path: Path) -> tuple[int, int] | None:
    match = IMAGE_NAME.fullmatch(path.name)
    if not match:
        return None
    width, height = match.group(2).split("x")
    return int(width), int(height)


def has_content(image: Image.Image) -> bool:
    rgb = normalized(image)
    flat = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    return ImageChops.difference(rgb, flat).getbbox() is not None


def normalized_difference(candidate: Image.Image, baseline: Image.Image) -> float:
    difference = ImageChops.difference(normalized(candidate), normalized(baseline))
    channel_means = ImageStat.Stat(difference).mean
    return sum(channel_means) / (len(channel_means) * 255.0)


def compare(
    candidate_dir: Path, baseline_dir: Path, tolerance: float, allow_missing: bool,
    views: tuple[str, ...], sizes: tuple[str, ...],
) -> int:
    candidate_paths = sorted(candidate_dir.glob("*.png")) if candidate_dir.is_dir() else []
    if not candidate_paths:
        print(f"No candidate screenshots found: {candidate_dir}", file=sys.stderr)
        return 2

    expected_names = {f"{view}-{size}.png" for view in views for size in sizes}
    present_names = {path.name for path in candidate_paths}
    failures = [f"missing candidate screenshot: {name}" for name in sorted(expected_names - present_names)]
    failures.extend(f"unexpected screenshot name: {name}" for name in sorted(present_names - expected_names))
    candidates: list[tuple[Path, tuple[int, int], Image.Image]] = []
    for candidate_path in candidate_paths:
        if candidate_path.name not in expected_names:
            continue
        size = expected_size(candidate_path)
        if size is None:
            failures.append(f"invalid screenshot name: {candidate_path.name}")
            continue
        try:
            with Image.open(candidate_path) as source:
                if source.format != "PNG":
                    raise ValueError(f"expected PNG, got {source.format or 'unknown'}")
                candidate = source.copy()
        except (OSError, ValueError) as error:
            failures.append(f"invalid PNG {candidate_path.name}: {error}")
            continue
        if candidate.size != size:
            failures.append(f"candidate dimension mismatch for {candidate_path.name}: {candidate.size} != {size}")
            continue
        if not has_content(candidate):
            failures.append(f"candidate has empty content bounds: {candidate_path.name}")
            continue
        candidates.append((candidate_path, size, candidate))

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1

    if not baseline_dir.is_dir():
        print(f"Baseline needs approval: {baseline_dir}", file=sys.stderr)
        return 0 if allow_missing else 2

    for candidate_path, size, candidate in candidates:
        baseline_path = baseline_dir / candidate_path.name
        if not baseline_path.is_file():
            message = f"Baseline needs approval: {baseline_path}"
            if allow_missing:
                print(message)
                continue
            failures.append(message)
            continue
        try:
            with Image.open(baseline_path) as source:
                if source.format != "PNG":
                    raise ValueError(f"expected PNG, got {source.format or 'unknown'}")
                baseline = source.copy()
        except (OSError, ValueError) as error:
            failures.append(f"invalid PNG {baseline_path.name}: {error}")
            continue
        if baseline.size != size:
            failures.append(f"baseline dimension mismatch for {baseline_path.name}: {baseline.size} != {size}")
            continue
        if not has_content(baseline):
            failures.append(f"baseline has empty content bounds: {baseline_path.name}")
            continue
        difference = normalized_difference(candidate, baseline)
        print(f"{candidate_path.name}: normalized difference={difference:.8f}")
        if difference > tolerance:
            failures.append(f"difference exceeds tolerance for {candidate_path.name}: {difference:.8f} > {tolerance:.8f}")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("baseline_dir", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.0, help="maximum normalized mean absolute difference (0..1)")
    parser.add_argument("--allow-missing-baseline", action="store_true", help="report missing baselines without failing; never creates them")
    parser.add_argument("--views", nargs="+", choices=VIEWS, default=list(VIEWS))
    parser.add_argument("--sizes", nargs="+", choices=SIZES, default=list(SIZES))
    args = parser.parse_args()
    if not 0.0 <= args.tolerance <= 1.0:
        parser.error("--tolerance must be between 0 and 1")
    views = tuple(dict.fromkeys(args.views))
    sizes = tuple(dict.fromkeys(args.sizes))
    return compare(args.candidate_dir, args.baseline_dir, args.tolerance, args.allow_missing_baseline, views, sizes)


if __name__ == "__main__":
    raise SystemExit(main())
