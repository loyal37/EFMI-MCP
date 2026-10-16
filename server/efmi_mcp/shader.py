"""Shader disassembly through the system's d3dcompiler_47.dll (in this process, not in the game)."""
from __future__ import annotations

import ctypes
import re
from collections import OrderedDict
from ctypes import POINTER, c_char_p, c_long, c_size_t, c_uint, c_void_p
from dataclasses import dataclass
from functools import cached_property

_dll = None


def _compiler():
    global _dll
    if _dll is None:
        dll = ctypes.WinDLL("d3dcompiler_47.dll")
        dll.D3DDisassemble.argtypes = [c_void_p, c_size_t, c_uint, c_char_p, POINTER(c_void_p)]
        dll.D3DDisassemble.restype = c_long
        _dll = dll
    return _dll


def disassemble(bytecode: bytes) -> str:
    """Assembly listing of a compiled shader. Raises OSError when it cannot be produced."""
    dll = _compiler()
    blob = c_void_p()
    buf = ctypes.create_string_buffer(bytecode, len(bytecode))
    hr = dll.D3DDisassemble(buf, len(bytecode), 0, None, ctypes.byref(blob))
    if hr < 0 or not blob.value:
        raise OSError(f"D3DDisassemble failed: 0x{hr & 0xFFFFFFFF:08x}")
    vtbl = ctypes.cast(blob, POINTER(POINTER(c_void_p))).contents
    get_pointer = ctypes.WINFUNCTYPE(c_void_p, c_void_p)(vtbl[3])
    get_size = ctypes.WINFUNCTYPE(c_size_t, c_void_p)(vtbl[4])
    release = ctypes.WINFUNCTYPE(c_uint, c_void_p)(vtbl[2])
    try:
        text = ctypes.string_at(get_pointer(blob), get_size(blob))
    finally:
        release(blob)
    return text.rstrip(b"\0").decode("utf-8", errors="replace")


_PROFILE = re.compile(r"^(vs|hs|ds|gs|ps|cs)_(\d+)_(\d+)$")
_RESOURCE = re.compile(r"\bt(\d+)\b")
_SAMPLER = re.compile(r"\bs(\d+)\b")


def resource_usage(text: str) -> dict:
    """Static SRV references in SM4/5 assembly, with evidence, not inferred material roles.

    Declarations alone do not establish use. Instructions in conditional branches
    are references too; this does not tell which branch any pixel executed.
    Register ranges in SM5.1 are deliberately refused rather than mistaken for
    D3D11 slots. See Microsoft's dcl_resource and sample assembly references.
    """
    resources: dict[int, dict] = {}
    profile = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        match = _PROFILE.fullmatch(line)
        if match:
            if match.groups()[1:] not in (("4", "0"), ("4", "1"), ("5", "0")):
                raise ValueError(f"unsupported shader profile {line}; texture slots require SM4 or SM5.0")
            profile = line
            continue
        if not profile or not line:
            continue
        refs = list(_RESOURCE.finditer(line))
        if any(line[m.end():].lstrip().startswith("[") for m in refs) or re.search(r"\bt\s*\[", line):
            raise ValueError("indexed resource registers cannot be resolved as fixed D3D11 slots")
        op = line.split(None, 1)[0]
        if op.startswith("dcl_resource_"):
            dimension = op[len("dcl_resource_"):].split("(", 1)[0]
            for m in refs:
                entry = resources.setdefault(int(m[1]), {"dimension": None, "instructions": []})
                entry.update(dimension=dimension, declaration_line=number)
            continue
        if op.startswith("dcl_"):
            continue
        # Keep future/unknown instructions as references as well: silently
        # dropping one would make a bound texture appear unused.
        base_op = op.split("(", 1)[0].replace("_indexable", "")
        access = ("sample" if base_op.startswith(("sample", "gather4")) else
                  "load" if base_op == "ld" or base_op.startswith("ld_") else
                  "query" if base_op.startswith(("resinfo", "bufinfo", "sample_info", "sample_pos", "lod")) else
                  "reference")
        # sample_info/sample_pos query dimensions or sample positions, not texels.
        if base_op.startswith(("sample_info", "sample_pos")):
            access = "query"
        for slot in sorted({int(m[1]) for m in refs}):
            entry = resources.setdefault(slot, {"dimension": None, "instructions": []})
            entry["instructions"].append({"line": number, "op": base_op, "access": access, "text": line,
                                           "samplers": sorted({int(m[1]) for m in _SAMPLER.finditer(line)})})
    if not profile:
        raise ValueError("no SM4/5 shader profile in the disassembly")
    return {"profile": profile, "resources": resources}


@dataclass
class ShaderListing:
    info: dict
    bytecode: bytes
    text: str

    @cached_property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @cached_property
    def usage(self) -> dict:
        return resource_usage(self.text)


class ShaderCache:
    """Bounded in-memory listings shared by slot queries and full shader paging.

    The owner clears this cache when its probe connection changes. Failed reads
    and disassemblies are not cached, so a later query can recover.
    """
    def __init__(self, max_entries: int = 128, max_bytes: int = 32 << 20):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self.entries: OrderedDict[str, tuple[ShaderListing, int]] = OrderedDict()

    def clear(self) -> None:
        self.entries.clear()
        self.bytes_used = 0

    def get(self, key: str, load) -> ShaderListing:
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key][0]
        info, code = load()
        listing = ShaderListing(info, code, disassemble(code))
        size = len(code) + len(listing.text) * 2
        if size <= self.max_bytes and self.max_entries > 0:
            while self.entries and (len(self.entries) >= self.max_entries or self.bytes_used + size > self.max_bytes):
                _, (_, old_size) = self.entries.popitem(last=False)
                self.bytes_used -= old_size
            self.entries[key] = (listing, size)
            self.bytes_used += size
        return listing
