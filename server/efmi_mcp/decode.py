"""Turning raw buffer and texture bytes into something readable."""
from __future__ import annotations

import io
import struct

# DXGI_FORMAT -> (struct code per component, component count, normalisation)
#   norm: None raw, "unorm" / "snorm" scaled to [0,1] / [-1,1]
_VERTEX_FORMATS: dict[int, tuple[str, int, str | None]] = {
    2: ("f", 4, None), 3: ("I", 4, None), 4: ("i", 4, None),
    6: ("f", 3, None), 7: ("I", 3, None), 8: ("i", 3, None),
    10: ("e", 4, None), 11: ("H", 4, "unorm"), 12: ("H", 4, None), 13: ("h", 4, "snorm"), 14: ("h", 4, None),
    16: ("f", 2, None), 17: ("I", 2, None), 18: ("i", 2, None),
    28: ("B", 4, "unorm"), 29: ("B", 4, "unorm"), 30: ("B", 4, None), 31: ("b", 4, "snorm"), 32: ("b", 4, None),
    34: ("e", 2, None), 35: ("H", 2, "unorm"), 36: ("H", 2, None), 37: ("h", 2, "snorm"), 38: ("h", 2, None),
    41: ("f", 1, None), 42: ("I", 1, None), 43: ("i", 1, None),
    49: ("B", 2, "unorm"), 50: ("B", 2, None), 51: ("b", 2, "snorm"), 52: ("b", 2, None),
    54: ("e", 1, None), 56: ("H", 1, "unorm"), 57: ("H", 1, None), 58: ("h", 1, "snorm"), 59: ("h", 1, None),
    61: ("B", 1, "unorm"), 62: ("B", 1, None), 63: ("b", 1, "snorm"), 64: ("b", 1, None),
    87: ("B", 4, "unorm"),
}
_NORM = {("B", "unorm"): 255.0, ("H", "unorm"): 65535.0, ("b", "snorm"): 127.0, ("h", "snorm"): 32767.0}

FORMAT_NAMES = [
    "UNKNOWN", "R32G32B32A32_TYPELESS", "R32G32B32A32_FLOAT", "R32G32B32A32_UINT", "R32G32B32A32_SINT",
    "R32G32B32_TYPELESS", "R32G32B32_FLOAT", "R32G32B32_UINT", "R32G32B32_SINT",
    "R16G16B16A16_TYPELESS", "R16G16B16A16_FLOAT", "R16G16B16A16_UNORM", "R16G16B16A16_UINT", "R16G16B16A16_SNORM", "R16G16B16A16_SINT",
    "R32G32_TYPELESS", "R32G32_FLOAT", "R32G32_UINT", "R32G32_SINT",
    "R32G8X24_TYPELESS", "D32_FLOAT_S8X24_UINT", "R32_FLOAT_X8X24_TYPELESS", "X32_TYPELESS_G8X24_UINT",
    "R10G10B10A2_TYPELESS", "R10G10B10A2_UNORM", "R10G10B10A2_UINT", "R11G11B10_FLOAT",
    "R8G8B8A8_TYPELESS", "R8G8B8A8_UNORM", "R8G8B8A8_UNORM_SRGB", "R8G8B8A8_UINT", "R8G8B8A8_SNORM", "R8G8B8A8_SINT",
    "R16G16_TYPELESS", "R16G16_FLOAT", "R16G16_UNORM", "R16G16_UINT", "R16G16_SNORM", "R16G16_SINT",
    "R32_TYPELESS", "D32_FLOAT", "R32_FLOAT", "R32_UINT", "R32_SINT",
    "R24G8_TYPELESS", "D24_UNORM_S8_UINT", "R24_UNORM_X8_TYPELESS", "X24_TYPELESS_G8_UINT",
    "R8G8_TYPELESS", "R8G8_UNORM", "R8G8_UINT", "R8G8_SNORM", "R8G8_SINT",
    "R16_TYPELESS", "R16_FLOAT", "D16_UNORM", "R16_UNORM", "R16_UINT", "R16_SNORM", "R16_SINT",
    "R8_TYPELESS", "R8_UNORM", "R8_UINT", "R8_SNORM", "R8_SINT", "A8_UNORM", "R1_UNORM",
    "R9G9B9E5_SHAREDEXP", "R8G8_B8G8_UNORM", "G8R8_G8B8_UNORM",
    "BC1_TYPELESS", "BC1_UNORM", "BC1_UNORM_SRGB", "BC2_TYPELESS", "BC2_UNORM", "BC2_UNORM_SRGB",
    "BC3_TYPELESS", "BC3_UNORM", "BC3_UNORM_SRGB", "BC4_TYPELESS", "BC4_UNORM", "BC4_SNORM",
    "BC5_TYPELESS", "BC5_UNORM", "BC5_SNORM", "B5G6R5_UNORM", "B5G5R5A1_UNORM",
    "B8G8R8A8_UNORM", "B8G8R8X8_UNORM", "R10G10B10_XR_BIAS_A2_UNORM",
    "B8G8R8A8_TYPELESS", "B8G8R8A8_UNORM_SRGB", "B8G8R8X8_TYPELESS", "B8G8R8X8_UNORM_SRGB",
    "BC6H_TYPELESS", "BC6H_UF16", "BC6H_SF16", "BC7_TYPELESS", "BC7_UNORM", "BC7_UNORM_SRGB",
]


def format_name(fmt: int) -> str:
    return FORMAT_NAMES[fmt] if 0 <= fmt < len(FORMAT_NAMES) else f"FORMAT_{fmt}"


def element_size(fmt: int) -> int:
    spec = _VERTEX_FORMATS.get(fmt)
    if not spec:
        return {24: 4, 26: 4}.get(fmt, 0)
    code, n, _ = spec
    return struct.calcsize("<" + code) * n


def decode_element(data: bytes, offset: int, fmt: int) -> tuple | None:
    """One vertex element at `offset`, or None if the format is not handled."""
    if fmt == 24:  # R10G10B10A2_UNORM
        (v,) = struct.unpack_from("<I", data, offset)
        return (round((v & 1023) / 1023.0, 5), round(((v >> 10) & 1023) / 1023.0, 5),
                round(((v >> 20) & 1023) / 1023.0, 5), round((v >> 30) / 3.0, 5))
    spec = _VERTEX_FORMATS.get(fmt)
    if not spec:
        return None
    code, n, norm = spec
    if offset + struct.calcsize("<" + code) * n > len(data):
        return None
    vals = struct.unpack_from(f"<{n}{code}", data, offset)
    if norm:
        scale = _NORM[(code, norm)]
        vals = tuple(round(max(v / scale, -1.0), 5) for v in vals)
    elif code in ("f", "e"):
        vals = tuple(round(v, 6) for v in vals)
    if fmt == 87:  # BGRA -> RGBA
        vals = (vals[2], vals[1], vals[0], vals[3])
    return vals


def decode_vertices(data: bytes, stride: int, elements: list[dict], first: int = 0, count: int | None = None) -> list[dict]:
    """Decodes vertices of one buffer slot using the elements that live in that slot."""
    if stride <= 0:
        return []
    total = len(data) // stride
    if count is None or first + count > total:
        count = max(total - first, 0)
    out = []
    for i in range(first, first + count):
        base = i * stride
        v: dict = {}
        for e in elements:
            name = e["semantic"] + (str(e["index"]) if e["index"] else "")
            v[name] = decode_element(data, base + e["offset"], e["format"])
        out.append(v)
    return out


def decode_indices(data: bytes, fmt: int) -> list[int]:
    if fmt == 42:  # R32_UINT
        return list(struct.unpack_from(f"<{len(data) // 4}I", data, 0))
    return list(struct.unpack_from(f"<{len(data) // 2}H", data, 0))


def decode_constants(data: bytes) -> list[tuple[float, float, float, float]]:
    """A constant buffer as rows of four floats (one row per register)."""
    n = len(data) // 16
    flat = struct.unpack_from(f"<{n * 4}f", data, 0)
    return [flat[i * 4:i * 4 + 4] for i in range(n)]


# ---------------------------------------------------------------- textures
def make_dds(width: int, height: int, fmt: int, data: bytes, mips: int = 1) -> bytes:
    """Wraps one subresource in a DDS container with a DX10 header."""
    DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x1, 0x2, 0x4, 0x1000, 0x20000, 0x80000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
    header = struct.pack("<4sIIIIIII", b"DDS ", 124, flags, height, width, len(data), 0, mips)
    header += b"\0" * 44
    header += struct.pack("<II4sIIIII", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)      # DDS_PIXELFORMAT, DDPF_FOURCC
    header += struct.pack("<IIIII", 0x1000 | (0x400008 if mips > 1 else 0), 0, 0, 0, 0)
    dx10 = struct.pack("<IIIII", fmt, 3, 0, 1, 0)                             # D3D10_RESOURCE_DIMENSION_TEXTURE2D
    return header + dx10 + data


def texture_to_png(width: int, height: int, fmt: int, data: bytes, max_size: int = 0) -> bytes | None:
    """Best-effort PNG of one subresource. Returns None when the format is not handled."""
    img = _texture_to_image(width, height, fmt, data)
    return _image_to_png(img, max_size) if img is not None else None


def texture_to_pngs(width: int, height: int, fmt: int, data: bytes,
                    preview_max_size: int = 512) -> tuple[bytes | None, bytes | None]:
    """Full PNG and optional preview from one decode of the same readback bytes.

    A non-positive preview_max_size disables preview generation. No readback
    data or decoded image is cached across calls: textures may change each frame.
    """
    img = _texture_to_image(width, height, fmt, data)
    if img is None:
        return None, None
    full = _image_to_png(img)
    preview = None
    if preview_max_size > 0:
        preview = full if max(img.size) <= preview_max_size else _image_to_png(img, preview_max_size)
    return full, preview


def _texture_to_image(width: int, height: int, fmt: int, data: bytes):
    try:
        from PIL import Image
    except Exception:
        return None
    img = None
    name = format_name(fmt)
    try:
        if name.startswith("BC") or name.startswith("R8G8B8A8") or name.startswith("B8G8R8"):
            if name.startswith("R8G8B8A8"):
                img = Image.frombytes("RGBA", (width, height), data[:width * height * 4])
            elif name.startswith("B8G8R8"):
                img = Image.frombytes("RGBA", (width, height), data[:width * height * 4], "raw", "BGRA")
                if "X8" in name:
                    img = img.convert("RGB")
            else:
                img = Image.open(io.BytesIO(make_dds(width, height, fmt, data)))
                img.load()
        else:
            img = _numeric_to_image(width, height, fmt, data)
    except Exception:
        img = None
    return img


def _image_to_png(img, max_size: int = 0) -> bytes:
    if max_size and max(img.size) > max_size:
        scale = max_size / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    out = io.BytesIO()
    # PNG compression changes size and CPU cost, never decoded pixel values.
    img.save(out, "PNG", compress_level=1)
    return out.getvalue()


def _numeric_to_image(width: int, height: int, fmt: int, data: bytes):
    """Float, depth and narrow integer formats, scaled into 8 bits for viewing."""
    import numpy as np
    from PIL import Image

    name = format_name(fmt)
    n = width * height

    def norm(a):
        a = np.nan_to_num(a.astype(np.float32))
        lo, hi = float(a.min()), float(a.max())
        if hi - lo < 1e-12:
            return np.zeros(a.shape, np.uint8)
        return ((a - lo) / (hi - lo) * 255.0).astype(np.uint8)

    if name in ("R8_UNORM", "A8_UNORM", "R8_TYPELESS", "R8_UINT"):
        return Image.frombytes("L", (width, height), data[:n])
    if name in ("R8G8_UNORM", "R8G8_TYPELESS", "R8G8_SNORM"):
        a = np.frombuffer(data[:n * 2], np.uint8).reshape(height, width, 2)
        rgb = np.dstack([a[..., 0], a[..., 1], np.zeros((height, width), np.uint8)])
        return Image.fromarray(rgb, "RGB")
    if name in ("R16_UNORM", "D16_UNORM", "R16_TYPELESS", "R16_UINT"):
        return Image.fromarray(norm(np.frombuffer(data[:n * 2], np.uint16).reshape(height, width)), "L")
    if name == "R16_FLOAT":
        return Image.fromarray(norm(np.frombuffer(data[:n * 2], np.float16).reshape(height, width)), "L")
    if name in ("R32_FLOAT", "D32_FLOAT", "R32_TYPELESS"):
        return Image.fromarray(norm(np.frombuffer(data[:n * 4], np.float32).reshape(height, width)), "L")
    if name in ("R32G8X24_TYPELESS", "D32_FLOAT_S8X24_UINT", "R32_FLOAT_X8X24_TYPELESS"):
        # 32-bit float depth, then 8 bits of stencil and 24 unused: the game's main depth buffer. Depth is shown.
        a = np.frombuffer(data[:n * 8], np.float32).reshape(height, width, 2)
        return Image.fromarray(norm(a[..., 0]), "L")
    if name == "X32_TYPELESS_G8X24_UINT":
        # the stencil plane of the same layout
        a = np.frombuffer(data[:n * 8], np.uint8).reshape(height, width, 8)
        return Image.fromarray(norm(a[..., 4]), "L")
    if name in ("R24G8_TYPELESS", "D24_UNORM_S8_UINT", "R24_UNORM_X8_TYPELESS"):
        a = np.frombuffer(data[:n * 4], np.uint32).reshape(height, width) & 0xFFFFFF
        return Image.fromarray(norm(a), "L")
    if name in ("R16G16_FLOAT", "R16G16_UNORM", "R16G16_SNORM", "R16G16_TYPELESS"):
        dt = np.float16 if "FLOAT" in name else np.int16 if "SNORM" in name else np.uint16
        a = np.frombuffer(data[:n * 4], dt).reshape(height, width, 2)
        rgb = np.dstack([norm(a[..., 0]), norm(a[..., 1]), np.zeros((height, width), np.uint8)])
        return Image.fromarray(rgb, "RGB")
    if name in ("R16G16B16A16_FLOAT", "R16G16B16A16_UNORM", "R16G16B16A16_TYPELESS"):
        dt = np.float16 if "FLOAT" in name else np.uint16
        a = np.frombuffer(data[:n * 8], dt).reshape(height, width, 4).astype(np.float32)
        if dt is np.uint16:
            a = a / 65535.0
        rgb = (np.clip(a[..., :3], 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
        return Image.fromarray(rgb, "RGB")
    if name == "R32G32B32A32_FLOAT":
        a = np.frombuffer(data[:n * 16], np.float32).reshape(height, width, 4)
        rgb = (np.clip(np.nan_to_num(a[..., :3]), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
        return Image.fromarray(rgb, "RGB")
    if name == "R11G11B10_FLOAT":
        v = np.frombuffer(data[:n * 4], np.uint32).reshape(height, width)

        def uf(bits, mant_bits):
            mant_mask = (1 << mant_bits) - 1
            e = (bits >> mant_bits).astype(np.int32)
            m = (bits & mant_mask).astype(np.float32)
            val = np.where(e == 0, m / (1 << mant_bits) * 2.0 ** -14,
                           (1.0 + m / (1 << mant_bits)) * np.exp2((e - 15).astype(np.float32)))
            return np.where(e == 31, 65504.0, val)

        r = uf(v & 0x7FF, 6)
        g = uf((v >> 11) & 0x7FF, 6)
        b = uf((v >> 22) & 0x3FF, 5)
        rgb = (np.clip(np.dstack([r, g, b]), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
        return Image.fromarray(rgb, "RGB")
    if name in ("R10G10B10A2_UNORM", "R10G10B10A2_TYPELESS"):
        v = np.frombuffer(data[:n * 4], np.uint32).reshape(height, width)
        rgb = np.dstack([(v & 1023) >> 2, ((v >> 10) & 1023) >> 2, ((v >> 20) & 1023) >> 2]).astype(np.uint8)
        return Image.fromarray(rgb, "RGB")
    return None
