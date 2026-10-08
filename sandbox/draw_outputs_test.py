"""Offline checks of efmi_capture_draw_outputs and of the pipeline state in efmi_get_draw.

The probe is a fake that hands out capture documents the way probe 0.1.5 writes
them, with render target copies of a few pixels. Nothing is recorded and no game
or installed EFMI is touched; each test owns a private folder. What the real
probe records is checked by selftest.py and mcp_test.py against the sandbox.
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

from efmi_mcp import draw_outputs, pipeline, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.mcp_stdio import ToolError  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint, ProbeError, ProbeNotFound  # noqa: E402

MESH = "64e97b80"
VS, PS = "1111111111111111", "2222222222222222"
RGBA_TYPELESS, RGBA, RGBA_SRGB, D32 = 27, 28, 29, 40
WIDTH, HEIGHT = 8, 4
FEATURES = ["reload_key", "create_frames", "mods_key", "region_hash_per_object", "pipeline_state", "rt_snapshots"]

RESOURCES = [
    {"id": 0, "ptr": "0x1000", "known": True, "origin": "game", "hash": "aaaaaaaa", "sn": 7, "kind": "buffer",
     "size": 4096, "bind": 2},
    {"id": 1, "ptr": "0x2000", "known": True, "origin": "game", "hash": "cccccccc", "sn": 8, "kind": "tex2d",
     "w": WIDTH, "h": HEIGHT, "mips": 1, "array": 1, "fmt": RGBA, "fmt_name": "R8G8B8A8_UNORM"},
    {"id": 2, "ptr": "0x3000", "known": True, "origin": "game", "hash": "dddddddd", "sn": 9, "kind": "tex2d",
     "w": WIDTH, "h": HEIGHT, "mips": 1, "array": 1, "fmt": D32, "fmt_name": "D32_FLOAT"},
    {"id": 3, "ptr": "0x4000", "known": True, "origin": "game", "hash": "eeeeeeee", "sn": 10, "kind": "tex2d",
     "w": 64, "h": 64, "mips": 4, "array": 1, "fmt": RGBA_TYPELESS, "fmt_name": "R8G8B8A8_TYPELESS"},
]
SHADERS = [{"id": 0, "known": True, "type": "vs", "hash": VS}, {"id": 1, "known": True, "type": "ps", "hash": PS}]
STATES = [
    {"k": "blend", "a2c": False, "ind": False, "rt": [[1, 5, 6, 1, 2, 1, 1, 15]]},
    {"k": "depth", "de": True, "dw": 1, "df": 4, "se": False, "srm": 255, "swm": 255, "ff": [1, 1, 1, 8], "bf": [1, 1, 1, 8]},
    {"k": "raster", "fill": 3, "cull": 3, "ccw": False, "db": 0, "dbc": 0, "ssdb": 0, "dc": True, "sc": False,
     "ms": False, "aal": False},
    {"k": "sampler", "f": 0x15, "u": 1, "v": 1, "w": 1, "lb": 0, "ma": 16, "cf": 1, "bc": [0, 0, 0, 0], "minl": 0, "maxl": 12},
]
# [resource, kind, format, dimension, first, count, first slice, slice count, flags]
VIEWS = [
    [3, 0, RGBA_SRGB, 4, 0, 4, 0, 0, 0],      # all of a typeless texture, read as sRGB
    [1, 1, RGBA, 4, 0, 0, 0, 0, 0],           # the colour target as it is
    [2, 2, D32, 3, 0, 0, 0, 0, 1],            # the depth target, depth read-only
]


def frame_doc(frame: int, *, ib_hash: str = MESH, rts=(), stats: dict | None = None, state: bool = True,
              detail: str = "full") -> dict:
    """One frame in which the game draws one mesh into a colour and a depth target."""
    draw = {"i": 0, "t": 2, "o": "game", "ic": 6, "fi": 12, "fv": 0, "topo": 4, "il": -1, "ib": [0, 57, 0, 0],
            "sh": [0, -1, -1, -1, 1, -1]}
    doc = {"frame": frame, "held": True, "detail": detail, "resolution": [WIDTH, HEIGHT], "resources": RESOURCES,
           "shaders": SHADERS, "regions": [[0, 24, 12, ib_hash, "ok"]], "draws": [draw],
           "stats": {"record_ms": 0.01, **(stats or {})}}
    if detail == "full":
        draw.update({"srv": [[4, 0, 3]], "out": [[10, 0, 1], [11, 0, 2]], "vp": [0, 0, WIDTH, HEIGHT]})
        if state:
            draw.update({"srvv": [0], "outv": [1, 2], "smp": [[4, 0, 3]], "bs": [0, 0.5, 0.5, 0.5, 0.5, 0xFFFFFFFF],
                         "dss": [1, 0], "rs": 2, "sc": [[0, 0, WIDTH, HEIGHT]], "vpn": 1, "vpz": [0, 1]})
            doc.update({"states": STATES, "views": VIEWS})
    if rts is not None and stats is not None:
        doc["rts"] = list(rts)
    return doc


def copy(when: int, state: str = "ok", kind: int = 10, box=(0, 0, WIDTH, HEIGHT), boxed: bool = False) -> dict:
    snap = {"draw": 0, "when": when, "kind": kind, "slot": 0, "res": 2 if kind == 11 else 1, "state": state}
    if state in ("ok", "over_budget"):
        snap.update({"mip": 0, "slice": 0, "w": WIDTH, "h": HEIGHT, "fmt": D32 if kind == 11 else RGBA,
                     "fmt_name": "D32_FLOAT" if kind == 11 else "R8G8B8A8_UNORM", "bytes": WIDTH * HEIGHT * 4,
                     "box": list(box), "boxed": boxed})
    return snap


def pixels(painted: bool) -> bytes:
    """The target filled with one colour; `painted` adds a block at x 2..5, y 1..3."""
    rows = []
    for y in range(HEIGHT):
        for x in range(WIDTH):
            rows.append(bytes((200, 100, 50, 255)) if painted and 2 <= x < 5 and 1 <= y < 3 else bytes((10, 20, 30, 255)))
    return b"".join(rows)


META = {"width": WIDTH, "height": HEIGHT, "format": RGBA, "row_bytes": WIDTH * 4}
COUNTS = {"rt_matched": 1, "rt_draws": 1, "rt_depth_only": 0, "rt_bytes": 2 * WIDTH * HEIGHT * 4, "rt_over_budget": 0}


class FakeProbe:
    def __init__(self):
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.version = "0.1.5"
        self.features = list(FEATURES)
        self.frame = 100
        self.requests: list[dict] = []
        self.released: list[int] = []
        self.reads: list[int] = []
        # What the frame recorded for a request with `rt` holds.
        self.rts = [copy(0), copy(1)]
        self.stats = dict(COUNTS)
        self.ib_hash = MESH
        self.copies = {0: (META, pixels(False)), 1: (META, pixels(True))}

    def status(self):
        return {"pid": self.endpoint.pid, "probe": self.version, "features": self.features, "attached": True}

    def capture(self, **request):
        self.frame += 1
        self.requests.append(request)
        if request.get("rt"):
            return frame_doc(self.frame, ib_hash=self.ib_hash, rts=self.rts, stats=self.stats), b""
        return frame_doc(self.frame, detail=request.get("detail", "full")), b""

    def read_rt(self, capture_frame, snap, timeout_ms=5000):
        self.reads.append(snap)
        item = self.copies[snap]
        if isinstance(item, Exception):
            raise item
        return item

    def release_rt(self, capture_frame):
        self.released.append(capture_frame)


class DrawOutputTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="draw-outputs-test-", dir=build)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Mods").mkdir()
        self.probe = FakeProbe()
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = self.probe
        self.session.add_capture(Capture(*self.probe.capture(detail="full")))
        self.probe.requests.clear()
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.root / "out", self.root / "probe.dll")
        # Even a regression that bypasses session.client must never open a user's probe.
        self.enterContext(patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")))

    def call(self, name="efmi_capture_draw_outputs", **arguments):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        content = reply["result"]["content"]
        return content[0]["text"], reply["result"]["isError"], [c for c in content if c["type"] == "image"]

    def request(self) -> dict:
        self.assertEqual(len(self.probe.requests), 1)
        return self.probe.requests[0]

    # ---------------------------------------------------------------- selecting
    def test_tool_is_registered_as_changing_what_latest_is(self):
        listed = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        entry = next(tool for tool in listed if tool["name"] == "efmi_capture_draw_outputs")
        self.assertFalse(entry["annotations"]["readOnlyHint"])
        self.assertEqual(entry["inputSchema"]["properties"]["targets"]["enum"], ["first", "all"])

    def test_index_buffer_hash_selects_every_pass_of_the_mesh(self):
        text, error, _ = self.call(hash=MESH)
        self.assertFalse(error, text)
        request = self.request()
        self.assertEqual((request["detail"], request["cb_data"]), ("full", False))
        # found again by buffer object and range, whatever the shaders
        self.assertEqual(request["rt"], {"match": [{"ib": "0x1000", "sn": 7, "ic": 6, "fi": 12, "o": "game"}], "max": 8,
                                         "before": True, "all": False, "depth": False, "budget_mb": 512})
        self.assertIn(f"every draw of the mesh(es) with index buffer hash {MESH} (taken from capture 101)", text)

    def test_the_hash_of_the_whole_buffer_selects_as_well(self):
        text, error, _ = self.call(hash="aaaaaaaa", max_draws=3, targets="all", depth=True, before=False, box=[1, 0, 5, 2])
        self.assertFalse(error, text)
        self.assertEqual(self.request()["rt"], {"match": [{"ib": "0x1000", "sn": 7, "ic": 6, "fi": 12, "o": "game"}], "max": 3,
                                                "before": False, "all": True, "depth": True, "budget_mb": 512,
                                                "box": [1, 0, 5, 2]})

    def test_draw_numbers_select_with_the_shaders(self):
        text, error, _ = self.call(draws=[0])
        self.assertFalse(error, text)
        self.assertEqual(self.request()["rt"]["match"],
                         [{"ib": "0x1000", "sn": 7, "ic": 6, "fi": 12, "vs": VS, "ps": PS, "o": "game"}])
        self.assertIn("draws [0] of capture 101, found again by index buffer, range and shaders", text)

    def test_a_shader_hash_needs_no_earlier_capture(self):
        self.session.captures.clear()
        self.session.latest = None
        text, error, _ = self.call(hash="0x" + PS.upper())
        self.assertFalse(error, text)
        self.assertEqual(self.request()["rt"]["match"], [{"sh": PS}])
        self.assertIn(f"every draw whose vertex or pixel shader is {PS}", text)

    def test_an_object_the_probe_did_not_see_created_is_matched_by_address_alone(self):
        cap = Capture(frame_doc(5))
        cap.resources = [dict(RESOURCES[0], known=False)] + RESOURCES[1:]
        self.assertEqual(draw_outputs.matcher_of(cap, cap.draw(0), with_shaders=False),
                         {"ib": "0x1000", "ic": 6, "fi": 12, "o": "game"})

    def test_what_cannot_select_is_refused_before_anything_is_recorded(self):
        for arguments, expected in (({}, "give draws"), ({"hash": "123"}, "give draws"),
                                    ({"hash": "not-a-hash-16-ch"}, "give draws"), ({"hash": "zzzzzzzz"}, "give draws"),
                                    ({"hash": "deadbeef"}, "no draw of capture 101 has index buffer hash deadbeef"),
                                    ({"draws": [4]}, "draw 4 is not in capture 101"),
                                    ({"hash": MESH, "box": [5, 0, 5, 2]}, "box must be"),
                                    ({"hash": MESH, "box": [0, 0, 5]}, "box must be"),
                                    ({"hash": MESH, "capture": 7}, "capture 7 is not in memory")):
            with self.subTest(arguments=arguments):
                text, error, _ = self.call(**arguments)
                self.assertTrue(error, text)
                self.assertIn(expected, text)
        self.assertEqual(self.probe.requests, [])
        self.assertEqual(self.session.latest, 101)

    def test_selection_without_a_capture_says_what_to_do(self):
        self.session.captures.clear()
        self.session.latest = None
        for arguments in ({"hash": MESH}, {"draws": [0]}):
            text, error, _ = self.call(**arguments)
            self.assertTrue(error, text)
            self.assertIn("call efmi_capture_frame first", text)
        self.assertEqual(self.probe.requests, [])

    def test_a_probe_that_cannot_copy_targets_is_named(self):
        self.probe.version = "0.1.4"
        self.probe.features = FEATURES[:4]
        text, error, _ = self.call(hash=MESH)
        self.assertTrue(error, text)
        self.assertIn("Probe 0.1.4 cannot copy render targets around draws; this needs 0.1.5", text)
        self.assertIn("Restart the game", text)
        self.assertEqual(self.probe.requests, [])

    # ---------------------------------------------------------------- the report
    def test_report_gives_the_changed_pixels_the_pictures_and_frees_the_copies(self):
        text, error, images = self.call(hash=MESH)
        self.assertFalse(error, text)
        lines = text.splitlines()
        self.assertEqual(lines[0].split(";")[0], "capture 102: 1 draws")
        self.assertEqual(lines[1], "draws that matched: 1; targets copied for 1 of them; 0.0 MiB of pixels")
        self.assertEqual(lines[2], f"#0 DrawIndexed game ib={MESH} ic=6 vs={VS} ps={PS}")
        self.assertEqual(lines[3], "    o0: cccccccc 8x4 R8G8B8A8_UNORM")
        self.assertEqual(lines[4], "    changed 6 pixels (18.75% of the copied area) within [2, 1, 5, 3] (left, top, right, bottom)")
        folder = self.root / "out" / "frame-102" / "draw-outputs"
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["draw00000-o0-after.png", "draw00000-o0-before.png", "draw00000-o0-changed.png"])
        for label, name in (("before", "before"), ("after", "after"), ("what it touched", "changed")):
            self.assertIn(f"    {label}: {folder / f'draw00000-o0-{name}.png'}", lines)
        self.assertIn("    preview image: 1", lines)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mimeType"], "image/png")
        self.assertNotIn("NOTE", text)
        self.assertNotIn("Depth pictures", text)
        # the new frame is the latest capture, and the game keeps none of the copies
        self.assertEqual(self.session.latest, 102)
        self.assertEqual(self.probe.released, [102])
        self.assertEqual(sorted(self.probe.reads), [0, 1])

    def test_pictures_hold_the_pixels_that_were_copied(self):
        from PIL import Image as PilImage

        self.call(hash=MESH)
        folder = self.root / "out" / "frame-102" / "draw-outputs"
        self.assertEqual(PilImage.open(folder / "draw00000-o0-before.png").convert("RGBA").tobytes(), pixels(False))
        self.assertEqual(PilImage.open(folder / "draw00000-o0-after.png").convert("RGBA").tobytes(), pixels(True))
        marked = PilImage.open(folder / "draw00000-o0-changed.png").convert("RGB")
        self.assertEqual(marked.getpixel((3, 1)), (200, 100, 50))      # what the draw painted stays as it is
        self.assertEqual(marked.getpixel((0, 0)), (2, 5, 7))           # the rest is dimmed

    def test_no_preview_when_it_is_not_wanted(self):
        text, error, images = self.call(hash=MESH, preview=False)
        self.assertFalse(error, text)
        self.assertEqual(images, [])
        self.assertNotIn("preview image", text)
        self.assertIn("what it touched", text)

    def test_a_box_is_reported_in_pixels_of_the_whole_target(self):
        self.probe.rts = [copy(0, box=(100, 50, 108, 54), boxed=True), copy(1, box=(100, 50, 108, 54), boxed=True)]
        text, error, _ = self.call(hash=MESH, box=[100, 50, 108, 54])
        self.assertFalse(error, text)
        self.assertIn("within [102, 51, 105, 53]", text)

    def test_a_draw_that_changes_nothing_is_said_to(self):
        self.probe.copies[1] = (META, pixels(False))
        text, error, images = self.call(hash=MESH)
        self.assertFalse(error, text)
        self.assertIn("    changed nothing in this target (culled, clipped, occluded or masked out", text)
        self.assertEqual(images, [])
        self.assertNotIn("what it touched", text)

    def test_depth_targets_are_reported_apart(self):
        depth_meta = dict(META, format=D32)
        far, near = struct.pack("<f", 1.0) * (WIDTH * HEIGHT), struct.pack("<f", 0.5) * (WIDTH * HEIGHT)
        self.probe.rts = [copy(0), copy(0, kind=11), copy(1), copy(1, kind=11)]
        self.probe.copies = {0: (META, pixels(False)), 1: (depth_meta, far), 2: (META, pixels(True)), 3: (depth_meta, near)}
        text, error, images = self.call(hash=MESH, depth=True)
        self.assertFalse(error, text)
        self.assertIn("    oD: dddddddd 8x4 D32_FLOAT", text)
        self.assertIn("    changed 32 pixels (100.00% of the copied area) within [0, 0, 8, 4]", text)
        self.assertIn("Depth pictures are scaled to each picture's own value range", text)
        self.assertEqual(len(images), 2)
        self.probe.copies[3] = (depth_meta, far)
        text, error, _ = self.call(hash=MESH, depth=True)
        self.assertIn("    changed nothing in this target (depth writes are off, or nothing passed the depth test)", text)

    def test_copies_that_were_not_made_say_why(self):
        for state, reason in (("over_budget", "not copied: over the pixel budget (use box, or select fewer draws)"),
                              ("multisampled", "not copied: the target is multisampled"),
                              ("not_2d", "not copied: the target is not a 2D texture"),
                              ("failed", "not copied: the copy could not be made")):
            with self.subTest(state=state):
                self.probe.rts = [copy(0, state), copy(1, state)]
                self.probe.stats = dict(COUNTS, rt_over_budget=2 if state == "over_budget" else 0, rt_bytes=0)
                self.probe.reads.clear()
                text, error, images = self.call(hash=MESH)
                self.assertFalse(error, text)
                self.assertIn(f"    before: {reason}", text)
                self.assertIn(f"    after: {reason}", text)
                self.assertNotIn("changed", text.split("\n", 2)[2].split("The pictures")[0])
                self.assertEqual((self.probe.reads, images), ([], []))
                self.assertEqual("2 copies left out, over the pixel budget (use box)" in text, state == "over_budget")

    def test_a_copy_from_after_the_draw_alone_is_still_saved(self):
        self.probe.rts = [copy(0, "over_budget"), copy(1)]
        self.probe.stats = dict(COUNTS, rt_over_budget=1)
        text, error, images = self.call(hash=MESH)
        self.assertFalse(error, text)
        self.assertIn("1 copy left out, over the pixel budget (use box)", text)
        self.assertIn("    before: not copied: over the pixel budget", text)
        self.assertIn("draw00000-o0-after.png", text)
        self.assertNotIn("changed", text.split("\n", 2)[2].split("The pictures")[0])
        self.assertEqual(self.probe.reads, [1])

    def test_index_data_that_is_not_the_selected_mesh_any_more_is_pointed_out(self):
        self.probe.ib_hash = "99999999"
        for arguments in ({"hash": MESH}, {"draws": [0]}):
            text, error, _ = self.call(capture=101, **arguments)
            self.assertFalse(error, text)
            self.assertIn("#0 DrawIndexed game ib=99999999 ic=6", text)
            self.assertIn("    NOTE: found by index buffer object and range, but the index data there hashes differently", text)
        # a shader selects whatever is drawn with it
        text, error, _ = self.call(hash=PS)
        self.assertNotIn("NOTE", text)

    def test_nothing_matched(self):
        self.probe.rts, self.probe.stats = [], dict(COUNTS, rt_matched=0, rt_draws=0, rt_bytes=0)
        text, error, images = self.call(hash=MESH)
        self.assertFalse(error, text)
        self.assertIn("draws that matched: 0; targets copied for 0 of them", text)
        self.assertIn("None of the watched draws was issued in this frame", text)
        self.assertEqual(self.probe.released, [102])

    def test_passes_without_a_colour_target_are_named(self):
        self.probe.stats = dict(COUNTS, rt_matched=2, rt_depth_only=1)
        text, _, _ = self.call(hash=MESH)
        self.assertIn("draws that matched: 2; targets copied for 1 of them; ", text)
        self.assertIn("1 matching draw has no colour target (a shadow or depth pass) and was not counted: depth=true", text)
        self.probe.stats = dict(COUNTS, rt_matched=5, rt_depth_only=4)
        text, _, _ = self.call(hash=MESH)
        self.assertIn("4 matching draws have no colour target (shadow or depth passes) and were not counted: depth=true", text)
        # only those: the frame holds nothing to show, and that is not "the mesh is off screen"
        self.probe.rts, self.probe.stats = [], dict(COUNTS, rt_matched=4, rt_draws=0, rt_depth_only=4, rt_bytes=0)
        text, _, _ = self.call(hash=MESH)
        self.assertIn("4 matching draws have no colour target", text)
        self.assertNotIn("None of the watched draws", text)

    def test_more_matches_than_the_limit_are_counted(self):
        self.probe.stats = dict(COUNTS, rt_matched=12, rt_draws=8)
        text, _, _ = self.call(hash=MESH)
        self.assertIn("draws that matched: 12; targets copied for 8 of them (the first 8 in frame order: raise max_draws, "
                      "or select narrower)", text)
        self.probe.stats = dict(COUNTS, rt_matched=3, rt_draws=2, rt_depth_only=1)
        text, _, _ = self.call(hash=MESH)
        self.assertNotIn("in frame order", text)

    def test_the_copies_are_freed_when_reading_them_fails(self):
        self.probe.copies[1] = ProbeError("timed out waiting for the game to present a frame")
        text, error, _ = self.call(hash=MESH)
        self.assertTrue(error, text)
        self.assertIn("timed out waiting for the game", text)
        self.assertEqual(self.probe.released, [102])

    def test_a_failing_release_does_not_hide_the_result(self):
        def refuse(frame):
            raise ProbeError("that capture is no longer held by the probe")

        self.probe.release_rt = refuse
        text, error, _ = self.call(hash=MESH)
        self.assertFalse(error, text)
        self.assertIn("changed 6 pixels", text)

    # ---------------------------------------------------------------- efmi_get_draw
    def test_get_draw_describes_state_samplers_and_views(self):
        text, error, _ = self.call("efmi_get_draw", draw=0)
        self.assertFalse(error, text)
        draw = json.loads(text)
        self.assertEqual(draw["blend"], {
            "alpha_to_coverage": False,
            "all_targets": {"enabled": True, "write_mask": "rgba", "src": "src_alpha", "dst": "inv_src_alpha", "op": "add",
                            "src_alpha": "one", "dst_alpha": "zero", "op_alpha": "add"},
            "blend_factor": [0.5, 0.5, 0.5, 0.5], "sample_mask": "0xffffffff"})
        self.assertEqual(draw["depth_stencil"], {"depth_test": True, "depth_write": True, "depth_func": "less_equal",
                                                 "stencil": False})
        self.assertEqual((draw["rasterizer"]["cull"], draw["rasterizer"]["fill"], draw["rasterizer"]["scissor"]),
                         ("back", "solid", False))
        # rectangles are bound, but this rasterizer state does not use them
        self.assertNotIn("scissor_rects", draw)
        self.assertEqual(draw["samplers"], {"ps-s0": {"filter": "min_mag_mip_linear", "address": "wrap/wrap/wrap",
                                                      "lod": [0, 12], "lod_bias": 0}})
        self.assertEqual(draw["viewport_depth_range"], [0, 1])
        self.assertNotIn("viewport_count", draw)
        self.assertNotIn("pipeline_state", draw)
        self.assertEqual(draw["textures"]["ps-t0"],
                         "eeeeeeee tex2d 64x64 R8G8B8A8_TYPELESS mips=4 | view: R8G8B8A8_UNORM_SRGB tex2d mips 0..+4")
        self.assertEqual(draw["outputs"], {"o0": "cccccccc tex2d 8x4 R8G8B8A8_UNORM mips=1",
                                           "oD": "dddddddd tex2d 8x4 D32_FLOAT mips=1 | view: D32_FLOAT tex2d mip 0 "
                                                 "read-only depth"})

    def test_get_draw_shows_scissor_rectangles_when_the_rasterizer_uses_them(self):
        doc = frame_doc(300)
        doc["states"] = STATES[:2] + [dict(STATES[2], sc=True)] + STATES[3:]
        doc["draws"][0].update(sc=[[1, 2, 3, 4], [0, 0, 8, 4]], vpn=2, vpz=[0.25, 0.75])
        self.session.add_capture(Capture(doc))
        draw = json.loads(self.call("efmi_get_draw", draw=0)[0])
        self.assertEqual(draw["scissor_rects"], [[1, 2, 3, 4], [0, 0, 8, 4]])
        self.assertEqual((draw["viewport_count"], draw["viewport_depth_range"]), (2, [0.25, 0.75]))

    def test_get_draw_of_a_frame_from_an_older_probe_says_what_is_missing(self):
        self.session.add_capture(Capture(frame_doc(200, state=False)))
        draw = json.loads(self.call("efmi_get_draw", draw=0)[0])
        self.assertIn("older than 0.1.5", draw["pipeline_state"])
        self.assertNotIn("blend", draw)
        self.assertEqual(draw["textures"]["ps-t0"], "eeeeeeee tex2d 64x64 R8G8B8A8_TYPELESS mips=4")
        # a light capture never has it, whatever the probe
        self.session.add_capture(Capture(frame_doc(201, detail="light")))
        draw = json.loads(self.call("efmi_get_draw", draw=0)[0])
        self.assertNotIn("pipeline_state", draw)
        self.assertNotIn("blend", draw)


class PipelineTests(unittest.TestCase):
    def test_no_state_object_means_the_defaults(self):
        blend = pipeline.describe_blend({"state": None, "factor": [1, 1, 1, 1], "sample_mask": 0xFFFFFFFF})
        self.assertEqual(blend, {"default": True, "alpha_to_coverage": False,
                                 "all_targets": {"enabled": False, "write_mask": "rgba"},
                                 "blend_factor": [1, 1, 1, 1], "sample_mask": "0xffffffff"})
        self.assertEqual(pipeline.describe_depth({"state": None, "stencil_ref": 0}),
                         {"default": True, "depth_test": True, "depth_write": True, "depth_func": "less", "stencil": False})
        raster = pipeline.describe_raster(None)
        self.assertEqual((raster["default"], raster["cull"], raster["fill"], raster["scissor"]), (True, "back", "solid", False))
        self.assertIsNone(pipeline.describe_sampler(None))

    def test_independent_blend_lists_every_target(self):
        state = {"k": "blend", "a2c": True, "ind": True,
                 "rt": [[1, 2, 2, 5, 9, 4, 3, 7], [0, 2, 1, 1, 2, 1, 1, 8]] + [[0, 2, 1, 1, 2, 1, 1, 0]] * 6}
        blend = pipeline.describe_blend({"state": state, "factor": [0, 0, 0, 0], "sample_mask": 1})
        self.assertTrue(blend["alpha_to_coverage"])
        self.assertNotIn("all_targets", blend)
        self.assertEqual(blend["targets"][0], {"enabled": True, "write_mask": "rgb", "src": "one", "dst": "one", "op": "max",
                                               "src_alpha": "dest_color", "dst_alpha": "inv_src_color",
                                               "op_alpha": "rev_subtract"})
        self.assertEqual(blend["targets"][1], {"enabled": False, "write_mask": "a"})
        self.assertEqual(blend["targets"][7], {"enabled": False, "write_mask": "none"})
        self.assertEqual(blend["sample_mask"], "0x00000001")

    def test_what_a_state_does_not_use_is_left_out(self):
        # D3D11 reports these as defaults or zero whatever the game asked for; they would only mislead.
        off = pipeline.describe_depth({"state": dict(STATES[1], de=False, dw=1, df=2), "stencil_ref": 9})
        self.assertEqual(off, {"depth_test": False, "stencil": False})
        point = pipeline.describe_sampler(dict(STATES[3], f=0, ma=0))
        self.assertEqual(point, {"filter": "min_mag_mip_point", "address": "wrap/wrap/wrap", "lod": [0, 12], "lod_bias": 0})
        aniso = pipeline.describe_sampler(dict(STATES[3], f=0x55, ma=8))
        self.assertEqual((aniso["filter"], aniso["max_anisotropy"]), ("anisotropic", 8))
        shadow = pipeline.describe_sampler(dict(STATES[3], f=0x95, cf=4, u=4, v=3, w=2, bc=[1, 1, 1, 1]))
        self.assertEqual((shadow["filter"], shadow["comparison"], shadow["address"], shadow["border_color"]),
                         ("comparison_min_mag_mip_linear", "less_equal", "border/clamp/mirror", [1, 1, 1, 1]))
        self.assertEqual(pipeline.describe_sampler(dict(STATES[3], f=0xD5))["filter"], "comparison_anisotropic")
        self.assertEqual(pipeline.filter_name(0x7F), "0x7f")

    def test_stencil_faces(self):
        state = dict(STATES[1], se=True, srm=0x3F, swm=0x0F, ff=[1, 4, 3, 6], bf=[2, 8, 6, 5])
        depth = pipeline.describe_depth({"state": state, "stencil_ref": 5})
        self.assertEqual(depth["stencil"], {"ref": 5, "read_mask": "0x3f", "write_mask": "0x0f",
                                            "front": {"func": "not_equal", "pass": "replace", "fail": "keep",
                                                      "depth_fail": "incr_sat"},
                                            "back": {"func": "greater", "pass": "invert", "fail": "zero", "depth_fail": "decr"}})

    def test_views_that_show_everything_in_the_format_of_the_texture_are_plain(self):
        srgb = dict(RESOURCES[3], fmt=RGBA_SRGB, fmt_name="R8G8B8A8_UNORM_SRGB")
        array = dict(srgb, array=6)

        def plain(raw, resource):
            return pipeline.view_is_plain(pipeline.view_info(raw), resource)

        self.assertTrue(plain([3, 0, RGBA_SRGB, 4, 0, 4, 0, 0, 0], srgb))
        self.assertTrue(plain([3, 0, RGBA_SRGB, 4, 0, 0xFFFFFFFF, 0, 0, 0], srgb))        # "all mips"
        self.assertTrue(plain([3, 0, RGBA_SRGB, 5, 0, 4, 0, 6, 0], array))
        self.assertTrue(plain([3, 0, RGBA_SRGB, 9, 0, 4, 0, 0, 0], array))                # a cube
        self.assertTrue(plain([3, 0, RGBA_SRGB, 10, 0, 4, 0, 1, 0], array))               # one cube of a cube array
        self.assertTrue(plain([1, 1, RGBA, 4, 0, 0, 0, 0, 0], RESOURCES[1]))
        self.assertTrue(plain([2, 2, D32, 3, 0, 0, 0, 0, 0], RESOURCES[2]))
        # another format, a part of the mips or slices, a read-only depth view, anything of a buffer
        self.assertFalse(plain([3, 0, RGBA_SRGB, 4, 0, 4, 0, 0, 0], RESOURCES[3]))
        self.assertFalse(plain([3, 0, RGBA_SRGB, 4, 1, 3, 0, 0, 0], srgb))
        self.assertFalse(plain([3, 0, RGBA_SRGB, 4, 0, 2, 0, 0, 0], srgb))
        self.assertFalse(plain([3, 0, RGBA_SRGB, 5, 0, 4, 2, 1, 0], array))
        self.assertFalse(plain([3, 0, RGBA_SRGB, 5, 0, 4, 0, 3, 0], array))
        self.assertFalse(plain([3, 1, RGBA_SRGB, 4, 2, 0, 0, 0, 0], srgb))
        self.assertFalse(plain([3, 1, RGBA_SRGB, 5, 0, 0, 3, 1, 0], array))
        self.assertFalse(plain([2, 2, D32, 3, 0, 0, 0, 0, 3], RESOURCES[2]))
        self.assertFalse(plain([0, 0, 0, 1, 0, 64, 0, 0, 0], RESOURCES[0]))
        self.assertFalse(plain([1, 1, RGBA, 4, 0, 0, 0, 0, 0], None))

    def test_view_texts(self):
        def text(raw):
            return pipeline.view_text(pipeline.view_info(raw))

        self.assertEqual(text([3, 0, RGBA_SRGB, 4, 1, 0xFFFFFFFF, 0, 0, 0]), "R8G8B8A8_UNORM_SRGB tex2d mips 1..+all")
        self.assertEqual(text([3, 0, RGBA, 10, 0, 1, 6, 2, 0]), "R8G8B8A8_UNORM cube_array mips 0..+1 faces from 6..+2")
        self.assertEqual(text([3, 1, RGBA, 8, 1, 0, 4, 8, 0]), "R8G8B8A8_UNORM tex3d mip 1 w 4..+8")
        self.assertEqual(text([2, 2, 45, 3, 0, 0, 0, 0, 3]), "D24_UNORM_S8_UINT tex2d mip 0 read-only depth+stencil")
        self.assertEqual(text([0, 0, 39, 11, 4, 16, 0, 0, 1]), "R32_TYPELESS buffer elements 4..+16 flags raw")
        self.assertEqual(text([0, 3, 0, 1, 0, 100, 0, 0, 6]), "UNKNOWN buffer elements 0..+100 flags append,counter")
        self.assertEqual(text([3, 0, RGBA, 6, 0, 0, 0, 0, 0]), "R8G8B8A8_UNORM tex2d_ms")
        self.assertEqual(pipeline.view_text(None), "")

    def test_indirect_arguments(self):
        self.assertEqual(pipeline.indirect_args(6, struct.pack("<IIIiI", 294, 2, 12, -3, 1)),
                         {"index_count": 294, "instance_count": 2, "first_index": 12, "first_vertex": -3, "first_instance": 1})
        self.assertEqual(pipeline.indirect_args(5, struct.pack("<IIII", 6, 4, 2, 1)),
                         {"vertex_count": 6, "instance_count": 4, "first_vertex": 2, "first_instance": 1})
        self.assertEqual(pipeline.indirect_args(9, struct.pack("<III", 8, 4, 1)), {"thread_groups": [8, 4, 1]})
        # not read back yet, cut short, or not an indirect call
        self.assertIsNone(pipeline.indirect_args(6, None))
        self.assertIsNone(pipeline.indirect_args(6, b"\0" * 16))
        self.assertIsNone(pipeline.indirect_args(4, b"\0" * 20))

    def test_capture_reads_the_additions_and_does_without_them(self):
        new = Capture(frame_doc(1)).draw(0)
        self.assertEqual(new["blend"]["state"]["k"], "blend")
        self.assertEqual((new["depth_stencil"]["stencil_ref"], new["viewport_count"], new["depth_range"]), (0, 1, [0, 1]))
        self.assertEqual(new["samplers"]["ps-s0"]["k"], "sampler")
        self.assertEqual(new["tex"]["ps-t0"]["view"]["format"], "R8G8B8A8_UNORM_SRGB")
        self.assertEqual(new["out"]["oD"]["view"], {"kind": "dsv", "format": "D32_FLOAT", "dimension": "tex2d",
                                                    "mip_slice": 0, "read_only": ["depth"]})
        old = Capture(frame_doc(2, state=False))
        draw = old.draw(0)
        self.assertEqual((old.states, old.views, old.rts), ([], [], []))
        self.assertTrue(all(key not in draw for key in ("blend", "depth_stencil", "rasterizer", "samplers", "scissors")))
        self.assertEqual(draw["tex"]["ps-t0"], {"res": 3, "hash": "eeeeeeee"})
        # a state or view number the document does not hold is "none", not an error
        self.assertIsNone(old.state(0))
        self.assertIsNone(Capture(frame_doc(3)).view(99))


class ChangedRegionTests(unittest.TestCase):
    def test_count_box_and_mask(self):
        count, box, mask = draw_outputs.changed_region(pixels(False), pixels(True), WIDTH, HEIGHT, WIDTH * 4)
        self.assertEqual((count, box), (6, [2, 1, 5, 3]))
        self.assertEqual(mask.shape, (HEIGHT, WIDTH))
        self.assertTrue(mask[1, 2] and not mask[0, 0])

    def test_nothing_changed(self):
        count, box, mask = draw_outputs.changed_region(pixels(True), pixels(True), WIDTH, HEIGHT, WIDTH * 4)
        self.assertEqual((count, box), (0, None))
        self.assertFalse(mask.any())

    def test_one_byte_of_a_wide_pixel_is_a_change(self):
        before = bytes(WIDTH * HEIGHT * 8)
        after = bytearray(before)
        after[(3 * WIDTH + 7) * 8 + 5] = 1                 # last pixel, one byte of its eight
        count, box, _ = draw_outputs.changed_region(before, bytes(after), WIDTH, HEIGHT, WIDTH * 8)
        self.assertEqual((count, box), (1, [7, 3, 8, 4]))

    def test_copies_that_do_not_fit_together_are_not_compared(self):
        for before, after, row_bytes in ((pixels(False), pixels(True)[:-4], WIDTH * 4),
                                         (pixels(False), pixels(True), WIDTH * 4 + 2),
                                         (pixels(False)[:16], pixels(True)[:16], WIDTH * 4)):
            self.assertEqual(draw_outputs.changed_region(before, after, WIDTH, HEIGHT, row_bytes), (None, None, None))
        self.assertEqual(draw_outputs.changed_region(b"", b"", 0, 0, 0), (None, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=1)
