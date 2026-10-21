"""Offline checks of efmi_live_history and the trigger tools through the MCP call boundary.

The probe is a fake with a scripted stream of 300 frames at 60 fps: a mesh drawn in every frame,
a flash drawn in two bursts of four frames, a line that is new from frame 1280 on, and a volatile
index buffer for a few frames. Its live_summary adds the stream up with summarize_frames, which is
what the server itself does for a probe without it; the real probe is held against that in
continuous_test.py. Its trigger answers from a script. No game is touched and nothing is written.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import live_tools, tools  # noqa: E402
from efmi_mcp.live_tools import parse_hashes, summarize_frames  # noqa: E402
from efmi_mcp.mcp_stdio import ToolError  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint, ProbeNotFound  # noqa: E402

VS = "1111111111111111"
PS_SCENE = "2222222222222222"
PS_FLASH = "ffffffffffffffff"
PS_NEW = "cccccccccccccccc"
SCENE = [2, "game", "aaaaaaaa", "ok", "a0a0a0a0", 300, 0, 0, VS, PS_SCENE]
FLASH = [2, "game", "bbbbbbbb", "ok", "b0b0b0b0", 150, 0, 0, VS, PS_FLASH]
NEW = [2, "game", "dddddddd", "ok", "d0d0d0d0", 90, 0, 0, VS, PS_NEW]
VOLATILE = [2, "game", None, "volatile", "e0e0e0e0", 60, 0, 0, VS, PS_SCENE]
TEXT = [2, "xxmi", "99999999", "ok", None, 6, 0, 0, "3333333333333333", "4444444444444444"]


def stream() -> list[dict]:
    frames = []
    for n in range(1000, 1300):
        items = [SCENE + [1, 1], TEXT + [1, 1]]
        if 1200 <= n <= 1203 or 1250 <= n <= 1253:
            items.append(FLASH + [1, 1])
        if n >= 1280:
            items.append(NEW + [2, 6])
        if 1100 <= n <= 1110:
            items.append(VOLATILE + [1, 1])
        frames.append({"frame": n, "t_ms": 50000 + round((n - 1000) * 1000 / 60), "draws": 10 + len(items),
                       "game_draws": 9 + len(items), "volatile": 0, "cost_ms": 0.1, "readback_bytes": 0,
                       "items": items})
    return frames


def flash_doc(frame: int) -> dict:
    return {"frame": frame, "held": True, "detail": "full", "resolution": [1280, 720],
            "resources": [{"kind": "buffer", "size": 300, "bind": 2, "hash": "b0b0b0b0", "known": True}],
            "shaders": [{"hash": VS, "known": True}, {"hash": PS_FLASH, "known": True}],
            "regions": [[0, 0, 300, "bbbbbbbb", "ok"]],
            "draws": [{"i": 0, "t": 2, "o": "game", "ic": 150, "fi": 0, "topo": 4, "ib": [0, 57, 0, 0],
                       "sh": [0, -1, -1, -1, 1, -1]}],
            "stats": {"record_ms": 0.2}}


class FakeProbe:
    def __init__(self):
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.features = ["reload_key", "mods_key", "contents", "live_summary", "trigger"]
        self.frames = stream()
        self.calls: list[tuple] = []
        self.waits: list[tuple] = []          # what trigger_wait answers, in turn
        self.trigger = "off"

    def status(self):
        return {"probe": "0.1.10", "features": self.features, "frame": 1300,
                "capture": {"live": True, "region_cache": 0, "live_frames": len(self.frames), "trigger": self.trigger}}

    def live(self, **kwargs):
        self.calls.append(("live", kwargs))
        return {"frame": 1300}

    def live_get(self, since=0, max_frames=0):
        self.calls.append(("live_get", since, max_frames))
        frames = [f for f in self.frames if f["frame"] > since]
        return 1300, frames[-max_frames:] if max_frames else frames

    def live_summary(self, timeline=True, **kwargs):
        self.calls.append(("live_summary", kwargs))
        summ = summarize_frames(self.frames, **kwargs)
        if not timeline:
            del summ["timeline"]
        return 1300, summ

    def trigger_arm(self, **spec):
        self.calls.append(("trigger_arm", spec))
        self.trigger = "arming"
        return {"state": "arming", "id": 7, "frame": 1300}

    def trigger_wait(self, wait_ms=0):
        self.calls.append(("trigger_wait", wait_ms))
        if self.waits:
            return self.waits.pop(0)
        return {"state": self.trigger, "id": 7}, None, b""

    def trigger_cancel(self):
        self.calls.append(("trigger_cancel",))
        self.trigger = "off"
        return {"state": "off", "id": 7}


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="continuous-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Mods").mkdir()
        self.probe = FakeProbe()
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = self.probe
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", self.root / "probe.dll")
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def call(self, name, **arguments):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        return reply["result"]["content"][0]["text"], reply["result"]["isError"]

    def armed(self) -> dict:
        return [c for c in self.probe.calls if c[0] == "trigger_arm"][-1][1]

    # ---------------------------------------------------------------- history
    def test_transient_lines_grouped_by_when_they_appeared(self):
        text, err = self.call("efmi_live_history", seconds=5)
        self.assertFalse(err, text)
        self.assertIn("window: frames 1000-1299, 300 frames over 4.98 s (60 fps)", text)
        self.assertIn("added up by the probe", text)
        self.assertNotIn("ib=aaaaaaaa", text)            # drawn in every frame
        self.assertNotIn("ib=99999999", text)            # XXMI's own, origin 'game' by default
        self.assertIn("at +1.67s (frame 1100): 1 lines appear, last drawn at +1.83s", text)
        self.assertIn("ib=volatile", text)
        self.assertIn("at +3.33s (frame 1200): 1 lines appear, last drawn at +4.22s", text)   # both flashes
        self.assertIn("+3.33s..+4.22s 8/300 frames", text)
        self.assertIn("at +4.67s (frame 1280): 1 lines appear, still drawn at the end", text)
        self.assertIn("draws<=2 inst<=6", text)
        self.assertIn("no stable hash", text)
        order = [text.index(h) for h in ("ib=volatile", "ib=bbbbbbbb", "ib=dddddddd")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.probe.calls[-1], ("live_summary", {"first": 0, "last": 0, "seconds": 5.0,
                                                                 "base_seconds": 0.0}))

    def test_new_lines_against_a_baseline(self):
        text, err = self.call("efmi_live_history", seconds=0.5, baseline_seconds=1, show="new")
        self.assertFalse(err, text)
        self.assertIn("window: frames 1269-1299, 31 frames over 0.50 s", text)     # both ends count
        self.assertIn("baseline: frames 1209-1268 (60 frames)", text)
        self.assertIn("new: 1 lines not drawn in any of the 60 baseline frames", text)
        self.assertIn("ib=dddddddd", text)
        self.assertIn("[not in baseline]", text)
        self.assertNotIn("ib=bbbbbbbb", text)            # in the baseline (frames 1250-1253)

    def test_new_needs_a_baseline(self):
        text, err = self.call("efmi_live_history", seconds=2, show="new")
        self.assertTrue(err)
        self.assertIn("baseline_seconds", text)

    def test_a_hash_shows_every_line_that_has_it(self):
        text, err = self.call("efmi_live_history", seconds=5, hash=PS_SCENE)
        self.assertFalse(err, text)
        self.assertIn("all: 2 lines", text)
        self.assertIn("ib=aaaaaaaa", text)
        self.assertIn("300/300 frames", text)
        self.assertIn("ib=volatile", text)
        text, err = self.call("efmi_live_history", hash="xyz")
        self.assertTrue(err)

    def test_origin_and_frames(self):
        text, err = self.call("efmi_live_history", seconds=0, first=1200, last=1203, origin="xxmi", show="all")
        self.assertFalse(err, text)
        self.assertIn("window: frames 1200-1203, 4 frames", text)
        self.assertIn("ib=99999999", text)
        self.assertNotIn("ib=aaaaaaaa", text)

    def test_timeline(self):
        text, err = self.call("efmi_live_history", seconds=1, timeline=True)
        self.assertFalse(err, text)
        self.assertIn("timeline (per quarter second", text)
        self.assertIn("+0.00s  15 frames", text)

    def test_older_probe_is_added_up_by_the_server_alike(self):
        probe_text, _ = self.call("efmi_live_history", seconds=5, baseline_seconds=0)
        self.probe.features.remove("live_summary")
        server_text, err = self.call("efmi_live_history", seconds=5, baseline_seconds=0)
        self.assertFalse(err, server_text)
        self.assertIn("added up by the server (probe before 0.1.10)", server_text)
        self.assertEqual(probe_text.split("\n")[1:], server_text.split("\n")[1:])
        self.assertIn(("live_get", 0, 0), self.probe.calls)

    def test_empty_stream(self):
        self.probe.frames = []
        text, err = self.call("efmi_live_history")
        self.assertTrue(err)
        self.assertIn("efmi_live_start", text)

    def test_summarize_frames_with_seconds_and_baseline(self):
        s = summarize_frames(stream(), seconds=1.0, base_seconds=0.5)
        self.assertEqual((s["window"]["first"], s["window"]["last"], s["window"]["frames"]), (1239, 1299, 61))
        self.assertEqual((s["baseline"]["first"], s["baseline"]["last"], s["baseline"]["frames"]), (1209, 1238, 30))
        rows = {it[2]: dict(zip(live_tools.ROW, it)) for it in s["items"]}
        self.assertEqual((rows["bbbbbbbb"]["frames"], rows["bbbbbbbb"]["base"]), (4, 0))
        self.assertEqual((rows["dddddddd"]["frames"], rows["dddddddd"]["max_inst"], rows["dddddddd"]["draws"]),
                         (20, 6, 40))
        self.assertEqual((rows["aaaaaaaa"]["frames"], rows["aaaaaaaa"]["base"]), (61, 30))
        self.assertNotIn(None, rows)                    # the volatile line is outside both stretches
        self.assertEqual(len(s["timeline"]), 61)

    # ---------------------------------------------------------------- live start
    def test_live_start_keeps_thirty_seconds_by_default(self):
        self.call("efmi_live_start")
        self.assertEqual(self.probe.calls[-1][1]["keep_frames"], live_tools.KEEP_FRAMES)
        text, err = self.call("efmi_live_start", keep_frames=99999)
        self.assertFalse(err, text)
        self.assertEqual(self.probe.calls[-1][1]["keep_frames"], 3600)

    # ---------------------------------------------------------------- trigger
    def test_hashes_are_checked(self):
        self.assertEqual(parse_hashes("0xAABBCCDD, aabbccdd 11223344", 8, "ib"), ["aabbccdd", "11223344"])
        with self.assertRaises(ToolError):
            parse_hashes("abc", 8, "ib")
        text, err = self.call("efmi_trigger_arm", ib="1234")
        self.assertTrue(err)
        self.assertIn("8 hex digits", text)
        text, err = self.call("efmi_trigger_arm")
        self.assertTrue(err)
        self.assertIn("watch for something", text)
        self.assertFalse([c for c in self.probe.calls if c[0] == "trigger_arm"])

    def test_older_probe_has_no_trigger(self):
        self.probe.features.remove("trigger")
        text, err = self.call("efmi_trigger_arm", shader=PS_FLASH)
        self.assertTrue(err)
        self.assertIn("0.1.10", text)
        self.assertIn("efmi_live_history", text)

    def test_arm_passes_the_spec_and_capture_options(self):
        text, err = self.call("efmi_trigger_arm", ib="BBBBBBBB", shader=PS_FLASH, delay_frames=2, expire_s=30,
                              contents="inputs", cb_data=True)
        self.assertFalse(err, text)
        spec = self.armed()
        self.assertEqual((spec["ib"], spec["shader"], spec["new_pairs"], spec["origin"]),
                         (["bbbbbbbb"], [PS_FLASH], False, ""))
        self.assertEqual((spec["delay_frames"], spec["expire_ms"]), (2, 30000))
        self.assertEqual(spec["capture"]["detail"], "full")
        self.assertTrue(spec["capture"]["cb_data"])
        self.assertIn("tex", spec["capture"]["contents"]["inputs"])
        self.assertIn("trigger 7 armed at frame 1300", text)
        self.assertIn("next frame + 2 is recorded (full, contents inputs, constant buffers)", text)
        self.assertIn("efmi_trigger_wait", text)

    def test_new_pairs_counts_the_games_draws_by_default(self):
        self.call("efmi_trigger_arm", new_pairs=True)
        self.assertEqual((self.armed()["origin"], self.armed()["baseline_frames"]), ("game", 120))
        self.call("efmi_trigger_arm", new_pairs=True, origin="any")
        self.assertEqual(self.armed()["origin"], "")

    def test_outputs_follow_the_shaders(self):
        self.call("efmi_trigger_arm", shader=PS_FLASH, contents="outputs")
        self.assertEqual(self.armed()["capture"]["contents"]["match"], [{"sh": PS_FLASH}])
        text, err = self.call("efmi_trigger_arm", new_pairs=True, contents="outputs")
        self.assertTrue(err)
        self.assertIn("contents='inputs'", text)

    def test_wait_hands_over_the_capture(self):
        fired = {"frame": 1300, "draw": 0, "type": 2, "origin": "game", "count": 150, "what": "shader",
                 "shader": PS_FLASH, "vs": VS, "ps": PS_FLASH, "late": False, "ms_ago": 40}
        self.probe.waits = [({"state": "armed", "id": 7, "watched_frames": 50, "armed_ms_ago": 800,
                              "armed_frame": 1250}, None, b""),
                            ({"state": "done", "id": 7, "fired": fired, "capture_frame": 1301, "watched_frames": 51},
                             flash_doc(1301), b"")]
        self.call("efmi_trigger_arm", shader=PS_FLASH)
        text, err = self.call("efmi_trigger_wait", wait_s=120)
        self.assertFalse(err, text)
        self.assertIn(f"fired on shader {PS_FLASH}: frame 1300, draw #0", text)
        self.assertIn("recorded frame 1301 (capture 1301, now the latest), 1 frame(s) after", text)
        self.assertIn("the recorded frame draws it 1 times: draws [0]", text)
        self.assertIn("capture 1301 (full): 1 draws", text)
        self.assertEqual(self.session.latest, 1301)
        waits = [c[1] for c in self.probe.calls if c[0] == "trigger_wait"]
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(0 < w <= 60000 for w in waits))

    def test_wait_says_when_the_frame_no_longer_draws_it(self):
        fired = {"frame": 1300, "what": "ib", "ib": "eeeeeeee", "late": True, "count": 150, "type": 2,
                 "origin": "game", "draw": 3}
        self.probe.waits = [({"state": "done", "id": 7, "fired": fired}, flash_doc(1303), b"")]
        self.call("efmi_trigger_arm", ib="eeeeeeee")
        text, err = self.call("efmi_trigger_wait", wait_s=1)
        self.assertFalse(err, text)
        self.assertIn("read back, so a later frame was recorded", text)
        self.assertIn("3 frame(s) after", text)
        self.assertIn("WARNING: the recorded frame does not draw it", text)

    def test_wait_without_a_capture(self):
        self.probe.waits = [({"state": "expired", "id": 7, "armed_ms_ago": 600000, "watched_frames": 36000},
                             None, b"")]
        text, _ = self.call("efmi_trigger_wait", wait_s=0)
        self.assertIn("expired after 600 s", text)
        self.assertEqual([c[1] for c in self.probe.calls if c[0] == "trigger_wait"], [0])
        text, _ = self.call("efmi_trigger_wait", wait_s=0)
        self.assertIn("no trigger is armed", text)
        self.probe.waits = [({"state": "fired", "id": 7, "watched_frames": 9, "armed_ms_ago": 100, "armed_frame": 1,
                              "fired": {"frame": 9, "what": "shader", "shader": PS_FLASH}}, None, b"")]
        text, _ = self.call("efmi_trigger_wait", wait_s=0)
        self.assertIn("trigger 7 is fired", text)
        self.assertIn("call efmi_trigger_wait again", text)

    def test_cancel_keeps_a_frame_already_recorded(self):
        self.probe.waits = [({"state": "done", "id": 7, "fired": {"frame": 1300}}, flash_doc(1301), b"")]
        text, err = self.call("efmi_trigger_cancel")
        self.assertFalse(err, text)
        self.assertIn("kept as the latest capture", text)
        self.assertEqual(self.session.latest, 1301)
        self.probe.trigger = "armed"
        text, _ = self.call("efmi_trigger_cancel")
        self.assertIn("trigger 7 cancelled (it was armed)", text)
        self.assertEqual(self.probe.calls[-1], ("trigger_cancel",))

    def test_tools_are_listed(self):
        listed = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        by_name = {t["name"]: t for t in listed}
        for name in ("efmi_live_history", "efmi_trigger_arm", "efmi_trigger_wait", "efmi_trigger_cancel"):
            self.assertIn(name, by_name)
        self.assertTrue(by_name["efmi_live_history"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["efmi_trigger_arm"]["annotations"]["readOnlyHint"])
        json.dumps(by_name["efmi_trigger_arm"]["inputSchema"])


if __name__ == "__main__":
    unittest.main()
