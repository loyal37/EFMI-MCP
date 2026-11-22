"""XXMI-compatible sampling on captured bytes, outside the game.

This is a fresh fingerprint of a named mip, not XXMI's private cached value.
"""
import ctypes
from pathlib import Path
from . import decode

def compute(meta, data, view_format=""):
    fmt = meta["format"]
    if view_format in decode.FORMAT_NAMES and not view_format.endswith("_TYPELESS"):
        fmt = decode.FORMAT_NAMES.index(view_format)
    name = decode.format_name(fmt)
    if name.endswith(("_UINT", "_SINT", "_TYPELESS")) or meta.get("depth", 0):
        raise ValueError("dHash requires a sampled, typed 2D float/normalized texture")
    path = Path(__file__).resolve().parents[2] / "dist" / "efmi_dhash.dll"
    lib = ctypes.CDLL(str(path))
    fn = lib.EfmiDHash
    fn.argtypes = [ctypes.c_uint]*4 + [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64)]
    fn.restype = ctypes.c_long
    rows = (meta["height"]+3)//4 if name.startswith("BC") else meta["height"]
    if not rows or len(data) % rows:
        raise ValueError("Invalid tightly packed texture payload")
    value = ctypes.c_uint64()
    hr = fn(meta["width"], meta["height"], fmt, len(data)//rows, data, len(data), ctypes.byref(value))
    if hr < 0:
        raise ValueError(f"D3D11 dHash sampling failed: 0x{hr & 0xffffffff:08x}")
    return f"{value.value:016x}"

def register(srv, session, capture_prop):
    @srv.tool("efmi_texture_dhash", "Compute a texture fingerprint from a captured mip using XXMI's sampling rules. "
              "This is computed from captured pixels, not read from XXMI's private cache. "
              "Needs contents='inputs'; mip is explicit and defaults to 0.",
              {"draw": {"type":"integer"}, "slot":{"type":"string"}, **capture_prop,
               "mip":{"type":"integer", "default":0}}, ["draw","slot"], read_only=False)
    def texture_dhash(draw, slot, capture="latest", mip=0):
        import json
        from .mcp_stdio import ToolError
        cap = session.capture(capture)
        entry = cap.draw(draw).get("tex", {}).get(slot.lower())
        if not entry: raise ToolError("No texture in that slot")
        resource = cap.resource(entry["res"])
        if resource.get("kind") != "tex2d" or resource.get("array", 1) != 1:
            raise ToolError("dHash supports non-array Texture2D only")
        ids = entry.get("mip_contents", [])
        kept = cap.content(ids[mip] if 0 <= mip < len(ids) else entry.get("content"))
        if not kept or kept.get("mip") != mip:
            raise ToolError("That mip was not kept; capture with inputs using probe 0.1.16")
        meta, data = session.call(lambda c: c.read_content(cap.frame, kept["id"]))
        if not meta.get("exact", False): raise ToolError("Captured contents are not exact")
        try: value = compute(meta, data, (entry.get("view") or {}).get("format", ""))
        except (ValueError, OSError) as exc: raise ToolError(str(exc))
        return json.dumps(dict(dhash=value, mip=mip, source="captured_pixels", xxmi_cached=False,
                               note="XXMI may cache a different mip or earlier upload; compare using its original dump log."))
