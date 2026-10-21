"""Offline checks of efmi_toggle_mods through the MCP call boundary.

The probe is a fake that behaves like EFMI: its key flips a switch, the frame
after it prints one of the two notification texts, and the mesh a mod overrides
is drawn by the game or by XXMI accordingly. No key is sent and no game or
installed EFMI is touched; each test owns a private folder.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import mods, reload_workflow, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint, ProbeError, ProbeNotFound  # noqa: E402

MESH = "64e97b80"
HUNTING = ("[Hunting]\nreload_config = no_modifiers VK_F10\nreload_fixes = no_modifiers VK_F10\n"
           "show_original = ctrl no_shift no_alt VK_F11\nanalyse_frame = no_ctrl shift no_alt VK_F11\n")
BINDING = "namespace = EFMIv1\n\n[KeyToggleMods]\nKey = no_modifiers VK_F11\nrun = CommandListToggleMods\n"
MAIN = ('namespace = EFMIv1\n[Constants]\nglobal $enable_mods = 1\n[CommandListToggleMods]\n$enable_mods = !$enable_mods\n'
        '[ResourceModsDisplayEnabled]\ntype = Buffer\ndata = "Mods Display: Enabled"\n'
        '[ResourceModsDisplayDisabled]\ntype = Buffer\ndata = "Mods Display: Disabled"\n')
TEXTS = {True: b"Mods Display: Enabled", False: b"Mods Display: Disabled"}


class FakeProbe:
    def __init__(self, root: Path):
        self.root = root
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.features = ["reload_key", "create_frames", "mods_key"]
        self.enabled = True          # EFMI's $enable_mods
        self.stuck = False           # the key has no effect
        self.notice = True           # the frame after the key prints the notification
        self.notice_origin = "xxmi"  # how the probe's heuristic labels the draw that prints it
        self.mesh_in_view = True     # the mesh that a mod overrides is on screen
        self.frame = 100
        self.live_on = False
        self.events: list[str] = []
        self.refusals: list[dict] = []   # receipts for the next presses, instead of pressing
        self.capture_error = None

    def status(self):
        self.frame += 1      # the game keeps presenting
        return {"pid": self.endpoint.pid, "exe": r"C:\Game\Endfield.exe", "probe": "0.1.3", "features": self.features,
                "attached": True, "swap_chain": True, "frame": self.frame, "resolution": [1280, 720],
                "xxmi": {"present": True, "path": str(self.root / "d3d11.dll")},
                "capture": {"live": self.live_on, "recording": self.live_on},
                "capture_target": {"device": "0x100", "context": "0x200", "swap_chain": "0x300"}}

    def press(self, key, **kwargs):
        self.events.append("key")
        receipt = {"code": "ok", "message": "", "sender": "probe", "key": key, "key_down_sent": True,
                   "key_down_observed": True, "key_up_sent": True, "frames_while_held": 2, "released_on": "two_frames",
                   "focus_changed": True, "focus_restored": True, "frame_at_key_down": self.frame + 1,
                   "frame_at_key_up": self.frame + 3, "xxmi_creates_before_press": 0, **kwargs}
        if self.refusals:
            receipt.update(self.refusals.pop(0))
        self.frame += 3
        return receipt

    def mods_key(self, **kwargs):
        receipt = self.press("F11", action="toggle_mods")
        if receipt["code"] == "ok" or receipt["key_down_sent"]:
            self.enabled = self.enabled if self.stuck else not self.enabled
            self.printing = self.notice
        return receipt

    def reload_key(self, **kwargs):
        self.enabled, self.printing = True, False
        return self.press("F10", action="reload")

    def release(self):
        self.events.append("release")

    def live(self, enable=True, **kwargs):
        self.events.append("live")
        self.live_on = enable
        return {"frame": self.frame}

    def capture(self, **kwargs):
        self.events.append("capture")
        if self.capture_error:
            raise self.capture_error
        self.frame += 1
        text = TEXTS[self.enabled]
        doc = {"frame": self.frame, "held": True, "detail": kwargs.get("detail", "full"), "resolution": [1280, 720],
               "resources": [{"kind": "buffer", "size": 6, "bind": 2, "hash": "aaaaaaaa", "known": True},
                             {"kind": "buffer", "size": len(text) + 3, "bind": 8, "known": True, "origin": "xxmi"},
                             {"kind": "buffer", "size": 4096, "bind": 8, "known": True, "origin": "xxmi"}],
               "regions": [[0, 0, 6, MESH, "ok"], [0, 0, 6, "bbbbbbbb", "ok"]],
               # Scenery no mod refers to, then the mesh a mod overrides: issued by XXMI while mods are on,
               # by the game while they are off, with the game's shaders either way.
               "draws": [{"i": 0, "t": 4, "o": "game", "ic": 3, "fi": 0, "fv": 0, "inst": 1, "topo": 4, "ib": [0, 57, 0, 1],
                          "sh": [0, -1, -1, -1, 1, -1]}],
               "stats": {"record_ms": 0.01}}
        if self.mesh_in_view:
            doc["draws"].append({"i": 2, "t": 4, "o": "xxmi" if self.enabled else "game", "ic": 3, "fi": 0, "fv": 0,
                                 "inst": 1, "topo": 4, "ib": [0, 57, 0, 0], "sh": [0, -1, -1, -1, 1, -1]})
        if getattr(self, "printing", False):
            doc["draws"].append({"i": 1, "t": 8, "o": self.notice_origin, "tg": [1, 1, 1],
                                 "srv": [[5, 100, 2], [5, 113, 1]]})
        self.printing = False
        return doc, b""

    def read_buffer(self, capture_frame, res, offset=0, size=0, **kwargs):
        self.events.append("read")
        return {}, (TEXTS[self.enabled] + b"\0\0\0")[offset:offset + size]


class ToggleTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="toggle-test-", dir=build)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Core" / "EFMI").mkdir(parents=True)
        (self.root / "Mods" / "Some Mod").mkdir(parents=True)
        (self.root / "d3d11.dll").touch()
        self.write("d3dx.ini", HUNTING)
        self.write("Core/EFMI/KeyBindings.ini", BINDING)
        self.write("Core/EFMI/main.ini", MAIN)
        self.write("Mods/Some Mod/mod.ini", f"[TextureOverrideBody]\nhash = {MESH}\nhandling = skip\n")
        self.probe = FakeProbe(self.root)
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = self.probe
        self.session.add_capture(Capture(self.probe.capture()[0]))
        self.probe.events.clear()
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", self.root / "probe.dll")
        # Even a regression that bypasses session.client must never open a user's probe.
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
        self.session.mods.invalidate() if hasattr(self, "session") else None

    def call(self, name="efmi_toggle_mods", **arguments):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        text = reply["result"]["content"][0]["text"]
        return (json.loads(text) if text.lstrip().startswith("{") else text), reply["result"]["isError"]

    def keys(self):
        return self.probe.events.count("key")

    def assert_no_key(self, result, error, expected=""):
        self.assertTrue(error, result)
        self.assertEqual(self.keys(), 0)
        self.assertNotIn("capture", self.probe.events)
        self.assertEqual((result["phase"], result["key_sent"], result["presses"]), ("preflight", False, 0))
        self.assertIn(expected, result["reason"])
        self.assertEqual(list(self.session.captures), [101])

    # ---------------------------------------------------------------- success
    def test_tool_is_registered_as_changing_the_game(self):
        listed = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        entry = next(tool for tool in listed if tool["name"] == "efmi_toggle_mods")
        self.assertFalse(entry["annotations"]["readOnlyHint"])
        self.assertFalse(entry["inputSchema"]["additionalProperties"])
        self.assertEqual(entry["inputSchema"]["properties"]["state"]["enum"], ["toggle", "on", "off"])

    def test_toggle_presses_once_and_reads_the_state_efmi_printed(self):
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertEqual((result["status"], result["mods_enabled"], result["presses"]), ("ok", False, 1))
        self.assertEqual(result["state_evidence"]["text"], "Mods Display: Disabled")
        self.assertEqual(result["state_evidence"]["slot"], "cs-t113")
        self.assertEqual(self.probe.events[:2], ["key", "capture"])
        self.assertEqual(result["input"][0]["key"], "F11")
        # The game draws the overridden mesh itself now, and the frame is the session's latest.
        self.assertEqual(result["drawn"]["game_draws_of_meshes_that_mods_override"], 1)
        self.assertEqual(result["drawn"]["those_meshes"], [{"ib": MESH, "draws": 1}])
        self.assertEqual(self.session.latest, result["capture"]["frame"])
        self.assertIn("OFF", result["note"])
        self.assertEqual(self.session.mods_display, {"enabled": False, "frame": result["capture"]["frame"]})

    def test_earlier_captures_and_live_mode_are_left_alone(self):
        self.probe.live_on = True
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertEqual(list(self.session.captures), [101, result["capture"]["frame"]])
        self.assertTrue(result["live"] and self.probe.live_on)
        self.assertFalse({"release", "live"} & set(self.probe.events))
        text, error = self.call("efmi_diff_captures", a=101)
        self.assertFalse(error, text)
        self.assertIn(f"only in {result['capture']['frame']}: 1", text)

    def test_wanted_state_takes_one_press_when_the_first_leads_to_it(self):
        result, error = self.call(state="off")
        self.assertFalse(error, result)
        self.assertEqual((result["mods_enabled"], result["presses"], self.keys()), (False, 1, 1))

    def test_wanted_state_takes_a_second_press_when_the_first_leads_away_from_it(self):
        for state, start in (("on", True), ("off", False)):
            with self.subTest(state=state):
                self.probe.enabled = start
                self.probe.events.clear()
                result, error = self.call(state=state)
                self.assertFalse(error, result)
                self.assertEqual((result["mods_enabled"], result["presses"], self.keys()), (start, 2, 2))
                self.assertEqual(self.probe.events.count("capture"), 2)
                self.assertIn("ON" if start else "OFF", result["note"])

    def test_no_key_when_the_requested_state_is_known_and_the_frame_agrees(self):
        for state, enabled in (("off", False), ("on", True)):
            with self.subTest(state=state):
                self.call(state=state)               # EFMI's notification is read here
                self.probe.events.clear()
                result, error = self.call(state=state)
                self.assertFalse(error, result)
                self.assertEqual((result["presses"], result["mods_enabled"], self.keys()), (0, enabled, 0))
                self.assertEqual(result["state_evidence"]["source"], "no key pressed")
                self.assertIn("No key was pressed", result["note"])
                self.assertEqual(self.session.latest, result["capture"]["frame"])

    def test_key_is_pressed_when_the_frame_contradicts_what_was_remembered(self):
        self.call(state="off")
        self.probe.enabled = True                    # someone pressed F11 by hand
        self.probe.events.clear()
        result, error = self.call(state="off")
        self.assertFalse(error, result)
        self.assertEqual((result["presses"], result["mods_enabled"], self.keys()), (1, False, 1))

    def test_frame_without_a_modded_mesh_does_not_stand_in_for_the_key(self):
        self.call(state="off")
        self.probe.mesh_in_view = False
        self.probe.events.clear()
        result, error = self.call(state="off")
        self.assertFalse(error, result)
        self.assertEqual((result["presses"], result["mods_enabled"], self.keys()), (2, False, 2))

    def test_text_is_recognised_from_what_the_installed_core_defines(self):
        wording = "\u6a21\u7ec4\u663e\u793a\uff1a\u5173\u95ed"      # as a localised core might print it; not ASCII on purpose
        self.write("Core/EFMI/main.ini", MAIN.replace("Mods Display: Disabled", wording))
        TEXTS_BEFORE = dict(TEXTS)
        TEXTS[False] = wording.encode("utf-8")
        self.addCleanup(TEXTS.update, TEXTS_BEFORE)
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertEqual((result["mods_enabled"], result["state_evidence"]["text"]), (False, wording))

    def test_text_is_found_even_if_its_draw_is_not_labelled_as_xxmi(self):
        self.probe.notice_origin = "game"
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertEqual((result["mods_enabled"], result["state_evidence"]["draw"]), (False, 1))

    def test_checking_an_export_is_refused_while_mods_are_known_to_be_off(self):
        project = self.root / "project"
        (project / "workplace").mkdir(parents=True)
        (project / "workplace" / "EFMI_MergedSkeleton.json").write_text("{}", encoding="utf-8")
        text, error = self.call("efmi_verify_exported_mod", project_dir=str(project), action="check")
        self.assertTrue(error and "no valid export" in text, text)
        self.call(state="off")
        text, error = self.call("efmi_verify_exported_mod", project_dir=str(project), action="check")
        self.assertTrue(error and "switched off by efmi_toggle_mods" in text, text)

    def test_unreadable_notification_means_unknown_and_no_second_press(self):
        cases = {"not printed": lambda: setattr(self.probe, "notice", False),
                 "core without the texts": lambda: self.write("Core/EFMI/main.ini", "[Constants]\nglobal $enable_mods = 1\n")}
        for name, arrange in cases.items():
            with self.subTest(name):
                self.probe.events.clear()
                arrange()
                result, error = self.call(state="on")
                self.assertFalse(error, result)
                self.assertEqual((result["status"], result["mods_enabled"], result["presses"]), ("state_unknown", None, 1))
                self.assertEqual(self.keys(), 1)
                self.assertIn("unknown", result["note"])
                self.assertIsNone(self.session.mods_display)

    def test_other_sections_that_f11_alone_triggers_are_listed(self):
        self.write("Mods/Some Mod/mod.ini", f"[TextureOverrideBody]\nhash = {MESH}\n\n[KeyOutfit]\nkey = VK_F11\n"
                                            "type = cycle\n$outfit = 0,1\n\n[KeyOther]\nkey = ctrl VK_F11\n$x = 0,1\n")
        result, error = self.call()
        self.assertFalse(error, result)
        self.assertEqual(result["other_f11_bindings"], [{"ini": str(Path("Mods/Some Mod/mod.ini")), "section": "KeyOutfit"}])

    def test_capture_after_switching_off_says_so_until_a_reload(self):
        self.call(state="off")
        text, error = self.call("efmi_capture_frame", detail="light")
        self.assertIn("switched all mods OFF", text)
        with patch.object(reload_workflow.time, "sleep"):
            result, error = self.call("efmi_reload_and_capture", settle_ms=250)
        self.assertFalse(error, result)
        self.assertIsNone(self.session.mods_display)
        text, error = self.call("efmi_capture_frame", detail="light")
        self.assertNotIn("switched all mods OFF", text)

    # ---------------------------------------------------------------- refusals before any key
    def test_changed_or_missing_binding_is_refused(self):
        bindings = {"other key": BINDING.replace("VK_F11", "VK_F9"),
                    "needs a modifier": BINDING.replace("no_modifiers VK_F11", "ctrl VK_F11"),
                    "any modifiers": BINDING.replace("no_modifiers VK_F11", "VK_F11"),
                    "two keys": BINDING + "key = VK_F9\n",
                    "other command list": BINDING.replace("CommandListToggleMods", "CommandListSomethingElse"),
                    "two sections": BINDING + "\n[KeyToggleMods]\nKey = no_modifiers VK_F11\nrun = CommandListToggleMods\n",
                    "no section": "namespace = EFMIv1\n"}
        for name, text in bindings.items():
            with self.subTest(name):
                self.write("Core/EFMI/KeyBindings.ini", text)
                self.assert_no_key(*self.call(), expected="KeyToggleMods")
        (self.root / "Core" / "EFMI" / "KeyBindings.ini").unlink()
        self.assert_no_key(*self.call(), expected="KeyToggleMods")

    def test_hunting_function_on_plain_f11_is_refused(self):
        for line in ("analyse_frame = VK_F11", "show_original = no_modifiers VK_F11", "take_screenshot = no_ctrl no_alt VK_F11"):
            with self.subTest(line):
                self.write("d3dx.ini", HUNTING + line + "\n")
                self.assert_no_key(*self.call(), expected=line.split()[0])

    def test_f11_with_a_modifier_or_a_second_key_elsewhere_is_fine(self):
        self.write("d3dx.ini", HUNTING + "next_pixelshader = VK_NUMPAD1 VK_F11\n; analyse_frame = VK_F11\n")
        result, error = self.call()
        self.assertFalse(error, result)

    def test_probe_that_cannot_press_f11_asks_for_a_game_restart(self):
        self.probe.features = ["reload_key", "create_frames"]
        result, error = self.call()
        self.assert_no_key(result, error, expected="restart the game")

    def test_invalid_arguments_are_refused(self):
        for arguments in ({"state": "maybe"}, {"timeout_ms": 10}, {"focus_game": "yes"}, {"cb_data": 1}):
            with self.subTest(arguments):
                result, error = self.call(**arguments)
                self.assertTrue(error, result)
                self.assertEqual(self.keys(), 0)

    # ---------------------------------------------------------------- failures around the key
    def test_press_the_probe_refused_is_not_repeated(self):
        self.probe.refusals = [{"code": "focus_refused", "message": "no", "key_down_sent": False, "key_up_sent": False}]
        result, error = self.call(state="off")
        self.assertTrue(error, result)
        self.assertEqual((result["phase"], result["input_error_code"], result["key_sent"], result["presses"]),
                         ("input", "focus_refused", False, 0))
        self.assertEqual(self.keys(), 1)
        self.assertNotIn("capture", self.probe.events)

    def test_press_aborted_while_the_key_was_down_leaves_the_state_unknown(self):
        self.probe.refusals = [{"code": "foreground_lost", "message": "lost", "key_down_sent": True, "key_up_sent": True}]
        result, error = self.call()
        self.assertTrue(error, result)
        self.assertEqual((result["key_sent"], result["presses"], result["mods_enabled"]), (True, 1, None))
        self.assertIsNone(self.session.mods_display)

    def test_capture_failure_after_the_key_is_reported_without_another_press(self):
        self.probe.capture_error = ProbeError("capture timed out")
        result, error = self.call(state="off")
        self.assertTrue(error, result)
        self.assertEqual((result["phase"], result["key_sent"], result["presses"], result["mods_enabled"]),
                         ("capture", True, 1, None))
        self.assertEqual(self.keys(), 1)
        self.assertIn("efmi_capture_frame", result["next"])

    def test_refused_second_press_keeps_what_the_first_one_showed(self):
        self.probe.refusals = [{}, {"code": "keys_held", "message": "Shift", "key_down_sent": False, "key_up_sent": False}]
        result, error = self.call(state="on")      # mods are on: the first press switches them off
        self.assertTrue(error, result)
        self.assertEqual((result["presses"], result["mods_enabled"], result["input_error_code"]), (1, False, "keys_held"))
        self.assertEqual(self.session.mods_display["enabled"], False)

    def test_no_third_press_when_efmi_keeps_reporting_the_other_state(self):
        self.probe.stuck = True
        result, error = self.call(state="off")
        self.assertTrue(error, result)
        self.assertEqual((self.keys(), result["presses"], result["mods_enabled"]), (2, 2, True))
        self.assertIn("after two presses", result["reason"])


class BindingTests(unittest.TestCase):
    def test_plain_key_means_no_modifier_and_no_second_key(self):
        cases = {"VK_F11": True, "no_modifiers VK_F11": True, "no_ctrl no_alt vk_f11 ; note": True,
                 "ctrl no_shift no_alt VK_F11": False, "no_ctrl shift no_alt VK_F11": False,
                 "VK_NUMPAD1 VK_F11": False, "no_modifiers VK_F10": False, "": False}
        for value, plain in cases.items():
            with self.subTest(value):
                self.assertEqual(mods.is_plain_key(mods.key_tokens(value), "vk_f11"), plain)


if __name__ == "__main__":
    unittest.main(verbosity=1)
