"""Offline pixel-equivalence checks for texture encoding; no game connection."""
from __future__ import annotations

import io
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import numpy as np
from PIL import Image

from efmi_mcp import decode


def pixels(png):
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        return image.mode, image.size, image.tobytes()


def reference_png(image, max_size=0):
    """Previous conversion's default PNG compression and resizing behavior."""
    if max_size and max(image.size) > max_size:
        scale = max_size / max(image.size)
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class TextureEncodingTests(unittest.TestCase):
    def assert_encodings(self, width, height, fmt, data, expected):
        with patch.object(decode, "_texture_to_image", wraps=decode._texture_to_image) as convert:
            full, small = decode.texture_to_pngs(width, height, fmt, data, preview_max_size=3)
            self.assertEqual(convert.call_count, 1)
        self.assertEqual(pixels(full), pixels(reference_png(expected)))
        self.assertEqual(pixels(small), pixels(reference_png(expected, 3)))
        self.assertEqual(pixels(decode.texture_to_png(width, height, fmt, data)), pixels(full))
        self.assertEqual(pixels(decode.texture_to_png(width, height, fmt, data, 3)), pixels(small))

    def test_rgba_color_and_alpha(self):
        width, height = 7, 5
        data = bytes((i * 37) % 256 for i in range(width * height * 4))
        self.assert_encodings(width, height, 28, data, Image.frombytes("RGBA", (width, height), data))

    def test_bc7_blocks(self):
        # Mode 6 is selected by bit 6 of each 128-bit block. Other bits define
        # endpoints, p-bits and indices, giving non-uniform deterministic pixels.
        data = b"".join(bytes([0x40]) + bytes((i + block * 17) % 256 for i in range(1, 16))
                        for block in range(4))
        expected = Image.open(io.BytesIO(decode.make_dds(8, 8, 99, data)))
        expected.load()
        self.assert_encodings(8, 8, 99, data, expected)

    def test_numeric_r32_float(self):
        values = np.array([i * 0.25 - 3 for i in range(24)], dtype=np.float32).reshape(4, 6)
        normalized = ((values - values.min()) / (values.max() - values.min()) * 255).astype(np.uint8)
        self.assert_encodings(6, 4, 41, values.tobytes(), Image.fromarray(normalized))

    def test_numeric_constant_image(self):
        self.assert_encodings(4, 4, 41, struct.pack("<16f", *([4.5] * 16)),
                              Image.new("L", (4, 4), 0))

    def test_preview_disabled_and_no_upscale(self):
        data = bytes(range(64))
        full, small = decode.texture_to_pngs(4, 4, 28, data, preview_max_size=0)
        self.assertIsNone(small)
        other, small = decode.texture_to_pngs(4, 4, 28, data, preview_max_size=512)
        self.assertEqual(full, other)
        self.assertEqual(other, small)
        self.assertEqual(pixels(small)[1], (4, 4))

    def test_unsupported_and_truncated_data(self):
        for fmt, data in ((0, b""), (28, b"\0"), (99, b"\0")):
            with self.subTest(format=fmt):
                self.assertIsNone(decode.texture_to_png(8, 8, fmt, data))
                self.assertEqual(decode.texture_to_pngs(8, 8, fmt, data), (None, None))


if __name__ == "__main__":
    unittest.main()
