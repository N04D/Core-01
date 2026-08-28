#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from plugins.media.image_overlay import apply_overlay, latest_image


class ImageOverlayTests(unittest.TestCase):
    def test_overlay_adds_border_and_caption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            output = root / "out.jpg"
            Image.new("RGB", (100, 80), (20, 30, 40)).save(source)
            apply_overlay(source, output, text="Ar-Rahman", border=10, font_size=18)
            with Image.open(output) as image:
                self.assertEqual(image.size, (120, 100))
                self.assertEqual(image.format, "JPEG")
                self.assertGreater(output.stat().st_size, 256)

    def test_latest_image_rejects_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                latest_image(Path(directory))


if __name__ == "__main__":
    unittest.main(verbosity=2)
