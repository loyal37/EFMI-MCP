"""Offline CRC32C regression tests; never connects to a game or probe."""
from __future__ import annotations

from array import array
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from efmi_mcp import hashes


def reference(data, seed=0):
    """Independent bit-at-a-time oracle, including legacy Python seed behavior."""
    crc = seed ^ 0xFFFFFFFF
    for value in bytes(data):
        # The byte-table implementation leaves high/negative seed bits intact.
        low = (crc ^ value) & 0xFF
        for _ in range(8):
            low = (low >> 1) ^ (0x82F63B78 if low & 1 else 0)
        crc = low ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


class CRC32CTests(unittest.TestCase):
    def test_standard_vectors_and_uint32_seeds(self):
        self.assertEqual(hashes.crc32c(b"123456789"), 0xE3069283)
        randomizer = random.Random(29091)
        for size in (0, 1, 2, 7, 8, 9, 15, 16, 31, 32, 255, 256, 1025, 65539):
            data = randomizer.randbytes(size)
            for seed in (0, 1, 0xFFFFFFFF, 0x80000000, randomizer.getrandbits(32)):
                with self.subTest(size=size, seed=seed):
                    self.assertEqual(hashes.crc32c(data, seed), reference(data, seed))

    def test_incremental_chunks(self):
        randomizer = random.Random(217)
        data = randomizer.randbytes(9001)
        for seed in (0, 0x12345678, 0xFFFFFFFF):
            expected = reference(data, seed)
            for chunk_size in (1, 7, 8, 63, 1024, len(data)):
                crc = seed
                for offset in range(0, len(data), chunk_size):
                    crc = hashes.crc32c(memoryview(data)[offset:offset + chunk_size], crc)
                self.assertEqual(crc, expected)

    def test_input_buffer_semantics(self):
        raw = bytes(range(256)) * 7
        inputs = (raw, bytearray(raw), memoryview(raw), memoryview(bytearray(raw)),
                  memoryview(raw)[3:-5], memoryview(raw)[::3], memoryview(raw)[::-1],
                  memoryview(array("I", (0, 1, 0xFFFFFFFF, 0x01020304))),
                  memoryview(raw).cast("B", shape=(7, 256)))
        for data in inputs:
            with self.subTest(type=type(data), format=getattr(data, "format", None)):
                self.assertEqual(hashes.crc32c(data, 0x12345678), reference(data, 0x12345678))

    def test_legacy_non_uint32_seeds(self):
        class CustomSeed(int):
            def __xor__(self, other):
                return int(self) + other

        for seed in (-1, -0x123456789ABCDEF, 1 << 32, 1 << 100, True, CustomSeed(7)):
            for data in (b"", b"a", bytes(range(21))):
                self.assertEqual(hashes.crc32c(data, seed), reference(data, seed))

    def test_python_fallback(self):
        with patch.object(hashes, "_NATIVE_CRC32C", None):
            self.test_standard_vectors_and_uint32_seeds()
            self.test_incremental_chunks()
            self.test_input_buffer_semantics()
            self.test_legacy_non_uint32_seeds()

    def test_missing_native_library(self):
        with patch.object(hashes.ctypes, "CDLL", side_effect=OSError("not installed")):
            self.assertIsNone(hashes._load_native_crc32c())

    def test_unsupported_cpu(self):
        with patch.object(hashes.ctypes, "CDLL") as library:
            library.return_value.efmi_crc32c_supported.return_value = 0
            self.assertIsNone(hashes._load_native_crc32c())
            library.return_value.efmi_crc32c.assert_not_called()

    def test_incompatible_native_library(self):
        with patch.object(hashes.ctypes, "CDLL") as library:
            library.return_value.efmi_crc32c_supported.return_value = 1
            library.return_value.efmi_crc32c.return_value = 0
            self.assertIsNone(hashes._load_native_crc32c())


if __name__ == "__main__":
    print("CRC32C backend:", "native SSE4.2" if hashes._NATIVE_CRC32C else "Python")
    unittest.main()
