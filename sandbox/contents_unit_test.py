"""Offline checks of the contents code: slots, sources of bytes, the capture option, the folder writer, dump names.

The probe is a fake that answers like probe 0.1.6. No game, no installed EFMI
and no key press; each test owns a private folder. What the real probe keeps is
checked by contents_test.py and contents_parity_test.py against the sandbox.
"""
from __future__ import annotations

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

from efmi_mcp import contents, decode, fa_dump, slot_data, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import crc32c, hex32  # noqa: E402
from efmi_mcp.mcp_stdio import ToolError  # noqa: E402
from efmi_mcp.parity import dds_payload  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint, ProbeError, ProbeNotFound  # noqa: E402

VS, PS, CS = "1111111111111111", "2222222222222222", "3333333333333333"
RGBA = 28
FEATURES = ["reload_key", "create_frames", "mods_key", "region_hash_per_object", "pipeline_state", "rt_snapshots", "contents"]

# Two versions of a vertex buffer (the game rewrites it between the two draws), an index buffer nothing writes to.
INDICES = struct.pack("<6H", 0, 1, 2, 2, 1, 3)
VERTICES = [struct.pack("<16f", *range(n, n + 16)) for n in (0, 100)]
POOL = bytes(range(256)) * 4            # 64 elements of 16 bytes
CONSTANTS = [struct.pack("<8f", 1, 2, 3, 4, 5, 6, 7, 8), struct.pack("<8f", 9, 10, 11, 12, 13, 14, 15, 16)]
WRITTEN = struct.pack("<4I", 7, 8, 9, 10)
PIXELS = [bytes((10, 20, 30, 255)) * 16, bytes((200, 100, 50, 255)) * 16]

RESOURCES = [
    {"id": 0, "ptr": "0x1000", "known": True, "origin": "game", "hash": "aaaaaaaa", "sn": 1, "kind": "buffer", "size": 12, "bind": 2, "stride": 0},
    {"id": 1, "ptr": "0x2000", "known": True, "origin": "game", "hash": "bbbbbbbb", "sn": 2, "kind": "buffer", "size": 64, "bind": 1, "stride": 0, "usage": 2},
    {"id": 2, "ptr": "0x3000", "known": True, "origin": "game", "hash": "cccccccc", "sn": 3, "kind": "buffer", "size": 1024, "bind": 8, "stride": 16},
    {"id": 3, "ptr": "0x4000", "known": True, "origin": "game", "hash": "dddddddd", "sn": 4, "kind": "buffer", "size": 32, "bind": 4, "stride": 0},
    {"id": 4, "ptr": "0x5000", "known": True, "origin": "game", "hash": "eeeeeeee", "sn": 5, "kind": "tex2d",
     "w": 4, "h": 4, "mips": 1, "array": 1, "fmt": RGBA, "fmt_name": "R8G8B8A8_UNORM"},
    {"id": 5, "ptr": "0x6000", "known": True, "origin": "game", "hash": "ffffffff", "sn": 6, "kind": "buffer", "size": 16, "bind": 128, "stride": 0},
    {"id": 6, "ptr": "0x7000", "known": False, "kind": "tex2d", "w": 4, "h": 4, "mips": 1, "array": 1, "fmt": RGBA, "fmt_name": "R8G8B8A8_UNORM"},
]
SHADERS = [{"id": 0, "known": True, "type": "vs", "hash": VS}, {"id": 1, "known": True, "type": "ps", "hash": PS},
           {"id": 2, "known": True, "type": "cs", "hash": CS}]
LAYOUTS = [{"id": 0, "known": True, "elems": [["POSITION", 0, 6, 0, 0, 0, 0], ["TEXCOORD", 1, 16, 0, 12, 0, 0]]}]
# [resource, kind, format, dimension, first, count, first slice, slice count, flags]
VIEWS = [[2, 0, 0, 1, 8, 24, 0, 0, 0], [4, 0, RGBA, 4, 0, 1, 0, 0, 0], [5, 3, 39, 1, 0, 4, 0, 0, 1], [6, 1, RGBA, 4, 0, 0, 0, 0, 0]]
# position in "cont": 0 index buffer, 1/2 the two versions of the vertex buffer, 3 pool, 4/5 constants, 6/7 texture
# versions, 8/9 what the two draws left in their target, 10 what the dispatch wrote
CONT = [[0, 0, 0], [1, 0, 0], [1, 0, 1], [2, 0, 0], [3, 0, 0], [3, 0, 1], [4, 1, 0, 0], [4, 1, 0, 0], [6, 1, 0, 0], [6, 1, 0, 0], [5, 0, 0]]
BYTES = {0: INDICES, 1: VERTICES[0], 2: VERTICES[1], 3: POOL, 4: CONSTANTS[0], 5: CONSTANTS[1], 10: WRITTEN}
TEXELS = {6: PIXELS[0], 7: PIXELS[1], 8: PIXELS[1], 9: PIXELS[0]}


def frame_doc(frame: int, kept: bool = True, events: bool = True) -> dict:
    def draw(n: int) -> dict:
        d = {"i": n, "t": 2, "o": "game", "ic": 6, "fi": 0, "topo": 4, "il": 0, "ib": [0, 57, 0, 0], "vb": [[0, 1, 32, 0, 1]],
             "sh": [0, -1, -1, -1, 1, -1], "cb": [[4, 2, 3, 0, 2, -1]], "srv": [[0, 9, 2], [4, 0, 4]], "srvv": [0, 1],
             "out": [[10, 0, 6]], "outv": [3], "vp": [0, 0, 4, 4]}
        if kept:
            d.update({"cib": 0, "cvb": [1 + n], "ccb": [4 + n], "csrv": [3, 6 + n], "cout": [8 + n]})
        return d

    compute = {"i": 2, "t": 8, "o": "game", "tg": [1, 1, 1], "sh": [-1, -1, -1, -1, -1, 2], "out": [[12, 0, 5]], "outv": [2]}
    if kept:
        compute["cout"] = [10]
    doc = {"frame": frame, "held": True, "detail": "full", "resolution": [4, 4], "resources": RESOURCES, "shaders": SHADERS,
           "layouts": LAYOUTS, "views": VIEWS, "regions": [[0, 0, 12, "0badf00d", "ok"], [1, 0, 64, None, "pending"]],
           "snaps": [], "draws": [draw(0), draw(1), compute],
           "stats": {"record_ms": 0.01, "ct_snaps": len(CONT), "ct_bytes": 96, "ct_budget": 1 << 30, "ct_out_calls": 3, "ct_lost": 0}}
    if kept:
        doc["cont"] = CONT
        if events:
            doc["ev"] = [[1, 1, 1, -1, 0, 4, 0, 0], [8, 1, 6, -1, 0, 0, 0, 0, 0.5, 0.25, 0, 1],
                         [3, 1, 3, -1, 0, 0, 0xFFFFFFFF, 0], [5, 2, 2, 1, 0, 256, 64, 0]]
            doc["stats"]["events"] = 4
    return doc


class FakeProbe:
    """Answers like probe 0.1.6: contents by position, late reads of what the buffers hold now."""

    def __init__(self):
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.frame = 100
        self.requests: list[dict] = []
        self.kept = True
        self.lost: set[int] = set()
        self.reads: list[tuple] = []
        self.features = list(FEATURES)
        self.unchanged = {0: True, 1: False, 2: True, 3: False, 5: False}
        self.now = {0: INDICES, 1: b"\xee" * 64, 2: POOL, 3: b"\xdd" * 32, 5: b"\xcc" * 16}

    def status(self):
        return {"pid": 4321, "probe": "0.1.6", "features": self.features, "attached": True}

    def capture(self, **request):
        self.frame += 1
        self.requests.append(request)
        self.kept = bool(request.get("contents"))
        return frame_doc(self.frame, kept=self.kept), b""

    def read_content(self, capture_frame, snap, offset=0, size=0, timeout_ms=5000):
        self.reads.append(("content", snap, offset, size))
        if capture_frame != self.frame or not self.kept:
            raise ProbeError("that capture is no longer held by the probe; capture again")
        if snap in self.lost:
            raise ProbeError("what this slot held at the call was not kept: the copy budget of the capture was used up")
        if snap in TEXELS:
            return {"width": 4, "height": 4, "format": RGBA, "format_name": "R8G8B8A8_UNORM", "row_bytes": 16, "rows": 4,
                    "mip": 0, "slice": 0, "mips": 1, "array": 1, "tex_width": 4, "tex_height": 4, "cube": False,
                    "kind": "tex2d", "read_frame": self.frame + 9, "exact": True, "from": "copy"}, TEXELS[snap]
        data = BYTES[snap]
        size = size or len(data) - offset
        return {"offset": offset, "size": size, "byte_width": len(data), "exact": True, "from": "copy", "pieces": 1,
                "read_frame": self.frame + 9}, data[offset:offset + size]

    def read_buffer(self, capture_frame, res, offset=0, size=0, timeout_ms=5000):
        self.reads.append(("late", res, offset, size))
        if capture_frame != self.frame:
            raise ProbeError("that capture is no longer held by the probe; capture again")
        data = self.now[res]
        size = size or len(data) - offset
        return {"offset": offset, "size": size, "read_frame": self.frame + 9,
                "unchanged_since_capture": self.unchanged[res]}, data[offset:offset + size]

    def read_texture(self, capture_frame, res, mip=0, slice=0, timeout_ms=5000):
        self.reads.append(("late texture", res, mip, slice))
        return {"width": 4, "height": 4, "format": RGBA, "format_name": "R8G8B8A8_UNORM", "mips": 1, "array": 1,
                "tex_width": 4, "tex_height": 4, "cube": False, "kind": "tex2d", "read_frame": self.frame + 9}, b"\x55" * 64


def call(server, name: str, **arguments):
    """A tool's answer as its text and its error flag, the way a client sees it."""
    result = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})["result"]
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text"), bool(result.get("isError"))


class ContentsCase(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="contents-test-", dir=build)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "Mods").mkdir()
        self.out = root / "out"
        self.probe = FakeProbe()
        self.session = tools.Session(root, self.out, root / "probe.dll")
        self.session.client = self.probe
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(root, self.out, root / "probe.dll")
        # Even a regression that bypasses session.client must never open a user's probe.
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def capture(self, **arguments):
        text, error = call(self.server, "efmi_capture_frame", **arguments)
        self.assertFalse(error, text)
        return text


class CaptureOption(ContentsCase):
    def test_inputs_asks_for_everything_a_call_reads(self):
        text = self.capture(contents="inputs")
        request = self.probe.requests[-1]["contents"]
        self.assertEqual(request["inputs"], ["ib", "vb", "cb", "buf", "tex"])
        self.assertNotIn("outputs", request)
        self.assertIs(request["events"], True)
        self.assertIn("contents: the probe keeps what each call saw", text)

    def test_outputs_without_a_selection_means_every_call(self):
        self.capture(contents="outputs", budget_mb=256)
        request = self.probe.requests[-1]["contents"]
        self.assertEqual(request["outputs"], ["rt", "uav"])
        self.assertIs(request["all"], True)
        self.assertEqual(request["budget_mb"], 256)

    def test_outputs_of_a_range_of_calls_and_of_a_mesh(self):
        self.capture(contents="outputs", first=10, last=20, depth=True)
        request = self.probe.requests[-1]["contents"]
        self.assertEqual(request["match"], [{"first": 10, "last": 20}])
        self.assertEqual(request["outputs"], ["rt", "uav", "depth"])
        self.assertNotIn("all", request)
        self.capture(contents="outputs", hash="0badf00d", first=1)
        request = self.probe.requests[-1]["contents"]
        self.assertEqual(request["match"], [{"ib": "0x1000", "sn": 1, "ic": 6, "fi": 0, "o": "game", "first": 1}])

    def test_buffers_only(self):
        text = self.capture(contents="buffers")
        request = self.probe.requests[-1]["contents"]
        self.assertEqual(request["inputs"], ["ib", "vb", "cb", "buf"])
        self.assertNotIn("outputs", request)
        self.assertIn("contents: the probe keeps what each call saw", text)

    def test_no_contents_by_default(self):
        self.capture()
        self.assertIsNone(self.probe.requests[-1].get("contents"))

    def test_an_older_probe_is_named(self):
        self.probe.features.remove("contents")
        text, error = call(self.server, "efmi_capture_frame", contents="inputs")
        self.assertTrue(error)
        self.assertIn("0.1.6", text)
        self.assertEqual(self.probe.requests, [])

    def test_unknown_level(self):
        with self.assertRaises(ToolError):
            contents.request("everything", None, -1, -1, False, 64)
        self.assertIsNone(contents.request("none", None, -1, -1, False, 64))


class Slots(ContentsCase):
    def cap(self, kept=True) -> Capture:
        return Capture(frame_doc(7, kept=kept))

    def test_what_each_kind_of_slot_exposes(self):
        cap = self.cap()
        d = cap.draw(0)
        ib = slot_data.buffer_binding(cap, d, "ib")
        self.assertEqual((ib["role"], ib["offset"], ib["size"], ib["element"], ib["content"]), ("ib", 0, 12, 2, 0))
        vb = slot_data.buffer_binding(cap, d, "VB0")
        self.assertEqual((vb["role"], vb["offset"], vb["size"], vb["exact_range"], vb["content"]), ("vb", 0, 64, False, 1))
        cb = slot_data.buffer_binding(cap, d, "ps-cb2")
        self.assertEqual((cb["role"], cb["offset"], cb["size"], cb["content"]), ("cb", 0, 32, 4))
        pool = slot_data.buffer_binding(cap, d, "vs-t9")
        self.assertEqual((pool["role"], pool["offset"], pool["size"], pool["element"]), ("srv", 128, 384, 16))
        written = slot_data.buffer_binding(cap, cap.draw(2), "u0")
        self.assertEqual((written["role"], written["size"], written["element"], written["content"]), ("uav", 16, 4, 10))

    def test_slots_that_are_not_buffers(self):
        cap = self.cap()
        for slot, wanted in (("ps-t0", "efmi_read_texture"), ("gs-cb7", "nothing bound"), ("vb5", "nothing bound"),
                             ("u3", "nothing bound"), ("banana", "slot must be")):
            with self.assertRaises(slot_data.SlotError) as raised:
                slot_data.buffer_binding(cap, cap.draw(0), slot)
            self.assertIn(wanted, str(raised.exception))

    def test_kept_contents_come_first_and_are_exact(self):
        self.capture(contents="outputs")
        cap = self.session.capture("latest")
        for n in (0, 1):
            data, info = slot_data.read_slot(self.session, cap, slot_data.buffer_binding(cap, cap.draw(n), "vb0"))
            self.assertEqual(data, VERTICES[n])
            self.assertEqual((info["source"], info["exact"]), ("contents", True))
            self.assertIn("when the call was issued", info["note"])
        data, info = slot_data.read_slot(self.session, cap, slot_data.buffer_binding(cap, cap.draw(2), "u0"))
        self.assertEqual(data, WRITTEN)
        self.assertIn("after the call", info["note"])
        self.assertFalse([r for r in self.probe.reads if r[0] == "late"])

    def test_a_part_and_the_whole(self):
        self.capture(contents="inputs")
        cap = self.session.capture("latest")
        pool = slot_data.buffer_binding(cap, cap.draw(0), "vs-t9")
        data, info = slot_data.read_slot(self.session, cap, pool)
        self.assertEqual((data, info["offset"], info["size"]), (POOL[128:512], 128, 384))
        data, info = slot_data.read_slot(self.session, cap, pool, whole=True)
        self.assertEqual((len(data), info["offset"]), (1024, 0))

    def test_without_contents_the_buffer_is_read_now_and_says_whether_it_changed(self):
        self.capture()
        cap = self.session.capture("latest")
        data, info = slot_data.read_slot(self.session, cap, slot_data.buffer_binding(cap, cap.draw(0), "ib"))
        self.assertEqual((data, info["source"], info["exact"]), (INDICES, "late read", True))
        data, info = slot_data.read_slot(self.session, cap, slot_data.buffer_binding(cap, cap.draw(0), "vb0"))
        self.assertEqual((info["source"], info["exact"]), ("late read", False))
        self.assertIn("WRITTEN TO since the capture", info["note"])
        self.assertIn("capture with contents", info["note"])

    def test_contents_that_were_lost_are_not_made_up(self):
        self.capture(contents="inputs")
        cap = self.session.capture("latest")
        self.probe.lost = {1}
        data, info = slot_data.read_slot(self.session, cap, slot_data.buffer_binding(cap, cap.draw(0), "vb0"))
        self.assertEqual((info["source"], info["exact"]), ("late read", False))
        self.assertIn("budget", info["contents_unavailable"])
        self.assertIn("could not be used", info["note"])


class Tools(ContentsCase):
    def test_read_buffer_gives_each_draw_its_own_vertices(self):
        self.capture(contents="outputs")
        for n in (0, 1):
            text, error = call(self.server, "efmi_read_buffer", draw=n, slot="vb0", count=1)
            self.assertFalse(error, text)
            answer = json.loads(text)
            self.assertEqual(answer["elements"][0]["POSITION"], [float(n * 100), n * 100 + 1.0, n * 100 + 2.0])
            self.assertIs(answer["exact"], True)

    def test_read_buffer_of_a_view_of_a_pool(self):
        self.capture(contents="inputs")
        text, error = call(self.server, "efmi_read_buffer", draw=0, slot="vs-t9", count=2, view="hex")
        answer = json.loads(text)
        self.assertEqual((answer["elements"], answer["element_bytes"], answer["first_element_of_buffer"]), (24, 16, 8))
        self.assertEqual(answer["values"], {"e0": POOL[128:144].hex(), "e1": POOL[144:160].hex()})

    def test_read_texture_uses_what_the_call_saw(self):
        self.capture(contents="outputs")
        text, error = call(self.server, "efmi_read_texture", draw=1, slot="ps-t0", preview=False)
        self.assertFalse(error, text)
        self.assertIn("as it was when the call was issued", text)
        saved = Path(next(line.split("saved: ")[1] for line in text.splitlines() if line.endswith(".dds")))
        self.assertEqual(dds_payload(saved.read_bytes())[0], PIXELS[1])
        self.assertIn("-draw1", saved.name)
        text, error = call(self.server, "efmi_read_texture", draw=0, slot="o0", preview=False)
        self.assertIn("as the call left it", text)

    def test_read_texture_without_contents_says_it_is_the_texture_as_it_is_now(self):
        self.capture()
        text, error = call(self.server, "efmi_read_texture", draw=0, slot="ps-t0", preview=False)
        self.assertFalse(error, text)
        self.assertIn("current contents, not necessarily those at the draw", text)
        self.assertIn("capture with contents", text)

    def test_get_draw_names_the_kept_slots(self):
        self.capture(contents="outputs")
        text, error = call(self.server, "efmi_get_draw", draw=0)
        kept = json.loads(text)["contents_kept"]
        self.assertEqual(kept["as_the_call_saw_them"], ["ib", "vb0", "ps-cb2", "vs-t9", "ps-t0"])
        self.assertEqual(kept["as_the_call_left_them"], ["o0"])
        self.capture()
        self.assertNotIn("contents_kept", json.loads(call(self.server, "efmi_get_draw", draw=0)[0]))

    def test_list_writes(self):
        self.capture(contents="inputs")
        text, error = call(self.server, "efmi_list_writes")
        self.assertFalse(error, text)
        lines = text.splitlines()
        self.assertIn("4 writes match of 4 listed", lines[0])
        self.assertIn("before #1     Map res1 bbbbbbbb buffer 64 bytes subresource 0 write-discard", lines[1])
        self.assertIn("ClearRenderTargetView res6 unhashed tex2d 4x4 to [0.5, 0.25, 0, 1]", lines[2])
        self.assertIn("UpdateSubresource res3 dddddddd buffer 32 bytes subresource 0 whole", lines[3])
        self.assertIn("CopySubresourceRegion into res2 cccccccc buffer 1024 bytes subresource 0 at 256 ([256, 320)) "
                      "from res1 bbbbbbbb buffer 64 bytes subresource 0", lines[4])
        text, _ = call(self.server, "efmi_list_writes", hash="cccccccc")
        self.assertIn("1 writes match", text)
        text, _ = call(self.server, "efmi_list_writes", first=2, kind="map")
        self.assertIn("0 writes match", text)

    def test_list_writes_needs_a_capture_that_has_them(self):
        self.capture()
        text, error = call(self.server, "efmi_list_writes")
        self.assertTrue(error)
        self.assertIn("contents='inputs'", text)

    def rebind_second_draw(self):
        """The second draw binds its vertices at another offset and no longer has the texture."""
        cap = self.session.capture("latest")
        cap.raw_draws[1]["vb"] = [[0, 1, 32, 32, 1]]
        cap.raw_draws[1]["srv"] = [[0, 9, 2]]
        cap.raw_draws[1]["srvv"] = [0]
        if "csrv" in cap.raw_draws[1]:
            cap.raw_draws[1]["csrv"] = [3]

    def test_what_a_call_has_bound_differently(self):
        self.capture(contents="inputs")
        self.rebind_second_draw()
        cap = self.session.capture("latest")
        first, second = (contents.bindings_of(cap, cap.draw(n)) for n in (0, 1))
        self.assertEqual(first["vs"], VS)
        self.assertEqual(first["ib"], "aaaaaaaa at 0 R16_UINT")
        self.assertEqual(first["ps-cb2"], "dddddddd registers 0..+2")
        self.assertEqual(first["vs-t9"], "cccccccc (UNKNOWN buffer elements 8..+24)")
        self.assertEqual(first["ps-t0"], "eeeeeeee")
        self.assertEqual(first["o0"], "res6")                      # a target without a hash is named by its number
        self.assertEqual(contents.binding_changes(first, second),
                         ["bind vb0: bbbbbbbb at 32 stride 32 (was bbbbbbbb at 0 stride 32)", "bind ps-t0: nothing (was eeeeeeee)"])
        self.assertEqual(contents.binding_changes(None, second), [])
        self.assertEqual(contents.binding_changes(first, first), [])
        # A dispatch is compared with the dispatch before it, not with the draw in between.
        self.assertIsNone(contents.previous_bindings(cap, 2, True))
        self.assertEqual(contents.previous_bindings(cap, 2, False), second)

    def test_list_writes_with_bindings(self):
        self.capture(contents="inputs")
        self.rebind_second_draw()
        text, error = call(self.server, "efmi_list_writes", bindings=True)
        self.assertFalse(error, text)
        lines = text.splitlines()
        self.assertIn("2 binding changes", lines[1])
        at_one = [line for line in lines if line.startswith("before #1 ")]
        # the writes before a call first, then what it has bound differently
        self.assertEqual([line.split(maxsplit=3)[2] for line in at_one], ["Map", "ClearRenderTargetView", "UpdateSubresource", "bind", "bind"])
        self.assertIn("bind vb0: bbbbbbbb at 32 stride 32 (was bbbbbbbb at 0 stride 32)", at_one[3])
        text, error = call(self.server, "efmi_list_writes", bindings=True, hash="eeeeeeee")
        self.assertIn("1 binding changes", text)
        self.assertIn("bind ps-t0: nothing (was eeeeeeee)", text)

    def test_bindings_do_not_need_contents(self):
        self.capture()
        self.rebind_second_draw()
        text, error = call(self.server, "efmi_list_writes", bindings=True)
        self.assertFalse(error, text)
        self.assertIn("no writes were listed", text)
        self.assertIn("2 binding changes", text)


class Dump(ContentsCase):
    def test_a_frame_written_out_reads_back_like_a_frame_analysis_dump(self):
        self.capture(contents="outputs")
        text, error = call(self.server, "efmi_dump_frame")
        self.assertFalse(error, text)
        answer = json.loads(text)
        folder = Path(answer["folder"])
        self.assertEqual(answer["calls_written"], [0, 2])
        self.assertNotIn("skipped", answer)
        dump = fa_dump.parse_frame_dump(folder)
        self.assertEqual([dump.draws[n].call for n in (0, 1, 2)], ["DrawIndexed", "DrawIndexed", "Dispatch"])
        self.assertEqual(dump.draws[0].args, {"IndexCount": 6, "StartIndexLocation": 0, "BaseVertexLocation": 0})
        self.assertEqual(dump.draws[0].shaders, {"vs": VS, "ps": PS})
        self.assertEqual(set(dump.draws[0].slots), {"ib", "vb0", "ps-cb2", "vs-t9", "ps-t0", "o0"})
        self.assertEqual((dump.draws[0].slots["ib"].hash, dump.draws[0].slots["ib"].full_hash), ("0badf00d", "aaaaaaaa"))
        self.assertIsNone(dump.draws[0].slots["o0"].hash)          # a target the game did not create: no hash, like XXMI
        self.assertEqual(set(dump.draws[2].slots), {"u0"})

        def file(call_number: int, slot: str, ext: str) -> bytes:
            return next(folder.glob(f"{call_number:06d}-{slot}*.{ext}")).read_bytes()

        self.assertEqual([file(n, "vb0", "buf") for n in (0, 1)], VERTICES)
        self.assertEqual([file(n, "ps-cb2", "buf") for n in (0, 1)], CONSTANTS)
        self.assertEqual(file(0, "vs-t9", "buf"), POOL)                          # whole, as XXMI writes it
        self.assertEqual(file(2, "u0", "buf"), WRITTEN)
        self.assertEqual([dds_payload(file(n, "ps-t0", "dds"))[0] for n in (0, 1)], PIXELS)
        self.assertEqual(dds_payload(file(0, "o0", "dds"))[0], PIXELS[1])
        self.assertEqual(file(0, "ib", "txt").decode(),
                         "byte offset: 0\nfirst index: 0\nindex count: 6\ntopology: trianglelist\n"
                         "format: DXGI_FORMAT_R16_UINT\n\n0 1 2\n2 1 3\n")
        vertices = file(1, "vb0", "txt").decode()
        # XXMI's conventions: no offset line when it is zero, entries numbered from the first vertex, a blank
        # line before each entry and none after the last (compared with XXMI itself in contents_parity_test.py).
        self.assertTrue(vertices.startswith("stride: 32\nfirst vertex: 0\nvertex count: 2\ntopology: trianglelist\n"
                                            "element[0]:\n  SemanticName: POSITION\n  SemanticIndex: 0\n  Format: R32G32B32_FLOAT\n"))
        self.assertIn("\n\nvertex-data:\n\nvb0[0]+000 POSITION: 100, 101, 102\nvb0[0]+012 TEXCOORD1: 103, 104\n\nvb0[1]+000 ", vertices)
        self.assertTrue(vertices.endswith("\n\nvb0[1]+000 POSITION: 108, 109, 110\nvb0[1]+012 TEXCOORD1: 111, 112\n"), vertices[-120:])
        self.assertEqual(file(0, "ps-cb2", "txt").decode().splitlines()[:5],
                         ["cb2[0].x: 1", "cb2[0].y: 2", "cb2[0].z: 3", "cb2[0].w: 4", "cb2[1].x: 5"])
        # One file under deduped/ for what several calls share.
        self.assertEqual(len(list((folder / "deduped").glob("*.buf"))), 7)
        self.assertEqual(next(folder.glob("000000-ib*.buf")).stat().st_ino, next(folder.glob("000001-ib*.buf")).stat().st_ino)
        log = (folder / "log.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("000001 Map res1 bbbbbbbb buffer 64 bytes subresource 0 write-discard", log)
        self.assertEqual(log[-1], "000002 Dispatch(ThreadGroupCountX:1, ThreadGroupCountY:1, ThreadGroupCountZ:1)")
        self.assertLess(log.index("000001 Map res1 bbbbbbbb buffer 64 bytes subresource 0 write-discard"),
                        log.index("000001 DrawIndexed(IndexCount:6, StartIndexLocation:0, BaseVertexLocation:0)"))
        self.assertFalse([line for line in log if " bind " in line])        # both draws have the same things bound

    def test_the_log_says_what_a_call_has_bound_differently(self):
        self.capture(contents="outputs")
        cap = self.session.capture("latest")
        cap.raw_draws[1]["vb"] = [[0, 1, 32, 32, 1]]
        answer = json.loads(call(self.server, "efmi_dump_frame", first=1, last=1, kinds=["ib"])[0])
        log = (Path(answer["folder"]) / "log.txt").read_text(encoding="utf-8").splitlines()
        # also for the first call that is written: against the call before it
        bind = "000001 bind vb0: bbbbbbbb at 32 stride 32 (was bbbbbbbb at 0 stride 32)"
        self.assertIn(bind, log)
        self.assertLess(log.index(bind), log.index("000001 DrawIndexed(IndexCount:6, StartIndexLocation:0, BaseVertexLocation:0)"))

    def test_a_capture_without_contents_only_writes_what_has_not_changed(self):
        self.capture()
        text, error = call(self.server, "efmi_dump_frame")
        answer = json.loads(text)
        folder = Path(answer["folder"])
        names = sorted(p.name.split("-vs=")[0].split("-cs=")[0] for p in folder.iterdir() if p.suffix in (".buf", ".dds"))
        self.assertEqual(names, ["000000-ib=0badf00d(aaaaaaaa)", "000000-vs-t9=cccccccc", "000001-ib=0badf00d(aaaaaaaa)",
                                 "000001-vs-t9=cccccccc"])
        self.assertGreater(answer["skipped"]["not_exact"], 0)
        self.assertIn("without contents", answer["note"])
        self.assertTrue(any("WRITTEN TO since the capture" in p for p in answer["problems"]))

    def test_kinds_and_range(self):
        self.capture(contents="outputs")
        text, error = call(self.server, "efmi_dump_frame", first=1, last=1, kinds=["cb", "out"], txt=False)
        answer = json.loads(text)
        names = sorted(p.name.split("=")[0] for p in Path(answer["folder"]).iterdir() if p.is_file() and p.name != "log.txt")
        self.assertEqual(names, ["000001-o0-vs", "000001-ps-cb2"])

    def test_the_size_limit_stops_and_says_where_to_continue(self):
        self.capture(contents="outputs")
        with patch.object(contents._Writer, "room", lambda self, size: (setattr(self, "over_limit", 1), False)[1]):
            answer = json.loads(call(self.server, "efmi_dump_frame", max_mb=1)[0])
        self.assertIn("call 0 alone does not fit max_mb=1", answer["stopped"])
        self.assertEqual((answer["calls_written"], answer["continue_with"]), ([], {"first": 0, "last": 2}))

    def test_the_size_limit_between_two_calls(self):
        self.capture(contents="outputs")

        def link(writer, target, name, real=contents._Writer.link):
            if name.startswith("000001-"):          # the limit is met while the second call is written
                writer.over_limit += 1
                return False
            return real(writer, target, name)

        with patch.object(contents._Writer, "link", link):
            answer = json.loads(call(self.server, "efmi_dump_frame")[0])
        self.assertEqual(answer["stopped"], "the size limit (max_mb) was reached")
        self.assertEqual((answer["calls_written"], answer["continue_with"]), ([0, 0], {"first": 1, "last": 2}))

    def test_bad_arguments(self):
        self.capture(contents="outputs")
        self.assertIn("first/last", call(self.server, "efmi_dump_frame", first=9)[0])
        self.assertIn("unknown kinds", call(self.server, "efmi_dump_frame", kinds=["everything"])[0])


class DumpNames(unittest.TestCase):
    def test_names_xxmi_writes(self):
        cases = {
            "000013-ib=5ede2273(9a09f1f0)-vs=b960839f9608d37c-ps=7b067f2b06883a0c.buf": ("ib", "5ede2273", "9a09f1f0", None),
            "000530-ps-t2=!S!=29c40628-vs=40426f24c41b60b9-ps=a9bbcbd75f739b89.dds": ("ps-t2", "29c40628", None, "S"),
            "000017-ps-t12=!US!=2824bacc-vs=40426f24c41b60b9-ps=a9bbcbd75f739b89.dds": ("ps-t12", "2824bacc", None, "US"),
            "000741-o0-vs=65ae3450d9b1fba5-ps=be28620e0873f773.jpg": ("o0", None, None, None),
            "000001-u0=554904b3-cs=1f543271de52fe8d.buf": ("u0", "554904b3", None, None),
            "000044-oD=0ddf2cb7-vs=65ae3450d9b1fba5-ps=be28620e0873f773.dds": ("oD", "0ddf2cb7", None, None),
            "000014-vs-cb0=a517561d-vs=b12fddff7c9409ef-ps=d7bb9dd57f5b70c6.txt": ("vs-cb0", "a517561d", None, None),
        }
        for name, (slot, hash_text, full, marks) in cases.items():
            m = fa_dump._RE_FILE.match(name)
            self.assertIsNotNone(m, name)
            self.assertEqual((m["slot"], m["hash"], m["full"], m["marks"]), (slot, hash_text, full, marks), name)
        self.assertIsNone(fa_dump._RE_FILE.match("log.txt"))
        self.assertEqual(fa_dump.MARKS["S"], "CopySubresourceRegion")

    def test_a_folder_with_marked_and_hashless_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("000002-ps-t0=!M!=aaaaaaaa-vs=1111111111111111-ps=2222222222222222.dds",
                         "000002-o0-vs=1111111111111111-ps=2222222222222222.jpg",
                         "000002-ib=bbbbbbbb(cccccccc)-vs=1111111111111111-ps=2222222222222222.buf"):
                (root / name).write_bytes(b"")
            (root / "log.txt").write_text("analyse_options: 0000073d\n000002 DrawIndexed(IndexCount:3, StartIndexLocation:0, BaseVertexLocation:0)\n")
            dump = fa_dump.parse_frame_dump(root)
            self.assertEqual(dump.file_count, 3)
            slots = dump.draws[2].slots
            self.assertEqual((slots["ps-t0"].marks, slots["ps-t0"].hash), ("M", "aaaaaaaa"))
            self.assertIsNone(slots["o0"].hash)
            self.assertEqual(slots["ib"].full_hash, "cccccccc")


class Volumes(unittest.TestCase):
    def test_a_volume_as_dds(self):
        data = bytes(range(64)) * 2
        dds = decode.make_dds(4, 4, 61, data, depth=8)
        self.assertEqual(struct.unpack_from("<I", dds, 24)[0], 8)                 # depth
        self.assertTrue(struct.unpack_from("<I", dds, 8)[0] & 0x800000)           # DDSD_DEPTH
        self.assertEqual(struct.unpack_from("<5I", dds, 128)[:2], (61, 4))        # R8_UNORM, a 3D texture
        self.assertEqual(dds[148:], data)
        flat = decode.make_dds(4, 4, 28, b"\0" * 64)
        self.assertEqual(struct.unpack_from("<5I", flat, 128)[1], 3)
        self.assertEqual(dds_payload(flat)[1]["format"], 28)


if __name__ == "__main__":
    unittest.main(verbosity=1)
