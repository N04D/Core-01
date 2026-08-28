#!/usr/bin/env python3
"""Apply publication-safe fine-art framing and typography to NightCafe images."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "vault" / "media" / "nightcafe"
PUBLICATION_DIR = ROOT / "vault" / "media" / "published"
SUPPORTED = {".png", ".jpg", ".jpeg", ".webp"}


def latest_image(directory: Path = SOURCE_DIR) -> Path:
    images = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED]
    if not images:
        raise FileNotFoundError(f"no supported images found in {directory}")
    return max(images, key=lambda p: p.stat().st_mtime)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if os.path.isfile(candidate):
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def apply_overlay(input_path: Path, output_path: Path, *, text: str = "", border: int = 24,
                  font_size: int = 34, text_color: str = "#f7f2e8", border_color: str = "#b79b68") -> Path:
    """Render one image with a restrained border and optional caption."""
    if border < 0 or font_size < 8:
        raise ValueError("border must be non-negative and font_size at least 8")
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if input_path.suffix.lower() not in SUPPORTED:
        raise ValueError(f"unsupported image extension: {input_path.suffix}")
    with Image.open(input_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    if border:
        image = ImageOps.expand(image, border=border, fill=border_color)
    if text.strip():
        image = image.convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")
        font = _font(font_size)
        bbox = draw.textbbox((0, 0), text.strip(), font=font)
        band_height = (bbox[3] - bbox[1]) + max(20, font_size // 2)
        draw.rectangle((0, image.height - band_height, image.width, image.height), fill=(0, 0, 0, 120))
        draw.text((border + 12, image.height - band_height + (band_height - (bbox[3] - bbox[1])) // 2 - bbox[1]),
                  text.strip(), font=font, fill=text_color)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        image.convert("RGB").save(output_path, quality=95)
    else:
        image.convert("RGB").save(output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Source image; defaults to the newest NightCafe image.")
    parser.add_argument("--output", type=Path, default=PUBLICATION_DIR / "nightcafe_overlay.jpg")
    parser.add_argument("--text", default="", help="Caption/title, e.g. an artwork name or one of the 99 names.")
    parser.add_argument("--border", type=int, default=24)
    parser.add_argument("--font-size", type=int, default=34)
    parser.add_argument("--border-color", default="#b79b68")
    parser.add_argument("--text-color", default="#f7f2e8")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve() if args.input else latest_image()
    output = apply_overlay(source, args.output, text=args.text, border=args.border,
                           font_size=args.font_size, border_color=args.border_color,
                           text_color=args.text_color)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
