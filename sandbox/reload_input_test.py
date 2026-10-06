"""Guarded F10 behavior using an injected backend; never inject real input."""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from efmi_mcp import reload_input as ri


class FakeBackend:
    def __init__(self):
        self.pid = 123
        self.window_pids = {10: 123, 20: 456}
        self.eligible = {10, 20}
        self.foreground = 10
        self.held = []
        self.focus_allowed = True
        self.focus_calls = []
        self.events = []
        self.sleeps = []
        self.down_result = (1, 0)
        self.up_results = [(1, 0)]
        self.on_sleep = None
        self.on_keys = None
        self.key_checks = 0
        self.f10_down = False
        self.discard_input = False

    def windows(self):
        return list(self.window_pids)

    def eligible_window(self, hwnd, pid):
        return hwnd in self.eligible and self.window_pids.get(hwnd) == pid

    def foreground_window(self):
        return self.foreground

    def set_foreground_window(self, hwnd):
        self.focus_calls.append(hwnd)
        if self.focus_allowed:
            self.foreground = hwnd
        return self.focus_allowed

    def held_keys(self, include_f10=True):
        self.key_checks += 1
        if self.on_keys:
            self.on_keys(self)
        return [key for key in self.held if include_f10 or key != "F10"]

    def send_f10(self, *, key_up):
        self.events.append("up" if key_up else "down")
        result = self.up_results.pop(0) if key_up else self.down_result
        if isinstance(result, BaseException):
            raise result
        if result[0] == 1 and not self.discard_input:
            self.f10_down = not key_up
        return result

    def f10_is_down(self):
        return self.f10_down

    def sleep(self, duration):
        self.sleeps.append(duration)
        if self.on_sleep:
            self.on_sleep(self)


class ReloadInputTests(unittest.TestCase):
    def assert_refused(self, backend, code, **kwargs):
        with self.assertRaises(ri.ReloadInputError) as ctx:
            ri.send_reload_f10(123, backend=backend, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(backend.events, [])
        return ctx.exception

    def test_windows_input_abi(self):
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(ri._INPUT), 40 if pointer_size == 8 else 28)
        self.assertEqual(ri._INPUT.value.offset, 8 if pointer_size == 8 else 4)
        self.assertEqual(ctypes.sizeof(ri._KEYBDINPUT), 24 if pointer_size == 8 else 16)

    def test_win32_adapter_builds_only_fixed_f10_events(self):
        # Exercise the real adapter's ctypes serialization against a fake
        # SendInput, without loading user32 or injecting any OS input.
        calls = []
        class User32:
            def SendInput(self, count, pointer, size):
                event = ctypes.cast(pointer, ctypes.POINTER(ri._INPUT)).contents
                calls.append((count, size, event.type, event.ki.wVk,
                              event.ki.wScan, event.ki.dwFlags, event.ki.dwExtraInfo))
                return 1
        backend = object.__new__(ri.Win32ReloadBackend)
        backend.user32 = User32()
        with patch.object(ctypes, "set_last_error", create=True), \
                patch.object(ctypes, "get_last_error", return_value=0, create=True):
            self.assertEqual(backend.send_f10(key_up=False), (1, 0))
            self.assertEqual(backend.send_f10(key_up=True), (1, 0))
        self.assertEqual(calls, [(1, ctypes.sizeof(ri._INPUT), 1, 0x79, 0, 0, 0),
                                 (1, ctypes.sizeof(ri._INPUT), 1, 0x79, 0, 2, 0)])

    def test_key_state_uses_high_bit_only(self):
        class User32:
            def GetAsyncKeyState(self, vk):
                return 0x8000 if vk == 0x11 else 1
        backend = object.__new__(ri.Win32ReloadBackend)
        backend.user32 = User32()
        self.assertEqual(backend.held_keys(), ["Ctrl"])
        self.assertFalse(backend.f10_is_down())
        backend.user32.GetAsyncKeyState = lambda vk: 0x8000
        self.assertTrue(backend.f10_is_down())

    def test_success_foreground_no_focus_steal(self):
        backend = FakeBackend()
        result = ri.send_reload_f10(123, backend=backend)
        self.assertTrue(result["key_down_sent"] and result["key_up_sent"])
        self.assertTrue(result["key_down_observed"])
        self.assertEqual(result["hwnd"], 10)
        self.assertEqual(result["key"], "F10")
        self.assertEqual(backend.events, ["down", "up"])
        self.assertAlmostEqual(sum(backend.sleeps), 0.2)
        self.assertEqual(backend.focus_calls, [])

    def test_accepted_but_discarded_input_is_not_success(self):
        backend = FakeBackend()
        backend.discard_input = True
        with self.assertRaises(ri.ReloadInputError) as ctx:
            ri.send_reload_f10(123, backend=backend)
        self.assertEqual(ctx.exception.code, "keydown_not_observed")
        self.assertTrue(ctx.exception.details["key_down_sent"])
        self.assertTrue(ctx.exception.details["key_up_sent"])
        self.assertFalse(ctx.exception.details["key_down_observed"])
        self.assertEqual(ctx.exception.details["key_down_count"], 1)
        self.assertEqual(backend.events, ["down", "up"])
        self.assertEqual(backend.sleeps, [0.02] * 10)

    def test_delayed_pressed_state_is_accepted(self):
        backend = FakeBackend()
        backend.on_sleep = lambda b: setattr(b, "f10_down", len(b.sleeps) >= 8)
        result = ri.send_reload_f10(123, backend=backend)
        self.assertTrue(result["key_down_sent"])
        self.assertTrue(result["key_down_observed"])
        self.assertTrue(result["key_up_sent"])
        self.assertEqual(backend.events, ["down", "up"])
        self.assertEqual(backend.sleeps, [0.02] * 10)
        self.assertFalse(backend.f10_down)

    def test_focus_once_and_do_not_restore(self):
        backend = FakeBackend()
        backend.foreground = 20
        result = ri.send_reload_f10(123, backend=backend)
        self.assertTrue(result["focus_changed"])
        self.assertEqual(backend.focus_calls, [10])
        self.assertEqual(backend.foreground, 10)

    def test_does_not_focus_if_disabled(self):
        backend = FakeBackend()
        backend.foreground = 20
        self.assert_refused(backend, "not_foreground", focus_game=False)
        self.assertEqual(backend.focus_calls, [])

    def test_focus_refusal_is_bounded_and_never_sends(self):
        backend = FakeBackend()
        backend.foreground = 20
        backend.focus_allowed = False
        self.assert_refused(backend, "focus_refused")
        self.assertEqual(backend.focus_calls, [10])
        self.assertAlmostEqual(sum(backend.sleeps), 0.5)

    def test_exact_pid_and_ineligible_window(self):
        for change in (lambda b: b.window_pids.update({10: 999}), lambda b: b.eligible.remove(10)):
            with self.subTest(change=change):
                backend = FakeBackend()
                change(backend)
                self.assert_refused(backend, "no_game_window")

    def test_multiple_windows_require_foreground_selection(self):
        backend = FakeBackend()
        backend.window_pids[20] = 123
        backend.foreground = 30
        self.assert_refused(backend, "ambiguous_game_window")
        backend.foreground = 20
        self.assertEqual(ri.send_reload_f10(123, backend=backend)["hwnd"], 20)

    def test_held_modifiers_and_f10_block_before_focus(self):
        for key in ["Shift", "Ctrl", "Alt", "Left Win", "Right Win", "F10"]:
            with self.subTest(key=key):
                backend = FakeBackend()
                backend.held = [key]
                backend.foreground = 20
                self.assert_refused(backend, "keys_held")
                self.assertEqual(backend.focus_calls, [])

    def test_modifier_pressed_after_focus_blocks_before_down(self):
        backend = FakeBackend()
        backend.on_keys = lambda b: setattr(b, "held", ["Ctrl"] if b.key_checks == 2 else [])
        self.assert_refused(backend, "keys_held")

    def test_focus_lost_during_final_key_check_blocks_down(self):
        backend = FakeBackend()
        backend.on_keys = lambda b: setattr(b, "foreground", 20 if b.key_checks == 2 else 10)
        self.assert_refused(backend, "foreground_lost")

    def test_reused_window_wrong_pid_blocks_down(self):
        backend = FakeBackend()
        def change_pid(b):
            if b.key_checks == 2:
                b.window_pids[10] = 999
        backend.on_keys = change_pid
        self.assert_refused(backend, "foreground_lost")

    def test_hold_interruption_releases_key(self):
        for interruption, code in [(lambda b: setattr(b, "foreground", 20), "foreground_lost"),
                                    (lambda b: setattr(b, "held", ["Ctrl"]), "keys_held")]:
            with self.subTest(code=code):
                backend = FakeBackend()
                backend.on_sleep = interruption
                with self.assertRaises(ri.ReloadInputError) as ctx:
                    ri.send_reload_f10(123, backend=backend)
                self.assertEqual(ctx.exception.code, code)
                self.assertTrue(ctx.exception.details["key_down_sent"])
                self.assertTrue(ctx.exception.details["key_up_sent"])
                self.assertEqual(backend.events, ["down", "up"])
                self.assertEqual(len(backend.sleeps), 1)

    def test_zero_or_exception_down_still_attempts_up(self):
        for result in [(0, 5), RuntimeError("send failed")]:
            with self.subTest(result=result):
                backend = FakeBackend()
                backend.down_result = result
                with self.assertRaises(ri.ReloadInputError):
                    ri.send_reload_f10(123, backend=backend)
                self.assertEqual(backend.events, ["down", "up"])

    def test_keyup_retry_and_exhaustion(self):
        backend = FakeBackend()
        backend.up_results = [(0, 5), (1, 0)]
        result = ri.send_reload_f10(123, backend=backend)
        self.assertTrue(result["key_up_sent"])
        self.assertEqual(result["key_up_attempts"], 2)
        backend = FakeBackend()
        backend.up_results = [(0, 5), OSError("blocked")]
        with self.assertRaises(ri.ReloadInputError) as ctx:
            ri.send_reload_f10(123, backend=backend)
        self.assertEqual(ctx.exception.code, "keyup_failed")
        self.assertFalse(ctx.exception.details["key_up_sent"])

    def test_keyboard_interrupt_releases_and_unlocks(self):
        backend = FakeBackend()
        def interrupt(_):
            raise KeyboardInterrupt()
        backend.on_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            ri.send_reload_f10(123, backend=backend)
        self.assertEqual(backend.events, ["down", "up"])
        ri.send_reload_f10(123, backend=FakeBackend())

    def test_invalid_parameters_do_not_construct_windows_backend(self):
        with patch.object(ri, "Win32ReloadBackend") as construct:
            for pid in [0, -1, True, "123", 2**32]:
                with self.assertRaises(ri.ReloadInputError):
                    ri.send_reload_f10(pid)
            with self.assertRaises(ri.ReloadInputError):
                ri.send_reload_f10(123, focus_game="false")
            construct.assert_not_called()

    def test_concurrent_call_refused(self):
        with ri._SEND_LOCK:
            self.assert_refused(FakeBackend(), "reload_busy")


if __name__ == "__main__":
    unittest.main()
