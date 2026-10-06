"""A deliberately narrow F10 pulse to the connected probe's foreground window.

Importing this module does not load user32 or send input. SendInput acceptance or
observed pressed state is not an acknowledgement that XXMI reloaded; the caller
must wait/capture separately.
See https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput
and https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setforegroundwindow.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time


VK_F10 = 0x79
_MODIFIERS = ((0x10, "Shift"), (0x11, "Ctrl"), (0x12, "Alt"),
              (0x5B, "Left Win"), (0x5C, "Right Win"))
_SEND_LOCK = threading.Lock()


class ReloadInputError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# Windows uses 32-bit LONG/DWORD even on 64-bit hosts. The union must include
# MOUSEINPUT: a keyboard-only union would give SendInput the wrong cbSize.
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _INPUT_UNION)]


class _RECT(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class Win32ReloadBackend:
    """Win32 operations separated so tests never inject into a real game."""

    def __init__(self):
        if os.name != "nt":
            raise ReloadInputError("unsupported_platform", "F10 reload requires Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = ctypes.c_void_p
        self._enum_callback = ctypes.WINFUNCTYPE(ctypes.c_int, hwnd, ctypes.c_ssize_t)
        signatures = {
            "EnumWindows": ([self._enum_callback, ctypes.c_ssize_t], ctypes.c_int),
            "GetWindowThreadProcessId": ([hwnd, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
            "GetForegroundWindow": ([], hwnd),
            "SetForegroundWindow": ([hwnd], ctypes.c_int),
            "IsWindow": ([hwnd], ctypes.c_int),
            "IsWindowVisible": ([hwnd], ctypes.c_int),
            "IsWindowEnabled": ([hwnd], ctypes.c_int),
            "IsIconic": ([hwnd], ctypes.c_int),
            "GetWindow": ([hwnd, ctypes.c_uint32], hwnd),
            "GetClientRect": ([hwnd, ctypes.POINTER(_RECT)], ctypes.c_int),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_int16),
            "SendInput": ([ctypes.c_uint32, ctypes.POINTER(_INPUT), ctypes.c_int], ctypes.c_uint32),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.user32, name)
            method.argtypes = args
            method.restype = result

    def windows(self) -> list[int]:
        found = []

        @self._enum_callback
        def collect(hwnd, _):
            found.append(int(hwnd))
            return 1

        ctypes.set_last_error(0)
        if not self.user32.EnumWindows(collect, 0):
            raise ReloadInputError("window_enumeration_failed", "EnumWindows failed.",
                                   {"win32_error": ctypes.get_last_error()})
        return found

    def window_pid(self, hwnd: int) -> int:
        pid = ctypes.c_uint32()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def eligible_window(self, hwnd: int, pid: int) -> bool:
        if not (self.user32.IsWindow(hwnd) and self.window_pid(hwnd) == pid
                and self.user32.IsWindowVisible(hwnd) and self.user32.IsWindowEnabled(hwnd)
                and not self.user32.IsIconic(hwnd) and not self.user32.GetWindow(hwnd, 4)):
            return False  # GW_OWNER = 4; exclude dialogs/owned helper windows.
        rect = _RECT()
        return bool(self.user32.GetClientRect(hwnd, ctypes.byref(rect))
                    and rect.right > rect.left and rect.bottom > rect.top)

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self.user32.SetForegroundWindow(hwnd))

    def held_keys(self, include_f10: bool = True) -> list[str]:
        keys = _MODIFIERS + ((VK_F10, "F10"),) if include_f10 else _MODIFIERS
        return [name for vk, name in keys if self.user32.GetAsyncKeyState(vk) & 0x8000]

    def f10_is_down(self) -> bool:
        """Observe the current high bit, not the unreliable 'pressed since' bit."""
        return bool(self.user32.GetAsyncKeyState(VK_F10) & 0x8000)

    def send_f10(self, *, key_up: bool) -> tuple[int, int]:
        event = _INPUT(type=1)  # INPUT_KEYBOARD; no arbitrary VK/scan-code interface.
        event.ki.wVk = VK_F10
        event.ki.dwFlags = 0x0002 if key_up else 0  # KEYEVENTF_KEYUP
        ctypes.set_last_error(0)
        count = int(self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT)))
        return count, ctypes.get_last_error()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


def _require_clear_keys(backend, *, include_f10: bool = True) -> None:
    held = backend.held_keys(include_f10=include_f10)
    if held:
        raise ReloadInputError("keys_held", "Release held keys before F10 reload: " + ", ".join(held),
                               {"held_keys": held})


def _require_foreground(backend, hwnd: int, pid: int) -> None:
    if not backend.eligible_window(hwnd, pid) or backend.foreground_window() != hwnd:
        raise ReloadInputError("foreground_lost", "The exact game window is no longer in the foreground.")


def send_reload_f10(pid: int, focus_game: bool = True, *, backend=None) -> dict:
    """Send one guarded F10 pulse; never Ctrl+F10 or an arbitrary key.

    ``pid`` must come from the already connected probe. With ``focus_game`` true,
    attempt normal foreground activation once, then verify for at most 500 ms.
    Minimized/ambiguous windows are refused. No focus restoration or focus-lock
    bypass is attempted. The unavoidable gap between checking and SendInput is
    not an atomic Windows guarantee; if focus/modifiers change during the pulse,
    release F10 immediately and report the partial operation.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
        raise ReloadInputError("invalid_pid", "A positive connected-probe process ID is required.")
    if not isinstance(focus_game, bool):
        raise ReloadInputError("invalid_focus_game", "focus_game must be a boolean.")
    if not _SEND_LOCK.acquire(blocking=False):
        raise ReloadInputError("reload_busy", "Another F10 pulse is already in progress.")
    try:
        return _send_reload_f10(pid, focus_game, backend or Win32ReloadBackend())
    finally:
        _SEND_LOCK.release()


def _send_reload_f10(pid: int, focus_game: bool, backend) -> dict:
    _require_clear_keys(backend)
    candidates = [hwnd for hwnd in backend.windows() if backend.eligible_window(hwnd, pid)]
    foreground = backend.foreground_window()
    if foreground in candidates:
        hwnd = foreground
    elif len(candidates) == 1:
        hwnd = candidates[0]
    elif not candidates:
        raise ReloadInputError("no_game_window", "No visible, enabled, non-minimized game window belongs to the probe PID.",
                               {"pid": pid})
    else:
        raise ReloadInputError("ambiguous_game_window", "Multiple game windows match the probe PID; put the intended game window in the foreground.",
                               {"pid": pid, "candidate_hwnds": candidates})

    result = {"pid": pid, "hwnd": hwnd, "key": "F10", "key_down_sent": False,
              "key_down_observed": False,
              "key_up_sent": False, "focus_attempted": False, "focus_changed": False,
              "requested_hold_ms": 200, "key_up_attempts": 0}
    if foreground != hwnd:
        if not focus_game:
            raise ReloadInputError("not_foreground", "The game window is not in the foreground; focus_game=false forbids activation.", result)
        _require_clear_keys(backend)
        result["focus_attempted"] = True
        backend.set_foreground_window(hwnd)
        for _ in range(10):
            if backend.foreground_window() == hwnd:
                break
            _require_clear_keys(backend)
            backend.sleep(0.05)
        if backend.foreground_window() != hwnd:
            raise ReloadInputError("focus_refused", "Windows did not grant game foreground activation; focus the game manually and retry. No key was sent.", result)
        result["focus_changed"] = True

    _require_foreground(backend, hwnd, pid)
    _require_clear_keys(backend)
    # Recheck HWND/PID after reading key state, immediately before injection.
    _require_foreground(backend, hwnd, pid)
    pending = None
    try:
        result["key_down_sent"] = None  # Unknown if the API raises before returning its count.
        count, error = backend.send_f10(key_up=False)
        result["key_down_count"] = count
        result["key_down_sent"] = count == 1
        if count != 1:
            raise ReloadInputError("keydown_failed", "Windows did not accept F10 key-down; input blocking or a privilege-level mismatch (UIPI) may be responsible.",
                                   {"win32_error": error})
        # Poll during the hold so switching windows/modifiers causes early
        # release instead of continuing a possibly destructive Ctrl+F10 chord.
        for _ in range(10):
            backend.sleep(0.02)
            _require_foreground(backend, hwnd, pid)
            _require_clear_keys(backend, include_f10=False)
            # SendInput's accepted count alone did not ensure observable input
            # in the real game. Processing is asynchronous, so observe across
            # the existing hold instead of demanding an immediate high bit.
            if backend.f10_is_down():
                result["key_down_observed"] = True
        if not result["key_down_observed"]:
            raise ReloadInputError(
                "keydown_not_observed",
                "Windows accepted F10 input, but its pressed state was never observed during the hold. "
                "A privilege-level mismatch or input filtering may be responsible. "
                "No automatic retry was made. Observing the key would still not acknowledge an XXMI reload.")
    except BaseException as exc:
        pending = exc
    finally:
        # The API could have accepted input before an exception surfaced.
        # Always attempt key-up once injection was attempted, even after a
        # foreground switch. Never send another key-down automatically.
        cleanup_error = None
        for _ in range(2):
            result["key_up_attempts"] += 1
            try:
                count, error = backend.send_f10(key_up=True)
                result["key_up_count"] = count
                result["key_up_sent"] = count == 1
                if count == 1:
                    cleanup_error = None
                    break
                cleanup_error = f"SendInput returned {count}, Win32 error {error}"
            except Exception as exc:
                cleanup_error = str(exc)
        if cleanup_error is not None:
            result["key_up_error"] = cleanup_error
            if pending is None:
                pending = ReloadInputError("keyup_failed", "Windows did not accept F10 key-up after two attempts; release F10 manually before retrying.")
    if pending is not None:
        if isinstance(pending, ReloadInputError):
            pending.details = {**result, **pending.details}
            raise pending
        if not isinstance(pending, Exception):
            raise pending
        raise ReloadInputError("input_failed", f"F10 input failed: {pending}", result) from pending
    return result
