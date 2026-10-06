"""The hash functions XXMI uses, for checking and for offline work on dumps.

crc32c  - resources and buffer regions (crc32c_hw in XXMI)
fnv64   - shaders (fnv_64_buf: FNV-1 with a zero start value)
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path


def _make_table() -> list[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_TABLE = _make_table()


def _crc32c_python(data: bytes | bytearray | memoryview, seed: int = 0) -> int:
    crc = seed ^ 0xFFFFFFFF
    table = _TABLE
    for b in bytes(data):
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _load_native_crc32c():
    """Optional game-external helper; missing/unsupported builds use Python."""
    if os.name != "nt":
        return None
    path = Path(__file__).resolve().parents[2] / "dist" / "efmi_crc32c.dll"
    try:
        # Use our absolute path, never a DLL discovered through PATH/CWD.
        library = ctypes.CDLL(str(path))
        supported = library.efmi_crc32c_supported
        supported.argtypes = []
        supported.restype = ctypes.c_int
        if not supported():
            return None
        function = library.efmi_crc32c
        function.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        # Reject an incompatible helper before using it on captured resources.
        for data, seed in ((b"123456789", 0), (b"\x00\xff\x80abc", 0x12345678),
                           (b"", 0xFFFFFFFF)):
            if function(data, len(data), seed) != _crc32c_python(data, seed):
                return None
        return function
    except (OSError, AttributeError):
        return None


_NATIVE_CRC32C = _load_native_crc32c()


def crc32c(data: bytes | bytearray | memoryview, seed: int = 0) -> int:
    """Standard CRC-32C with an incremental seed, accelerated when available.

    Arbitrary Python seeds keep the original behavior instead of being silently
    truncated to uint32. Only the normal uint32 seed domain uses the C helper.
    bytes() also preserves the old behavior for strided/typed memoryviews.
    """
    if _NATIVE_CRC32C is not None and type(seed) is int and 0 <= seed <= 0xFFFFFFFF:
        payload = bytes(data)
        return _NATIVE_CRC32C(payload, len(payload), seed)
    return _crc32c_python(data, seed)


def fnv64(data: bytes | bytearray | memoryview) -> int:
    """FNV-1, 64 bit, starting from zero (not from the usual offset basis)."""
    h = 0
    mask = 0xFFFFFFFFFFFFFFFF
    for b in bytes(data):
        h = ((h * 0x100000001B3) & mask) ^ b
    return h


def hex32(v: int) -> str:
    return f"{v & 0xFFFFFFFF:08x}"


def hex64(v: int) -> str:
    return f"{v & 0xFFFFFFFFFFFFFFFF:016x}"


def index_region(fmt: int, bind_offset: int, first_index: int, index_count: int) -> tuple[int, int]:
    """Byte range of an index buffer a draw reads (GetIndexBufferRegion*)."""
    stride = 4 if fmt == 42 else 2          # DXGI_FORMAT_R32_UINT == 42
    return bind_offset + stride * first_index, stride * index_count


def vertex_region(stride: int, bind_offset: int, first_vertex: int, vertex_count: int,
                  index_count: int) -> tuple[int, int]:
    """Byte range of a vertex buffer XXMI hashes for a draw (GetVertexBufferRegion*).

    For indexed draws the vertex count is unknown, so XXMI estimates it as
    0.15 * index_count + 3.
    """
    count = vertex_count if vertex_count > 0 else (3 * index_count + 10) // 20 + 3
    return bind_offset + stride * (first_vertex & 0xFFFFFFFF), stride * count
