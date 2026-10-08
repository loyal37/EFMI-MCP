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

    def test_server_sent_key_that_never_arrives_explains_the_restart(self):
        self.sender.side_effect = ReloadInputError(
            "keydown_not_observed", "Accepted event did not establish a visible pressed state",
            {"key_down_sent": True, "key_up_sent": True, "key_down_observed": False})
        text, error = self.call()
        self.assertTrue(error, text)
        result = json.loads(text)
        self.assertEqual(result["sender"], "server")
        self.assertIn("restart the game", result["hint"])


class KeyPressingProbe(FakeProbe):
    """A probe new enough to press F10 itself and to report creation frames."""
    def __init__(self, root, clock):
        super().__init__(root, clock)
        self.status_overrides["features"] = ["reload_key", "create_frames"]
        self.status_overrides["probe"] = "0.1.2"
        self.receipt = {}
        self.presses = []
        self.edit_document = None

    def reload_key(self, **kwargs):
        self.events.append(("key", self.clock.now))
        self.presses.append(kwargs)
        if isinstance(self.receipt, BaseException):
            raise self.receipt
        receipt = {"code": "ok", "message": "", "sender": "probe", "key": "F10",
                   "key_down_sent": True, "key_down_observed": True, "key_up_sent": True,
                   "frames_while_held": 2, "released_on": "two_frames", "poll_window": "full_frame",
                   "frame_at_key_down": 100 + int(self.clock.now * 60), "xxmi_creates_before_press": 0}
        receipt.update(self.receipt)
        return receipt

    def document(self, frame):
        doc = super().document(frame)
        doc["resources"][0].update({"origin": "game", "cf": 5, "sn": 1})
        doc["shaders"] = []
        if self.edit_document:
            self.edit_document(doc)
        return doc


def xxmi_buffer(created_frame, kind="buffer"):
    return {"kind": kind, "size": 16, "bind": 8, "hash": "0badf00d", "known": True,
            "origin": "xxmi", "cf": created_frame, "sn": 40}


class ProbePressedReloadTests(unittest.TestCase):
    """The key goes down at frame 100; the fresh capture is frame 221."""
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="reload-test-", dir=build)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "d3dx.ini").write_text("[Hunting]\nreload_config = no_modifiers VK_F10\n"
                                            "reload_fixes = no_modifiers VK_F10\n", encoding="utf-8")
        (self.root / "d3d11.dll").touch()
        self.clock = FakeClock()
        self.probe = KeyPressingProbe(self.root, self.clock)
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = self.probe
        self.session.add_capture(Capture(self.probe.document(80)))
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", self.root / "probe.dll")
        # The server must not send a key of its own when the probe can.
        self.server_sender = self.enterContext(patch.object(
            reload_workflow, "send_reload_f10", side_effect=AssertionError("server-side key must not be used")))
        self.enterContext(patch.object(reload_workflow.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(reload_workflow.time, "sleep", self.clock.sleep))
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def call(self, **arguments):
        arguments.setdefault("confirm_wait_ms", 0)      # one frame, unless a test is about the waiting
        text, error = mcp_call(self.server, "efmi_reload_and_capture", **arguments)
        return json.loads(text), error

    def captures(self):
        return [(event[2]["detail"], event[2]["cb_data"]) for event in self.probe.events if event[0] == "capture"]

    def xxmi_draws_from(self, second):
        """Nothing of XXMI's is drawn until ``second`` on the test clock, as after a reload that stalled the game."""
        def edit(doc):
            if self.clock.now >= second:
                doc["resources"].append(xxmi_buffer(150))
                doc["draws"][0]["vb"] = [[0, len(doc["resources"]) - 1, 16, 0, -1]]
        self.probe.edit_document = edit

    def test_frames_are_looked_at_until_xxmi_draws_what_it_created_anew(self):
        self.xxmi_draws_from(2.0 + 0.6)                  # 0.6 s after the settle time
        result, error = self.call(confirm_wait_ms=3000)
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], True)
        self.assertEqual(self.captures(), [("full", True), ("light", False), ("light", False), ("light", False), ("full", True)])
        self.assertEqual(result["frames_looked_at"], 5)
        self.assertEqual(result["waited_for_xxmi_ms"], 1000.0)
        self.assertEqual(len(self.probe.presses), 1)
        # Only the frame that was returned is kept: the probe can no longer read the first one.
        self.assertEqual(list(self.session.captures), [result["capture"]["frame"]])
        self.assertEqual(self.session.latest, result["capture"]["frame"])
        self.assertEqual(self.session.capture("latest").detail, "full")

    def test_waiting_for_xxmi_is_bounded_and_says_so(self):
        result, error = self.call(confirm_wait_ms=1000)
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], False)
        self.assertEqual(self.captures(), [("full", True)] + [("light", False)] * 4 + [("full", True)])
        self.assertEqual(result["waited_for_xxmi_ms"], 1250.0)
        self.assertIn("nor by the 5 later frame(s)", result["confirmation_note"])
        self.assertEqual(len(self.probe.presses), 1)

    def test_no_waiting_when_the_first_frame_settles_it(self):
        self.bind_to_game_draw(xxmi_buffer(150))
        result, error = self.call(confirm_wait_ms=3000)
        self.assertFalse(error, result)
        self.assertEqual((result["reload_confirmed"], result["frames_looked_at"], result["waited_for_xxmi_ms"]), (True, 1, None))
        self.assertEqual(self.captures(), [("full", True)])
        # XXMI creating resources on its own before the key press cannot be told apart by waiting either.
        self.probe.events.clear()
        self.probe.edit_document = None
        self.probe.receipt = {"xxmi_creates_before_press": 3}
        result, error = self.call(confirm_wait_ms=3000)
        self.assertFalse(error, result)
        self.assertEqual((result["reload_confirmed"], result["frames_looked_at"]), (False, 1))
        self.assertEqual(self.captures(), [("full", True)])

    def test_confirm_wait_out_of_range_is_refused_before_anything_happens(self):
        for bad in (-1, 15001, 2.5, "3000"):
            with self.subTest(bad=bad):
                text, error = mcp_call(self.server, "efmi_reload_and_capture", confirm_wait_ms=bad)
                self.assertTrue(error, text)
        self.assertEqual(self.probe.presses, [])

    def bind_to_game_draw(self, resource):
        def edit(doc):
            doc["resources"].append(resource)
            doc["draws"][0]["srv"] = [[4, 60, len(doc["resources"]) - 1]]
        self.probe.edit_document = edit

    def test_probe_presses_the_key_and_flags_are_forwarded(self):
        result, error = self.call(focus_game=False, restore_focus=False)
        self.assertFalse(error, result)
        self.assertEqual(result["sender"], "probe")
        self.assertEqual(self.probe.presses, [{"focus": False, "restore_focus": False}])
        self.server_sender.assert_not_called()
        events = [event[0] for event in self.probe.events]
        self.assertLess(events.index("release"), events.index("key"))
        self.assertLess(events.index("key"), events.index("capture"))

    def test_probe_that_keeps_hashes_by_address_gets_a_caution(self):
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIn("0.1.2", result["hash_caution"])
        self.assertIn("efmi_read_buffer", result["hash_caution"])
        self.probe.status_overrides["features"] = ["reload_key", "create_frames", "region_hash_per_object"]
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertNotIn("hash_caution", result)

    def test_settle_time_starts_when_the_game_presents_again(self):
        # The reload keeps the game from presenting for two seconds after the key went up at frame 220.
        self.probe.receipt = {"frame_at_key_up": 220}
        result, error = self.call(settle_ms=500)
        self.assertFalse(error, result)
        self.assertGreaterEqual(result["resumed_after_ms"], 2000)
        self.assertEqual(self.clock.sleeps[-1], 0.5)
        self.assertTrue(all(pause == 0.05 for pause in self.clock.sleeps[:-1]) and len(self.clock.sleeps) > 30)
        self.assertGreater(result["capture"]["frame"], 220)

    def test_a_game_that_never_presents_again_does_not_hang_the_tool(self):
        self.probe.receipt = {"frame_at_key_up": 10 ** 9}
        result, error = self.call(settle_ms=250)
        self.assertTrue(error, result)
        self.assertEqual((result["phase"], result["reload_requested"]), ("settle", True))
        self.assertIn("presented no frame", result["reason"])
        self.assertLessEqual(sum(self.clock.sleeps), reload_workflow.RESUME_TIMEOUT_S + 1)
        self.assertNotIn("capture", [event[0] for event in self.probe.events])

    def test_resource_created_after_the_key_confirms_the_reload(self):
        self.bind_to_game_draw(xxmi_buffer(150))
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], True)
        self.assertEqual(result["reload_evidence"]["key_down_frame"], 100)
        self.assertEqual(result["reload_evidence"]["resources_created_after_key"], 1)
        self.assertEqual(result["reload_evidence"]["examples"][0]["created_frame"], 150)
        self.assertIn("the reload took place", result["confirmation_note"])

    def test_nothing_new_from_xxmi_is_not_a_confirmation(self):
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_requested"], True)
        self.assertIs(result["reload_confirmed"], False)
        self.assertEqual(result["reload_evidence"]["resources_created_after_key"], 0)
        self.assertIn("no mod content is on screen", result["confirmation_note"])

    def test_a_later_frame_can_confirm_what_the_first_frame_after_the_reload_could_not(self):
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], False)          # no mod was drawing yet
        used = {"frame": 260, "resources": [xxmi_buffer(150)], "draws": [{"i": 0, "t": 4, "o": "xxmi", "srv": [[4, 60, 0]]}]}
        later = reload_workflow.confirm_on_frame(result, Capture(used))
        self.assertEqual((later["reload_confirmed"], later["confirmed_on_frame"]), (True, 260))
        self.assertEqual(later["reload_evidence"]["resources_created_after_key"], 1)
        self.assertIn("frame 260 uses", later["confirmation_note"])
        still_old = {"frame": 261, "resources": [xxmi_buffer(90)], "draws": used["draws"]}
        self.assertIsNone(reload_workflow.confirm_on_frame(result, Capture(still_old)))
        self.assertIsNone(reload_workflow.confirm_on_frame({**result, "reload_confirmed": True}, Capture(used)))
        self.assertIsNone(reload_workflow.confirm_on_frame({**result, "reload_requested": False}, Capture(used)))

    def test_resource_from_the_frame_of_the_key_or_earlier_is_not_evidence(self):
        for created in (50, 100):
            with self.subTest(created=created):
                self.bind_to_game_draw(xxmi_buffer(created))
                result, error = self.call()
                self.assertFalse(error, result)
                self.assertIs(result["reload_confirmed"], False)
                self.clock.now = 0.0

    def test_iniparams_texture_and_game_resources_are_not_evidence(self):
        for resource in (xxmi_buffer(150, kind="tex1d"), dict(xxmi_buffer(150), origin="game"),
                         dict(xxmi_buffer(150), known=False)):
            with self.subTest(resource=resource):
                self.bind_to_game_draw(resource)
                result, error = self.call()
                self.assertFalse(error, result)
                self.assertIs(result["reload_confirmed"], False)
                self.clock.now = 0.0

    def test_older_mod_buffers_drawn_by_xxmi_are_reported(self):
        def edit(doc):
            doc["resources"].append(xxmi_buffer(50))
            doc["draws"].append({"i": 1, "t": 2, "o": "xxmi", "ic": 3, "fi": 0, "fv": 0, "inst": 1,
                                 "topo": 4, "ib": [1, 57, 0, 0], "vb": [[0, 1, 12, 0, 0]]})
        self.probe.edit_document = edit
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], False)
        self.assertEqual(result["reload_evidence"]["xxmi_mesh_buffers_older_than_key"], 1)
        self.assertIn("did not take effect", result["confirmation_note"])

    def test_new_custom_shader_confirms_the_reload(self):
        def edit(doc):
            doc["shaders"] = [{"id": 0, "known": True, "type": "ps", "hash": "00000000000000aa",
                               "origin": "xxmi", "cf": 160},
                              {"id": 1, "known": True, "type": "vs", "hash": "00000000000000bb",
                               "origin": "game", "cf": 170}]
            doc["draws"][0]["sh"] = [1, -1, -1, -1, 0, -1]
        self.probe.edit_document = edit
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], True)
        self.assertEqual(result["reload_evidence"]["shaders_created_after_key"], 1)

    def test_resources_xxmi_was_already_creating_are_not_a_confirmation(self):
        self.bind_to_game_draw(xxmi_buffer(150))
        self.probe.receipt = {"xxmi_creates_before_press": 3}
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIs(result["reload_confirmed"], False)
        self.assertEqual(result["reload_evidence"]["resources_created_after_key"], 1)
        self.assertIn("already creating resources", result["confirmation_note"])

    def test_probe_without_creation_frames_gives_no_evidence(self):
        def edit(doc):
            for resource in doc["resources"]:
                resource.pop("cf", None)
        self.probe.edit_document = edit
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertIsNone(result["reload_evidence"])
        self.assertIs(result["reload_confirmed"], False)

    def test_probe_refusal_sends_nothing_and_captures_nothing(self):
        self.probe.receipt = {"code": "focus_refused", "message": "Windows did not let the game take the foreground",
                              "key_down_sent": False, "key_up_sent": False, "key_down_observed": False}
        result, error = self.call()
        self.assertTrue(error, result)
        self.assertEqual((result["phase"], result["input_error_code"]), ("input", "focus_refused"))
        self.assertIs(result["reload_requested"], False)
        self.assertEqual(result["sender"], "probe")
        self.assertNotIn("hint", result)
        self.assertEqual(len(self.probe.presses), 1)
        self.assertNotIn("capture", [event[0] for event in self.probe.events])
        self.assertIsNone(self.session.latest)

    def test_press_aborted_after_key_down_is_reported_as_requested(self):
        self.probe.receipt = {"code": "foreground_lost", "message": "Another window took the foreground",
                              "key_down_sent": True, "key_up_sent": True, "key_down_observed": True}
        result, error = self.call()
        self.assertTrue(error, result)
        self.assertIs(result["reload_requested"], True)
        self.assertIs(result["reload_confirmed"], False)
        self.assertNotIn("capture", [event[0] for event in self.probe.events])

    def test_broken_pipe_during_the_press_has_unknown_delivery(self):
        self.probe.receipt = ProbeError("the probe closed the pipe (did the game exit?)")
        result, error = self.call()
        self.assertTrue(error, result)
        self.assertIsNone(result["reload_requested"])
        self.assertEqual(len(self.probe.presses), 1)

    def test_restore_focus_must_be_a_boolean(self):
        result, error = self.call(restore_focus=1)
        self.assertTrue(error, result)
        self.assertEqual(self.probe.presses, [])
        self.assertEqual(self.session.latest, 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
