"""Offline reload/capture regressions through the actual MCP call boundary.

The probe and Windows input are fakes: these tests never connect to a user's
game, send a key, or modify the installed EFMI. Each test owns a private INI.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.capture_model import Capture
from efmi_mcp.hashes import crc32c, hex32
from efmi_mcp.probe_client import ProbeEndpoint, ProbeError, ProbeNotFound
from efmi_mcp import tools
from efmi_mcp import reload_workflow
from efmi_mcp.reload_input import ReloadInputError


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    perf_counter = monotonic

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative wait")
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProbe:
    """Small real capture format, with advancing frames and readable indices."""
    def __init__(self, root, clock):
        self.clock = clock
        self.endpoint = ProbeEndpoint(12345, "fake-never-opened")
        self.connected = True
        self.events = []
        self.live_enabled = True
        self.indices = struct.pack("<3H", 0, 1, 2)
        self.status_overrides = {}
        self.capture_error = None
        self.capture_empty = False
        self.capture_frame = None
        self.release_error = None
        self.reads = []
        self.root = root

    def status(self):
        self.events.append(("status", self.clock.now))
        result = {
            "pid": self.endpoint.pid, "exe": r"C:\Game\Endfield.exe", "probe": "0.1.1",
            "attached": True, "swap_chain": True,
            "frame": 100 + int(self.clock.now * 60), "resolution": [1280, 720],
            "xxmi": {"present": True, "path": str(self.root / "d3d11.dll")},
            "capture": {"live": self.live_enabled},
            "capture_target": {"device": "0x100", "context": "0x200", "swap_chain": "0x300"},
        }
        result.update(copy.deepcopy(self.status_overrides))
        return result

    def live(self, enable=True, **kwargs):
        self.events.append(("live", enable))
        self.live_enabled = enable
        return {"frame": 100 + int(self.clock.now * 60)}

    def release(self):
        self.events.append(("release", self.clock.now))
        if self.release_error:
            raise self.release_error

    def capture(self, **kwargs):
        self.events.append(("capture", self.clock.now, kwargs))
        if self.capture_error:
            raise self.capture_error
        frame = self.capture_frame if self.capture_frame is not None else 101 + int(self.clock.now * 60)
        doc = self.document(frame)
        doc["detail"] = kwargs.get("detail", "full")
        if self.capture_empty:
            doc["draws"] = []
        return doc, b""

    def document(self, frame):
        h = hex32(crc32c(self.indices))
        return {
            "frame": frame, "held": True, "detail": "full", "resolution": [1280, 720],
            "resources": [{"kind": "buffer", "size": len(self.indices), "bind": 2,
                           "hash": h, "known": True}],
            "regions": [[0, 0, len(self.indices), h, "ok"]],
            "draws": [{"i": 0, "t": 1, "o": "game", "ic": 3, "fi": 0,
                       "fv": 0, "inst": 1, "topo": 4, "ib": [0, 57, 0, 0]}],
            "stats": {"record_ms": 0.01},
        }

    def read_buffer(self, capture_frame, res, offset=0, size=0, **kwargs):
        self.reads.append((capture_frame, res, offset, size))
        return {"read_frame": capture_frame + 1}, self.indices[offset:offset + size]


def mcp_call(server, name, **arguments):
    reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    result = reply["result"]
    text = "\n".join(item["text"] for item in result["content"] if item["type"] == "text")
    return text, result["isError"]


class ReloadWorkflowTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="reload-test-", dir=build)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_config("[Hunting]\nreload_config = no_modifiers VK_F10\n"
                          "reload_fixes = no_modifiers VK_F10\n")
        (self.root / "d3d11.dll").touch()
        self.clock = FakeClock()
        self.probe = FakeProbe(self.root, self.clock)
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = self.probe
        self.session.add_capture(Capture(self.probe.document(80)))
        self.old_mod_index = self.session.mods.get()
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", self.root / "probe.dll")
        self.sender = self.enterContext(patch.object(reload_workflow, "send_reload_f10", side_effect=self.send_key))
        self.enterContext(patch.object(reload_workflow.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(reload_workflow.time, "sleep", self.clock.sleep))
        # Defense in depth: even a regression that bypasses session.client must
        # never discover or open a user's actual probe while running this suite.
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def write_config(self, text):
        (self.root / "d3dx.ini").write_text(text, encoding="utf-8")

    def send_key(self, *args, **kwargs):
        self.probe.events.append(("key", self.clock.now))
        return {"pid": self.probe.endpoint.pid, "key": "F10", "input_sent": True,
                "key_down_sent": True, "key_up_sent": True, "key_released": True,
                "key_down_observed": True}

    def call(self, **arguments):
        return mcp_call(self.server, "efmi_reload_and_capture", **arguments)

    def assert_preflight_rejected(self, text, error):
        self.assertTrue(error, text)
        self.sender.assert_not_called()
        self.assertEqual(self.session.latest, 80)
        self.assertEqual(list(self.session.captures), [80])
        self.assertNotIn("capture", [event[0] for event in self.probe.events])
        self.assertNotIn("release", [event[0] for event in self.probe.events])

    def assert_stale_unavailable(self):
        self.assertIsNone(self.session.latest)
        self.assertFalse(self.session.captures)
        text, error = mcp_call(self.server, "efmi_get_draw", draw=0, capture=80)
        self.assertTrue(error, text)
        self.assertIn("not in memory", text)

    def test_registered_tool_is_explicitly_mutating(self):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        entry = next(t for t in reply["result"]["tools"] if t["name"] == "efmi_reload_and_capture")
        self.assertFalse(entry["annotations"]["readOnlyHint"])
        self.assertFalse(entry["inputSchema"]["additionalProperties"])

    def test_success_uses_fresh_capture_without_claiming_reload_confirmation(self):
        text, error = self.call()
        self.assertFalse(error, text)
        result = json.loads(text)
        self.assertEqual(result["status"], "captured_after_reload_request")
        self.assertIs(result["reload_requested"], True)
        self.assertIs(result["reload_confirmed"], False)
        self.assertGreater(result["capture"]["frame"], result["barrier_frame"])
        self.assertEqual(self.session.latest, result["capture"]["frame"])
        self.assertEqual(len(self.session.captures), 1)
        self.assertNotIn(80, self.session.captures)
        self.assertFalse(self.probe.live_enabled)
        self.assertIsNot(self.session.mods.get(), self.old_mod_index)
        self.sender.assert_called_once()
        events = [event[0] for event in self.probe.events]
        self.assertLess(events.index("live"), events.index("key"))
        self.assertLess(events.index("release"), events.index("key"))
        capture = next(event for event in self.probe.events if event[0] == "capture")
        self.assertGreaterEqual(capture[1], 2.0)
        self.assertEqual(capture[2]["detail"], "full")
        self.assertIs(capture[2]["cb_data"], True)
        text, error = mcp_call(self.server, "efmi_get_draw", draw=0)
        self.assertFalse(error, text)
        self.assertEqual(json.loads(text)["capture"], self.session.latest)
        text, error = mcp_call(self.server, "efmi_read_buffer", draw=0, slot="ib")
        self.assertFalse(error, text)
        self.assertEqual(json.loads(text)["indices"], [0, 1, 2])
        self.assertEqual(self.probe.reads[-1][0], self.session.latest)

    def test_capture_options_are_forwarded(self):
        text, error = self.call(detail="light", cb_data=False, settle_ms=750, focus_game=False)
        self.assertFalse(error, text)
        capture = next(event for event in self.probe.events if event[0] == "capture")
        self.assertEqual(capture[2]["detail"], "light")
        self.assertIs(capture[2]["cb_data"], False)
        self.assertGreaterEqual(capture[1], 0.75)
        self.assertIs(self.sender.call_args.kwargs["focus_game"], False)

    def test_timeout_is_per_capture_and_is_forwarded_once(self):
        text, error = self.call(settle_ms=2000, timeout_ms=1000)
        self.assertFalse(error, text)
        captures = [event for event in self.probe.events if event[0] == "capture"]
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0][2]["timeout_ms"], 1000)
        self.assertGreaterEqual(captures[0][1], 2.0)

    def test_absent_probe_never_sends_input(self):
        self.session.client = None
        self.assert_preflight_rejected(*self.call())

    def test_wrong_xxmi_installation_never_sends_input(self):
        self.probe.status_overrides["xxmi"] = {"present": True, "path": str(self.root / "other" / "d3d11.dll")}
        self.assert_preflight_rejected(*self.call())

    def test_missing_xxmi_never_sends_input(self):
        self.probe.status_overrides["xxmi"] = {"present": False, "path": ""}
        self.assert_preflight_rejected(*self.call())

    def test_no_attached_device_never_sends_input(self):
        self.probe.status_overrides["attached"] = False
        self.assert_preflight_rejected(*self.call())

    def test_no_swap_chain_never_sends_input(self):
        self.probe.status_overrides["swap_chain"] = False
        self.assert_preflight_rejected(*self.call())

    def test_missing_configuration_never_sends_input(self):
        (self.root / "d3dx.ini").unlink()
        self.assert_preflight_rejected(*self.call())

    def test_non_f10_or_incomplete_mapping_never_sends_input(self):
        configs = ["[Hunting]\nreload_config = no_modifiers VK_F11\nreload_fixes = no_modifiers VK_F10\n",
                   "[Hunting]\nreload_config = no_modifiers VK_F10\n",
                   "[Hunting]\nreload_config = VK_F10\nreload_fixes = VK_F10\n",
                   "[Input]\nreload_config = no_modifiers VK_F10\nreload_fixes = no_modifiers VK_F10\n"]
        for config in configs:
            with self.subTest(config=config):
                self.write_config(config)
                self.assert_preflight_rejected(*self.call())

    def test_invalid_parameters_never_send_input(self):
        cases = [{"detail": "garbage"}, {"cb_data": "false"}, {"focus_game": 1},
                 {"settle_ms": True}, {"settle_ms": 249}, {"settle_ms": 10001},
                 {"timeout_ms": False}, {"timeout_ms": 999}, {"timeout_ms": 30001}]
        for args in cases:
            with self.subTest(args=args):
                self.assert_preflight_rejected(*self.call(**args))

    def test_probe_capture_failure_records_request_and_never_repeats_f10(self):
        self.probe.capture_error = ProbeError("capture timed out")
        text, error = self.call()
        self.assertTrue(error, text)
        result = json.loads(text)
        self.assertIs(result["reload_requested"], True)
        self.assertIs(result["reload_confirmed"], False)
        self.sender.assert_called_once()
        self.assertEqual(sum(e[0] == "capture" for e in self.probe.events), 1)
        self.assert_stale_unavailable()

    def test_input_failure_reports_whether_keydown_was_accepted(self):
        for accepted in (False, True, None):
            with self.subTest(accepted=accepted):
                self.sender.reset_mock()
                self.sender.side_effect = ReloadInputError(
                    "test_failure", "test: cannot finish key pulse",
                    {"key_down_sent": accepted, "key_up_sent": False})
                text, error = self.call()
                self.assertTrue(error, text)
                result = json.loads(text)
                self.assertIs(result["reload_requested"], accepted)
                self.assertIs(result["reload_confirmed"], False)
                self.sender.assert_called_once()
                self.assertNotIn("capture", [e[0] for e in self.probe.events])
                self.assert_stale_unavailable()

    def test_unexpected_input_error_does_not_claim_no_key_was_sent(self):
        self.sender.side_effect = RuntimeError("unexpected failure after attempting input")
        text, error = self.call()
        self.assertTrue(error, text)
        self.assertIsNone(json.loads(text)["reload_requested"])
        self.sender.assert_called_once()
        self.assert_stale_unavailable()

    def test_accepted_but_unobserved_input_error_never_starts_capture(self):
        self.sender.side_effect = ReloadInputError(
            "keydown_not_observed", "Accepted event did not establish a visible pressed state",
            {"key_down_sent": True, "key_up_sent": True, "key_down_observed": False})
        text, error = self.call()
        self.assertTrue(error, text)
        result = json.loads(text)
        self.assertEqual(result["phase"], "input")
        self.assertEqual(result["input_error_code"], "keydown_not_observed")
        self.assertTrue(result["reload_requested"])
        self.assertFalse(result["input"]["key_down_observed"])
        self.assertFalse(result["reload_confirmed"])
        self.sender.assert_called_once()
        self.assertNotIn("capture", [e[0] for e in self.probe.events])
        self.assert_stale_unavailable()

    def test_receipt_without_observed_press_is_rejected(self):
        self.sender.side_effect = None
        for observation in (False, None):
            with self.subTest(observation=observation):
                self.sender.return_value = {"key_down_sent": True, "key_up_sent": True,
                                            "key_down_observed": observation}
                text, error = self.call()
                self.assertTrue(error, text)
                self.assertEqual(json.loads(text)["phase"], "input")
                self.assertNotIn("capture", [e[0] for e in self.probe.events])
                self.assert_stale_unavailable()

    def test_partial_input_receipt_does_not_start_capture(self):
        self.sender.side_effect = None
        self.sender.return_value = {"key_down_sent": True, "key_up_sent": False}
        text, error = self.call()
        self.assertTrue(error, text)
        self.assertIs(json.loads(text)["reload_requested"], True)
        self.assertNotIn("capture", [e[0] for e in self.probe.events])
        self.assert_stale_unavailable()

    def test_empty_frame_is_not_published_as_success(self):
        self.probe.capture_empty = True
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_called_once()
        self.assert_stale_unavailable()

    def test_old_frame_is_not_published_as_fresh(self):
        self.probe.capture_frame = 99
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_called_once()
        self.assert_stale_unavailable()

    def test_same_frame_as_after_wait_barrier_is_rejected(self):
        self.probe.capture_frame = 220  # status after default two-second settle
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_called_once()
        self.assert_stale_unavailable()

    def test_changed_process_after_key_does_not_read_a_different_game(self):
        original_sender = self.send_key

        def changed(*args, **kwargs):
            receipt = original_sender(*args, **kwargs)
            self.probe.status_overrides["pid"] = 54321
            return receipt

        self.sender.side_effect = changed
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_called_once()
        self.assertNotIn("capture", [e[0] for e in self.probe.events])
        self.assert_stale_unavailable()

    def test_changed_capture_target_after_key_does_not_claim_success(self):
        original_sender = self.send_key

        def changed(*args, **kwargs):
            receipt = original_sender(*args, **kwargs)
            self.probe.status_overrides["capture_target"] = {
                "device": "0x999", "context": "0x200", "swap_chain": "0x300"}
            return receipt

        self.sender.side_effect = changed
        text, error = self.call()
        self.assertTrue(error, text)
        self.assertNotIn("capture", [e[0] for e in self.probe.events])
        self.assert_stale_unavailable()

    def test_target_change_during_capture_does_not_publish_it(self):
        original_capture = self.probe.capture

        def changed(**kwargs):
            result = original_capture(**kwargs)
            self.probe.status_overrides["capture_target"] = {
                "device": "0x100", "context": "0x200", "swap_chain": "0x999"}
            return result

        self.probe.capture = changed
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_called_once()
        self.assert_stale_unavailable()

    def test_preexisting_recording_is_not_interrupted(self):
        self.probe.status_overrides["capture"] = {"live": False, "recording": True}
        self.assert_preflight_rejected(*self.call())

    def test_live_recording_is_stopped_before_reload(self):
        self.probe.status_overrides["capture"] = {"live": True, "recording": True}
        text, error = self.call()
        self.assertFalse(error, text)
        events = [e[0] for e in self.probe.events]
        self.assertLess(events.index("live"), events.index("key"))
        self.assertFalse(self.probe.live_enabled)
        self.sender.assert_called_once()

    def test_pipe_pid_mismatch_is_rejected_before_input(self):
        self.probe.endpoint = ProbeEndpoint(67890, "fake-never-opened")
        self.probe.status_overrides["pid"] = 12345
        self.assert_preflight_rejected(*self.call())

    def test_unknown_arguments_are_rejected_at_mcp_boundary(self):
        self.assert_preflight_rejected(*self.call(key="F11"))

    def test_case_whitespace_and_comments_on_f10_bindings(self):
        self.write_config("[HUNTING]\n ReLoad_Config = No_Modifiers  vk_f10 ; comment\n"
                          "\treload_fixes\t=\tno_modifiers\tVK_F10\n")
        text, error = self.call()
        self.assertFalse(error, text)
        self.sender.assert_called_once()

    def test_duplicate_active_reload_mapping_is_rejected(self):
        self.write_config("[Hunting]\nreload_config = no_modifiers VK_F10\n"
                          "reload_config = no_modifiers VK_F10\nreload_fixes = no_modifiers VK_F10\n")
        self.assert_preflight_rejected(*self.call())

    def test_live_already_off_stays_off(self):
        self.probe.live_enabled = False
        text, error = self.call()
        self.assertFalse(error, text)
        self.assertFalse(self.probe.live_enabled)
        self.assertFalse(any(e[0] == "live" and e[1] for e in self.probe.events))

    def test_release_failure_does_not_send_input_or_keep_old_capture(self):
        self.probe.release_error = ProbeError("release failed")
        text, error = self.call()
        self.assertTrue(error, text)
        self.sender.assert_not_called()
        self.assert_stale_unavailable()


if __name__ == "__main__":
    unittest.main(verbosity=2)
