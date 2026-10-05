"""Shader disassembly through the system's d3dcompiler_47.dll (in this process, not in the game)."""
from __future__ import annotations

import ctypes
from ctypes import POINTER, c_char_p, c_long, c_size_t, c_uint, c_void_p

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
