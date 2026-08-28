#!/usr/bin/env python3
"""Apply publication-safe fine-art framing and typography to NightCafe images."""

from __future__ import annotations

import argparse
import os
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.database import connect_database

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
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (image.width - text_width) // 2 - bbox[0]
        text_y = (image.height - text_height) // 2 - bbox[1]
        # Centered title: no opaque banner, only a restrained shadow for contrast.
        draw.text((text_x + 3, text_y + 3), text.strip(), font=font, fill=(0, 0, 0, 150))
        draw.text((text_x, text_y), text.strip(), font=font, fill=text_color)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        image.convert("RGB").save(output_path, quality=95)
    else:
        image.convert("RGB").save(output_path)
    return output_path


def register_overlay(db_path: Path, path: Path, text: str) -> int:
    """Register the finalized asset so dashboard Media Store can find it."""
    with connect_database(db_path) as db:
        db.execute("""INSERT INTO media_sources(source_key,display_name,provider,root_path,config,is_active,last_indexed_at)
                      VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP)
                      ON CONFLICT(source_key) DO UPDATE SET root_path=excluded.root_path,is_active=1,last_indexed_at=CURRENT_TIMESTAMP""",
                    ("vault-overlays", "Vault Image Overlays", "image_overlay", str(path.parent), "{}"))
        source_id = int(db.execute("SELECT id FROM media_sources WHERE source_key='vault-overlays'").fetchone()[0])
        external_id = hashlib.sha256(path.read_bytes()).hexdigest()
        db.execute("""INSERT INTO media_assets(source_id,external_id,filename,file_path,thumbnail_path,media_type,mime_type,file_size,modified_at,prompt,metadata,is_available,indexed_at)
                      VALUES (?,?,?,?,?,'image','image/jpeg',?,?,?,?,1,CURRENT_TIMESTAMP)
                      ON CONFLICT(source_id,external_id) DO UPDATE SET file_path=excluded.file_path,file_size=excluded.file_size,modified_at=excluded.modified_at,metadata=excluded.metadata,is_available=1,indexed_at=CURRENT_TIMESTAMP""",
                   (source_id, external_id, path.name, str(path), str(path), path.stat().st_size,
                    datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), text,
                    json.dumps({"overlay": True, "caption": text}, ensure_ascii=False)))
        db.commit()
        return int(db.execute("SELECT id FROM media_assets WHERE source_id=? AND external_id=?", (source_id, external_id)).fetchone()[0])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Source image; defaults to the newest NightCafe image.")
    parser.add_argument("--output", type=Path, default=PUBLICATION_DIR / "nightcafe_overlay.jpg")
    parser.add_argument("--db", type=Path, help="Optional SQLite database for Media Store registration.")
    parser.add_argument("--text", default="", help="Caption/title, e.g. an artwork name or one of the 99 names.")
    parser.add_argument("--border", type=int, default=24)
    parser.add_argument("--font-size", type=int, default=64)
    parser.add_argument("--border-color", default="#b79b68")
    parser.add_argument("--text-color", default="#f7f2e8")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve() if args.input else latest_image()
    output = apply_overlay(source, args.output, text=args.text, border=args.border,
                           font_size=args.font_size, border_color=args.border_color,
                           text_color=args.text_color)
    result = {"status": "COMPLETED", "output": str(output)}
    if args.db:
        result["asset_id"] = register_overlay(args.db.expanduser().resolve(), output, args.text)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
