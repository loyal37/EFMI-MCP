"""Offline checks of the recording tools (efmi_record, efmi_record_list, efmi_record_history,
efmi_record_keyframe) through the MCP call boundary.

A recording folder is written here by hand, laid out as the probe writes it: 600 frames at 60 fps; a scene
mesh drawn throughout; an effect seen three times (a mesh, and a particle quad drawn from index data the CPU
rewrites, recorded as two lines with different ranges); an object that comes into view once and stays; XXMI's
own text; keyframes at the start, for the effect's new shader pair and on time. A second folder was not stopped
(the game ended): its open runs come from open-runs.json. The probe is a fake; nothing is written outside a
temporary folder.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import record_tools, tools  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint, ProbeNotFound  # noqa: E402

VS = "1111111111111111"
PS_SCENE = "2222222222222222"
PS_FX = "ffffffffffffffff"
PS_QUAD = "eeeeeeeeeeeeeeee"
LINES = {
    0: [2, "game", "aaaaaaaa", "ok", "a0a0a0a0", 300, 0, 0, VS, PS_SCENE],
    1: [2, "game", "bbbbbbbb", "ok", "b0b0b0b0", 150, 0, 0, VS, PS_FX],
    2: [2, "game", None, "volatile", "e0e0e0e0", 6, 120, 0, VS, PS_QUAD],
    3: [2, "game", None, "volatile", "e0e0e0e0", 6, 126, 0, VS, PS_QUAD],
    4: [2, "game", "cccccccc", "ok", "c0c0c0c0", 900, 0, 0, VS, PS_SCENE],
    5: [2, "xxmi", "99999999", "ok", None, 6, 0, 0, "3333333333333333", "4444444444444444"],
}
RUNS = [[0, 1000, 1599, 600, 1, 1, 600], [5, 1050, 1060, 11, 1, 1, 11], [4, 1200, 1599, 400, 1, 1, 400]]
for start in (1100, 1300, 1500):
    RUNS += [[1, start, start + 30, 31, 1, 1, 31], [2, start, start + 15, 16, 2, 2, 32],
             [3, start + 16, start + 30, 15, 2, 2, 30]]


def t_ms(frame: int) -> int:
    return round((frame - 1000) * 1000 / 60)


def keyframe_doc(frame: int) -> dict:
    return {"frame": frame, "held": False, "detail": "full", "resolution": [1280, 720],
            "resources": [{"kind": "buffer", "size": 300, "bind": 2, "hash": "b0b0b0b0", "known": True}],
            "shaders": [{"hash": VS, "known": True}, {"hash": PS_FX, "known": True}],
            "regions": [[0, 0, 300, "bbbbbbbb", "ok"]],
            "draws": [{"i": 0, "t": 2, "o": "game", "ic": 150, "fi": 0, "topo": 4, "ib": [0, 57, 0, 0],
                       "sh": [0, -1, -1, -1, 1, -1]}],
            "stats": {"record_ms": 9.5}}


def write_recording(folder: Path, stopped: bool = True) -> None:
    (folder / "keyframes").mkdir(parents=True)
    meta = {"probe": "0.1.11", "started_by": "hotkey", "start_frame": 998, "started_local": "2026-10-21 15:30:00",
            "state": "stopped" if stopped else "recording"}
    if stopped:
        meta.update(stopped_by="hotkey", stop_frame=1600, duration_ms=10000, frames=600, lines=6, runs=len(RUNS),
                    keyframes=3)
    (folder / "recording.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "lines.jsonl").write_text("".join(json.dumps({"id": i, "frame": 1000, "line": l}) + "\n"
                                                for i, l in LINES.items()), encoding="utf-8")
    runs = RUNS if stopped else [r for r in RUNS if r[0] != 0]
    (folder / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs) + '[1,1700', encoding="utf-8")
    if not stopped:
        (folder / "open-runs.json").write_text(json.dumps({"frame": 1599, "runs": [RUNS[0]]}), encoding="utf-8")
    (folder / "frames.bin").write_bytes(b"".join(struct.pack("<5I", f, t_ms(f), 12, 11, 3) for f in range(1000, 1600)))
    kfs = []
    for frame, reason, what in ((999, "start", ""), (1101, "new shader pair", f"draw 0: vs {VS} ps {PS_FX}"),
                                (1599, "periodic", "")):
        doc = keyframe_doc(frame)
        (folder / "keyframes" / f"{frame}.json").write_text(json.dumps(doc), encoding="utf-8")
        (folder / "keyframes" / f"{frame}.bin").write_bytes(b"")
        kfs.append({"frame": frame, "t_ms": max(0, t_ms(frame)), "reason": reason, "what": what, "draws": 1,
                    "json": f"keyframes/{frame}.json", "bin": f"keyframes/{frame}.bin", "json_bytes": 500, "bin_bytes": 0})
    (folder / "keyframes.jsonl").write_text("".join(json.dumps({k: v for k, v in kf.items() if v != ""}) + "\n"
                                                    for kf in kfs), encoding="utf-8")


class FakeProbe:
    def __init__(self):
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.features = ["record", "trigger", "live_summary"]
        self.calls = []
        self.state = {"on": False, "hotkey": "ctrl+F9"}

    def status(self):
        return {"probe": "0.1.11", "features": self.features, "frame": 1}

    def record(self, action="status", flush=False, timeout_ms=5000):
        self.calls.append((action, flush))
        if action == "start":
            self.state = {"on": True, "hotkey": "ctrl+F9", "id": 1, "started_by": "client", "seconds": 0.5,
                          "frames": 30, "lines": 400, "keyframes": 1, "news": 0, "folder": "F:\\x\\recording-1",
                          "bytes": 2 << 20, "pending": 0}
        elif action == "stop":
            self.state = {**self.state, "on": False, "stopped_by": "client", "runs": 900, "seconds": 4.0}
        return dict(self.state)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="record-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Mods").mkdir()
        (self.root / "dist").mkdir()
        self.captures = self.root / "captures"
        self.captures.mkdir()
        self.probe = FakeProbe()
        dll = self.root / "dist" / "efmi_probe.dll"
        self.session = tools.Session(self.root, self.root / "out", dll)
        self.session.client = self.probe
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", dll)
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def call(self, name, **arguments):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        return reply["result"]["content"][0]["text"], reply["result"]["isError"]

    def recording(self, name="recording-20261021-153000", stopped=True) -> Path:
        folder = self.captures / name
        write_recording(folder, stopped)
        return folder

    # ---------------------------------------------------------------- on / off
    def test_switching(self):
        text, err = self.call("efmi_record", state="on")
        self.assertFalse(err, text)
        self.assertIn("recording 1 is on", text)
        self.assertIn("folder: F:\\x\\recording-1 (2.0 MiB written)", text)
        self.assertIn("hotkey in the game: ctrl+F9", text)
        text, err = self.call("efmi_record", state="off")
        self.assertIn("recording 1 is off (stopped by client after 4.0 s)", text)
        self.assertEqual(self.probe.calls, [("start", False), ("stop", True)])
        self.probe.features.remove("record")
        text, err = self.call("efmi_record")
        self.assertTrue(err)
        self.assertIn("0.1.11", text)

    def test_no_recording_yet(self):
        text, err = self.call("efmi_record_history")
        self.assertTrue(err)
        self.assertIn("Ctrl+F9", text)
        text, err = self.call("efmi_record_list")
        self.assertIn("no recordings in", text)

    # ---------------------------------------------------------------- list
    def test_list(self):
        self.recording("recording-20261021-150000")
        self.recording()
        text, err = self.call("efmi_record_list")
        self.assertFalse(err, text)
        rows = text.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("recording-20261021-153000: started 2026-10-21 15:30:00 by hotkey, 10.0 s, "
                                           "600 frames recorded, 6 lines, 12 runs; keyframes: 1 start, "
                                           "1 new shader pair, 1 periodic; stopped by hotkey"), rows[1])

    # ---------------------------------------------------------------- history
    def test_events(self):
        self.recording()
        text, err = self.call("efmi_record_history")
        self.assertFalse(err, text)
        self.assertIn("window 0.00-9.98 s: 600 recorded frames (60 fps); 4 lines of origin 'game'", text)
        self.assertIn("events: 3 moments where at least 2 lines started together (of 4 with any)", text)
        self.assertIn("at 1.67 s: 2 lines start, 2 of them for the first time; drawn 0.52 s (median), up to 0.52 s; "
                      "keyframe 1101 at 1.68 s (new shader pair)", text)
        self.assertIn("at 5.00 s: 2 lines start, 0 of them for the first time", text)
        self.assertIn("ib=volatile 2 ranges, ic<=6 vs=1111111111111111 ps=eeeeeeeeeeeeeeee draws<=2 inst<=2 [first time]",
                      text)
        self.assertNotIn("at 3.33 s", text)          # the object coming into view: one line
        text, _ = self.call("efmi_record_history", min_lines=1)
        self.assertIn("at 3.33 s: 1 lines start, 1 of them for the first time", text)

    def test_recurring(self):
        self.recording()
        text, err = self.call("efmi_record_history", show="recurring")
        self.assertFalse(err, text)
        self.assertIn("recurring: 1 groups", text)
        self.assertIn("3 times, 2 lines: at 1.67 s (0.52 s), 5.00 s (0.52 s), 8.33 s (0.52 s)", text)
        self.assertIn("ib=bbbbbbbb ic=150", text)

    def test_lines_and_filters(self):
        self.recording()
        text, err = self.call("efmi_record_history", show="lines")
        self.assertFalse(err, text)
        self.assertIn("lines: 2 drawn in at most 50% of the frames", text)
        self.assertIn("93/600 frames in 3 stretch(es): 1.67-2.17, 5.00-5.50, 8.33-8.83 s", text)
        self.assertNotIn("ib=cccccccc", text)        # in 400 of the 600 frames
        text, _ = self.call("efmi_record_history", show="lines", origin="xxmi")
        self.assertIn("ib=99999999", text)
        text, _ = self.call("efmi_record_history", show="lines", hash=PS_FX)
        self.assertIn(f"lines with {PS_FX}: 1", text)
        text, _ = self.call("efmi_record_history", show="lines", from_s=4, to_s=6)
        self.assertIn("window 4.00-6.00 s: 121 recorded frames", text)
        self.assertIn("31/121 frames in 1 stretch(es): 5.00-5.50 s", text)

    def test_unfinished_recording_uses_open_runs(self):
        self.recording(stopped=False)
        text, err = self.call("efmi_record_history", show="lines", max_share=1)
        self.assertFalse(err, text)
        self.assertIn("the recording was not stopped", text)
        self.assertIn("ib=aaaaaaaa", text)
        self.assertIn("600/600 frames in 1 stretch(es)", text)

    def test_choosing_a_recording(self):
        self.recording("recording-20261021-150000")
        self.recording()
        text, _ = self.call("efmi_record_history", recording="150000")
        self.assertIn("recording-20261021-150000:", text)
        text, err = self.call("efmi_record_history", recording="nope")
        self.assertTrue(err)
        text, _ = self.call("efmi_record_list", folder=str(self.captures / "recording-20261021-150000"))
        self.assertEqual(len(text.splitlines()), 1)

    # ---------------------------------------------------------------- keyframes
    def test_keyframes(self):
        self.recording()
        text, err = self.call("efmi_record_keyframe")
        self.assertFalse(err, text)
        self.assertIn("3 keyframes:", text)
        self.assertIn(f"frame 1101 at    1.68 s  new shader pair      1 draws, 0.0 MiB  (draw 0: vs {VS} ps {PS_FX})", text)
        text, err = self.call("efmi_record_keyframe", at_s=1.5)
        self.assertFalse(err, text)
        self.assertIn("keyframe 1101 of recording-20261021-153000, at 1.68 s (new shader pair", text)
        self.assertIn("capture 1101 (full): 1 draws", text)
        self.assertEqual(self.session.latest, 1101)
        text, err = self.call("efmi_list_draws")
        self.assertFalse(err, text)
        self.assertIn("ib=bbbbbbbb", text)
        text, err = self.call("efmi_record_keyframe", frame=5)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
