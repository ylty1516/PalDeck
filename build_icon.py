"""Build multi-size app.ico from Imagine-generated artwork."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageFilter


def make_transparent_rounded(src: Image.Image) -> Image.Image:
    """Remove near-uniform corner background; keep the icon tile."""
    img = src.convert("RGBA")
    w, h = img.size
    px = img.load()

    # Sample corner colors for chroma key
    samples = [
        px[2, 2],
        px[w - 3, 2],
        px[2, h - 3],
        px[w - 3, h - 3],
        px[w // 2, 2],
        px[2, h // 2],
    ]
    # Average background
    br = sum(s[0] for s in samples) // len(samples)
    bg = sum(s[1] for s in samples) // len(samples)
    bb = sum(s[2] for s in samples) // len(samples)

    def dist(c):
        return abs(c[0] - br) + abs(c[1] - bg) + abs(c[2] - bb)

    # Threshold: outer gradient is purple-blue
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    opx = out.load()
    for y in range(h):
        for x in range(w):
            c = px[x, y]
            d = dist(c)
            # Soft edge
            if d < 45:
                opx[x, y] = (0, 0, 0, 0)
            elif d < 80:
                a = int(255 * (d - 45) / 35)
                opx[x, y] = (c[0], c[1], c[2], a)
            else:
                opx[x, y] = c

    # Crop to non-transparent bounding box with padding
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
        # pad to square
        side = max(out.size)
        pad = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        ox = (side - out.size[0]) // 2
        oy = (side - out.size[1]) // 2
        pad.paste(out, (ox, oy), out)
        out = pad

    # slight refine edges
    return out


def build_ico(source: Path, out_ico: Path) -> None:
    raw = Image.open(source).convert("RGBA")
    master = make_transparent_rounded(raw)
    # Upscale cleanly to 512 if needed
    if max(master.size) < 512:
        master = master.resize((512, 512), Image.Resampling.LANCZOS)
    else:
        master = master.resize((512, 512), Image.Resampling.LANCZOS)

    out_ico.parent.mkdir(parents=True, exist_ok=True)
    preview = out_ico.with_suffix(".png")
    master.save(preview, "PNG")

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [master.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
    images[0].save(
        out_ico,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"ICO: {out_ico}")
    print(f"PNG: {preview}")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    default_src = root / "assets" / "icon_source.png"
    # Prefer CLI arg, then known Imagine session images
    candidates = []
    if len(sys.argv) > 1:
        candidates.append(Path(sys.argv[1]))
    candidates.extend(
        [
            default_src,
            Path.home()
            / ".grok"
            / "sessions"
            / "C%3A%5CUsers%5Cyyx"
            / "019f4be5-1576-7ee1-ac8e-a89a30e494b4"
            / "images"
            / "2.jpg",
            Path.home()
            / ".grok"
            / "sessions"
            / "C%3A%5CUsers%5Cyyx"
            / "019f4be5-1576-7ee1-ac8e-a89a30e494b4"
            / "images"
            / "1.jpg",
        ]
    )
    # Also search recent images folders
    sess = Path.home() / ".grok" / "sessions"
    if sess.is_dir():
        for p in sorted(sess.glob("**/images/*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
            candidates.append(p)
        for p in sorted(sess.glob("**/images/*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
            candidates.append(p)

    src = next((p for p in candidates if p.is_file()), None)
    if not src:
        raise SystemExit("No source image found. Pass path: py -3 build_icon.py image.jpg")

    print("Source:", src)
    # Copy source into assets for archive
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    dest_src = assets / "icon_source.png"
    Image.open(src).convert("RGBA").save(dest_src, "PNG")
    build_ico(dest_src, assets / "app.ico")
