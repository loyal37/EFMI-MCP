"""The hash functions XXMI uses, for checking and for offline work on dumps.

crc32c  - resources and buffer regions (crc32c_hw in XXMI)
fnv64   - shaders (fnv_64_buf: FNV-1 with a zero start value)
"""
from __future__ import annotations

try:  # optional speed-up, results are identical
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


def _make_table() -> list[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_TABLE = _make_table()


def crc32c(data: bytes | bytearray | memoryview, seed: int = 0) -> int:
    """Standard CRC-32C (Castagnoli) with an incremental seed, like crc32c_hw()."""
    crc = seed ^ 0xFFFFFFFF
    table = _TABLE
    for b in bytes(data):
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


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
