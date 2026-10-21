"""The ZZMI model workflow offline: a live frame written for LoyalTools, and the in-game comparison rules.

A fake probe answers with a frame drawn the way Zenless Zone Zero draws a
character: a Draw streams the posed vertices of the Position / Texcoord / Blend
buffers into a buffer (stream output), with an index buffer left bound from an
earlier draw, and the visible DrawIndexed reads that buffer as vb0 and vb3. The
frame is written as a frame analysis folder (log.txt in XXMI's syntax) and
LoyalTools' own ZZMI extractor, as installed in Blender, has to find the skinned
mesh in it and extract the very bytes of the frame. Skipped when the add-on is
not installed. No game, no Blender run, no key press.
"""
from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import blender_runner, mod_runtime, model_zzmi, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import crc32c, hex32  # noqa: E402
from efmi_mcp.probe_client import ProbeEndpoint  # noqa: E402

R32G32B32_FLOAT, R32G32B32A32_FLOAT, R32G32B32A32_UINT, R8G8B8A8_UNORM, R16G16_FLOAT, R32_UINT = 6, 2, 3, 28, 34, 42
VS_POSE, VS_LIT, PS_LIT = "e8425f64cfb887cd", "c280f6945b23a42a", "1234567890abcdef"
VERTICES = 4


def position(n: int) -> bytes:
    return struct.pack("<3f3f4f", n, n * 2.0, n * 3.0, 0, 1, 0, 1, 0, 0, 1)                 # 40 bytes


def texcoord(n: int) -> bytes:
    return struct.pack("<4B", 255, 255, 255, 255) + struct.pack("<2e", n / 4, 1 - n / 4)    # 8 bytes


def blend(n: int) -> bytes:
    return struct.pack("<4f4I", 1.0, 0, 0, 0, n % 2, 0, 0, 0)                               # 32 bytes


POSITIONS = b"".join(position(n) for n in range(VERTICES))
TEXCOORDS = b"".join(texcoord(n) for n in range(VERTICES))
BLENDS = b"".join(blend(n) for n in range(VERTICES))
POSED = bytes(len(POSITIONS))                  # what the skinning draw streamed out (not read by the extractor)
INDICES = struct.pack("<6I", 0, 1, 2, 2, 1, 3)
OTHER_INDICES = struct.pack("<3I", 0, 1, 2)    # left bound from an earlier draw
PALETTE = struct.pack("<24f", *([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0] * 2))   # two bones, 3 float4 each
OBJECT = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)

# id: (bytes, kind, bind, stride, hash)
BUFFERS = {0: (POSITIONS, 1, 40, "66456501"), 1: (TEXCOORDS, 1, 8, "5174be85"), 2: (BLENDS, 1, 32, "0fc8027f"),
           3: (POSED, 1 | 16, 40, "4ac4d074"), 4: (INDICES, 2, 0, "9040e2c4"), 5: (OTHER_INDICES, 2, 0, "78f5c854"),
           6: (PALETTE, 8, 0, "3ccf2db6"), 7: (OBJECT, 4, 0, "56d656ba")}
LAYOUT_POSE = [["POSITION", 0, R32G32B32_FLOAT, 0, 0, 0, 0], ["NORMAL", 0, R32G32B32_FLOAT, 0, 12, 0, 0],
               ["TANGENT", 0, R32G32B32A32_FLOAT, 0, 24, 0, 0], ["COLOR", 0, R8G8B8A8_UNORM, 1, 0, 0, 0],
               ["TEXCOORD", 0, R16G16_FLOAT, 1, 4, 0, 0], ["BLENDWEIGHT", 0, R32G32B32A32_FLOAT, 2, 0, 0, 0],
               ["BLENDINDICES", 0, R32G32B32A32_UINT, 2, 16, 0, 0]]
LAYOUT_LIT = LAYOUT_POSE[:5]


def frame_doc(frame: int) -> dict:
    resources = [{"id": rid, "known": True, "origin": "game", "hash": h, "kind": "buffer", "size": len(data),
                  "bind": bind, "stride": stride} for rid, (data, bind, stride, h) in sorted(BUFFERS.items())]
    # contents: one version per buffer, in the order of the resources
    cont = [[rid, 0, 0] for rid in sorted(BUFFERS)]
    pose = {"i": 0, "t": 1, "o": "game", "vc": VERTICES, "fv": 0, "topo": 1, "il": 0, "ib": [5, R32_UINT, 0, -1],
            "vb": [[0, 0, 40, 0, -1], [1, 1, 8, 0, -1], [2, 2, 32, 0, -1]], "so": [[0, 3]],
            "sh": [0, -1, -1, -1, -1, -1], "cb": [[0, 1, 7, 0, 4, -1]], "srv": [[0, 0, 6]], "srvv": [0],
            "cib": 5, "cvb": [0, 1, 2], "ccb": [7], "csrv": [6]}
    lit = {"i": 1, "t": 2, "o": "game", "ic": 6, "fi": 0, "fv": 0, "topo": 4, "il": 1, "ib": [4, R32_UINT, 0, -1],
           "vb": [[0, 3, 40, 0, -1], [1, 1, 8, 0, -1], [2, 2, 32, 0, -1], [3, 3, 40, 0, -1]],
           "sh": [1, -1, -1, -1, 2, -1], "cb": [[0, 1, 7, 0, 4, -1]], "cib": 4, "cvb": [3, 1, 2, 3], "ccb": [7]}
    return {"frame": frame, "held": True, "detail": "full", "resolution": [4, 4], "resources": resources,
            "shaders": [{"id": 0, "known": True, "type": "vs", "hash": VS_POSE},
                        {"id": 1, "known": True, "type": "vs", "hash": VS_LIT},
                        {"id": 2, "known": True, "type": "ps", "hash": PS_LIT}],
            "layouts": [{"id": 0, "known": True, "elems": LAYOUT_POSE}, {"id": 1, "known": True, "elems": LAYOUT_LIT}],
            "views": [[6, 0, R32G32B32A32_FLOAT, 1, 0, 6, 0, 0, 0]], "regions": [], "snaps": [],
            "cont": cont, "ev": [], "xxmi_settings": {"track_region_hashes": False},
            "draws": [pose, lit], "stats": {"ct_snaps": len(cont), "ct_bytes": 0, "events": 0}}


class FakeProbe:
    def __init__(self):
        self.endpoint = ProbeEndpoint(4321, "fake-never-opened")
        self.connected = True
        self.frame = 100
        self.xxmi_settings = {"track_region_hashes": False}

    def status(self):
        return {"pid": 4321, "probe": "0.1.12", "features": ["contents", "stream_output"], "attached": True}

    def capture(self, **request):
        self.frame += 1
        return frame_doc(self.frame), b""

    def read_content(self, capture_frame, snap, offset=0, size=0, timeout_ms=5000):
        data = BUFFERS[snap][0]
        size = size or len(data) - offset
        return {"offset": offset, "size": size, "byte_width": len(data), "exact": True, "from": "copy"}, \
            data[offset:offset + size]


def installed_loyaltools() -> Path | None:
    try:
        addon = blender_runner.find_addon(blender_runner.find_blender(""))
    except blender_runner.BlenderError:
        return None
    return addon if addon and (addon / "extract" / "zzmi_dump_extractor.py").is_file() else None


class LiveFrame(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="zzmi-model-unit-", dir=build)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")
        self.session.client = FakeProbe()

    def test_the_calls_with_files_are_the_skinning_draw_and_the_draws_that_read_it(self):
        cap = Capture(frame_doc(1))
        self.assertEqual(model_zzmi.skinned_calls(cap), {0, 1})

    def test_loyaltools_reads_the_written_frame(self):
        addon = installed_loyaltools()
        if addon is None:
            self.skipTest("no LoyalTools with a ZZMI extractor is installed in Blender")
        folder = self.root / "frame"
        written = model_zzmi.live_frame(self.session, folder)
        self.assertEqual(written["calls_with_files"], 2)
        log = (folder / "log.txt").read_text(encoding="utf-8")
        self.assertIn("SOSetTargets(NumBuffers:1", log)
        listing = model_zzmi.list_models(folder, addon)
        self.assertEqual(listing["meshes"][0]["ib_hash"], "9040e2c4")
        self.assertTrue(listing["meshes"][0]["skinned"], listing)
        module = model_zzmi.load_extractor(addon)
        workspace = self.root / "workspace"
        result = module.ZZMIDumpExtractor(str(folder)).extract(["9040e2c4"], str(workspace), copy_textures=False)
        self.assertEqual(len(result.json_paths), 1, result.warnings)
        submesh = json.loads(Path(result.json_paths[0]).read_text(encoding="utf-8"))
        part = Path(result.json_paths[0]).parent
        files = {entry["FileName"].rsplit("-", 1)[-1]: (part / entry["FileName"]).read_bytes()
                 for entry in submesh["CategoryBufferList"]}
        # The unposed vertices of the skinning draw, not what it streamed out.
        self.assertEqual(files["Position.buf"], POSITIONS)
        self.assertEqual(files["Texcoord.buf"], TEXCOORDS)
        self.assertEqual(files["Blend.buf"], BLENDS)
        self.assertEqual(submesh["ZZMIFrameExtract"]["DrawVB"], "4ac4d074")
        self.assertTrue(submesh["ZZMIFrameExtract"]["PreSkinned"])


class InGameComparison(unittest.TestCase):
    """mod_runtime.verify_capture on a ZZMI export: the streamed vb0 is not compared, faces keep the game's mesh."""

    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="zzmi-verify-unit-", dir=build)
        self.addCleanup(self.tmp.cleanup)
        self.mod = Path(self.tmp.name)
        (self.mod / "Meshes").mkdir()
        for name, data in (("Position.buf", POSITIONS), ("Texcoord.buf", TEXCOORDS), ("Blend.buf", BLENDS),
                           ("Index.buf", INDICES)):
            (self.mod / "Meshes" / name).write_bytes(data)
        files = [{"path": f"Meshes/{n}", "bytes": len(d), "sha256": __import__("hashlib").sha256(d).hexdigest()}
                 for n, d in (("Position.buf", POSITIONS), ("Texcoord.buf", TEXCOORDS), ("Blend.buf", BLENDS),
                              ("Index.buf", INDICES))]
        self.manifest = {"game": "ZZMI", "mod_dir": str(self.mod), "files": files,
                         "draws": [{"section": "TextureOverride_body", "index_count": 6, "first_index": 0,
                                    "base_vertex": 0, "ib_file": "Meshes/Index.buf"}],
                         "vertex_draws": [{"section": "TextureOverride_VB_body_Blend", "vertex_count": VERTICES,
                                           "first_vertex": 0, "vb0_file": "Meshes/Position.buf"}],
                         "entry_points": [{"section": "TextureOverride_IB_body", "hash": "9040e2c4", "skips": True},
                                          {"section": "TextureOverride_body", "hash": "9040e2c4", "skips": False,
                                           "match_first_index": 0, "match_index_count": 6,
                                           "index_files": ["Meshes/Index.buf"]},
                                          {"section": "TextureOverride_face", "hash": "d58b6350", "skips": False,
                                           "match_first_index": 0, "match_index_count": 3}]}

    def frame(self, mod_on: bool) -> tuple[Capture, object]:
        """After the reload: XXMI's skinning draw and indexed draw with the mod's files, the game's face draw."""
        data = {0: POSITIONS, 1: TEXCOORDS, 2: BLENDS, 3: POSED, 4: INDICES, 5: OTHER_INDICES}
        origin = {0: "xxmi", 1: "xxmi", 2: "xxmi", 3: "game", 4: "xxmi", 5: "game"}
        resources = [{"id": rid, "known": True, "origin": origin[rid], "hash": hex32(crc32c(d)), "kind": "buffer",
                      "size": len(d)} for rid, d in sorted(data.items())]
        regions = [[rid, 0, len(d), hex32(crc32c(d)), "ok"] for rid, d in sorted(data.items())]
        draws = [{"i": 0, "t": 1, "o": "xxmi", "vc": VERTICES, "fv": 0, "topo": 1,
                  "vb": [[0, 0, 40, 0, 0], [2, 2, 32, 0, 2]], "so": [[0, 3]], "sh": [0, -1, -1, -1, -1, -1]},
                 {"i": 1, "t": 2, "o": "xxmi", "ic": 6, "fi": 0, "fv": 0, "topo": 4, "ib": [4, R32_UINT, 0, 4],
                  "vb": [[0, 3, 40, 0, 3], [1, 1, 8, 0, 1], [2, 2, 32, 0, 2]], "sh": [1, -1, -1, -1, 2, -1]},
                 # the face: the game's own mesh, with the mod's textures (a texture-only override)
                 {"i": 2, "t": 2, "o": "game", "ic": 3, "fi": 0, "fv": 0, "topo": 4, "ib": [5, R32_UINT, 0, 5],
                  "vb": [[0, 3, 40, 0, 3]], "sh": [1, -1, -1, -1, 2, -1]}]
        if not mod_on:
            draws = [draws[2], {**draws[1], "i": 3, "o": "game"}]
        doc = {"frame": 7, "detail": "full", "resources": resources, "regions": regions,
               "xxmi_settings": {"track_region_hashes": False}, "draws": draws}
        # The face's index buffer is named d58b6350 by ZZMI (the whole buffer's hash).
        doc["resources"][5]["hash"] = "d58b6350"
        doc["resources"][4]["hash"] = "9040e2c4" if not mod_on else doc["resources"][4]["hash"]

        class Reader:
            def read_buffer(self, frame, rid, offset=0, size=0, **kwargs):
                return {}, data[rid][offset:offset + (size or len(data[rid]))]
        return Capture(doc), Reader()

    def test_the_mod_passes_with_its_skinning_draw_and_the_face_kept(self):
        cap, reader = self.frame(mod_on=True)
        result = mod_runtime.verify_capture(cap, reader, self.manifest)
        self.assertEqual(result["target_mod_status"], "passed", result["reasons"])
        self.assertEqual(result["mod_draws"][0]["vertex_buffers"]["vb0"], "stream_output")
        self.assertEqual([d["vertex_file"] for d in result["vertex_draws_of_this_mod"]], ["Meshes/Position.buf"])
        statuses = {c["name"]: c["status"] for c in result["components"]}
        self.assertEqual(statuses["TextureOverride_face"], "game_mesh_kept_by_export")
        self.assertIn("TextureOverride_face", result["covered"])
        self.assertEqual(result["exported_buffers_seen"]["Meshes/Position.buf"]["whole_buffer"], "identical")

    def test_the_game_drawing_a_replaced_mesh_fails(self):
        cap, reader = self.frame(mod_on=False)
        result = mod_runtime.verify_capture(cap, reader, self.manifest)
        self.assertEqual(result["target_mod_status"], "failed", result)

    def test_for_efmi_every_entry_point_still_replaces(self):
        cap, reader = self.frame(mod_on=True)
        manifest = {**self.manifest, "game": "EFMI"}
        result = mod_runtime.verify_capture(cap, reader, manifest)
        # EFMI's core replaces what an entry point names, so the face drawn by the game is a failure there.
        self.assertEqual(result["target_mod_status"], "failed")


class ProjectKind(unittest.TestCase):
    def test_the_game_of_a_project_folder(self):
        build = ROOT / "sandbox" / "build"
        with tempfile.TemporaryDirectory(prefix="zzmi-kind-", dir=build) as tmp:
            root = Path(tmp)
            self.assertIsNone(model_zzmi.project_game(root))
            (root / "workplace").mkdir()
            (root / "workplace" / "ZZMI_MergedSkeleton.json").write_text("{}", encoding="utf-8")
            self.assertEqual(model_zzmi.project_game(root), "ZZMI")
            (root / "workplace" / "EFMI_MergedSkeleton.json").write_text("{}", encoding="utf-8")
            self.assertEqual(model_zzmi.project_game(root), "EFMI")


if __name__ == "__main__":
    unittest.main()
