"""Offline checks of what differs between XXMI installations (EFMI, ZZMI) and of the ZZMI paths.

No game, no installed XXMI and no key press; each test owns a private folder.
The real ZZMI core, F6 and F10 are exercised by zzmi_integration_test.py in a
private sandbox copy.

  - which installation a folder is, its hash settings and its mods switch binding;
  - ib/vb hashes as mods use them: a part of the buffer (EFMI) or the whole buffer (ZZMI);
  - which mod sections replace a game draw, and how the draws tell the switch's state;
  - log.txt in XXMI's own syntax, stream-output targets included.
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
sys.path.insert(0, str(ROOT / "sandbox"))

from efmi_mcp import contents, mods, mods_toggle, tools, xxmi_install  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.probe_client import ProbeNotFound  # noqa: E402
from contents_parity_test import replay_log  # noqa: E402
from contents_unit_test import FakeProbe, call, frame_doc  # noqa: E402

ZZMI_D3DX = """[Loader]
target = ZenlessZoneZero.exe
[Include]
include = Core\\ZZMI\\main.ini
include_recursive = Mods
[Hunting]
hunting = 2
reload_config = no_modifiers VK_F10
show_original = no_modifiers VK_F9
[Rendering]
track_region_hashes = 0
"""
ZZMI_MAIN = """[KeyToggleMods]
Key = no_modifiers F6
$costume_mods = 0, 1
type = cycle
"""


class Folder(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="zzmi-unit-", dir=build)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class Installation(Folder):
    def test_zzmi_is_recognised_with_its_settings_and_key(self):
        self.write("d3dx.ini", ZZMI_D3DX)
        self.write("Core/ZZMI/main.ini", ZZMI_MAIN)
        install = xxmi_install.detect(self.root)
        self.assertEqual((install.name, install.target, install.region_hashes), ("ZZMI", "zenlesszonezero.exe", False))
        self.assertIs(install.toggle, xxmi_install.ZZMI_TOGGLE)
        self.assertFalse(install.is_efmi)
        self.assertEqual(xxmi_install.toggle_binding(install),
                         {"KeyToggleMods": "no_modifiers F6", "file": r"Core\ZZMI\main.ini", "variable": "$costume_mods"})

    def test_efmi_is_recognised(self):
        self.write("d3dx.ini", "[Include]\ninclude = Core\\EFMI\\main.ini\n[Rendering]\ntrack_region_hashes = 1\n")
        install = xxmi_install.detect(self.root)
        self.assertEqual((install.name, install.region_hashes, install.toggle), ("EFMI", True, xxmi_install.EFMI_TOGGLE))

    def test_a_folder_that_names_no_core_is_treated_as_efmi(self):
        install = xxmi_install.detect(self.root)
        self.assertEqual((install.name, install.region_hashes), ("EFMI", True))
        self.assertTrue(install.notes)

    def test_another_core_is_named_and_has_no_key(self):
        self.write("d3dx.ini", "[Include]\ninclude = Core\\GIMI\\main.ini\n")
        install = xxmi_install.detect(self.root)
        self.assertEqual((install.name, install.region_hashes, install.toggle), ("GIMI", False, None))
        with self.assertRaisesRegex(ValueError, "no known key"):
            xxmi_install.toggle_binding(install)

    def test_a_changed_zzmi_binding_or_a_hunting_key_on_f6_is_refused(self):
        self.write("d3dx.ini", ZZMI_D3DX)
        self.write("Core/ZZMI/main.ini", ZZMI_MAIN.replace("F6", "F7"))
        with self.assertRaisesRegex(ValueError, "expected one \\[KeyToggleMods\\]"):
            xxmi_install.toggle_binding(xxmi_install.detect(self.root))
        self.write("Core/ZZMI/main.ini", ZZMI_MAIN.replace("F6", "VK_F6"))
        xxmi_install.toggle_binding(xxmi_install.detect(self.root))         # the same key, spelled with VK_
        self.write("d3dx.ini", ZZMI_D3DX.replace("show_original = no_modifiers VK_F9", "show_original = no_modifiers F6"))
        with self.assertRaisesRegex(ValueError, "show_original is bound to F6"):
            xxmi_install.toggle_binding(xxmi_install.detect(self.root))
        self.write("d3dx.ini", ZZMI_D3DX.replace("show_original = no_modifiers VK_F9", "show_original = ctrl F6"))
        xxmi_install.toggle_binding(xxmi_install.detect(self.root))         # needs Ctrl: F6 alone does not set it off


def mesh_doc(settings: dict | None) -> dict:
    doc = {"frame": 1, "detail": "full",
           "resources": [{"kind": "buffer", "size": 600, "known": True, "hash": "aaaa0000"},
                         {"kind": "buffer", "size": 4096, "known": True, "hash": "bbbb0000"}],
           "regions": [[0, 0, 60, "aaaa1111", "ok"], [1, 0, 320, "bbbb1111", "ok"]],
           "draws": [{"i": 0, "t": 4, "o": "game", "ic": 30, "fi": 0, "inst": 1, "topo": 4, "ib": [0, 57, 0, 0],
                      "vb": [[0, 1, 32, 0, 1]], "sh": [-1, -1, -1, -1, -1, -1]}]}
    if settings is not None:
        doc["xxmi_settings"] = settings
    return doc


class Hashes(unittest.TestCase):
    def test_region_hashes_for_efmi_whole_buffers_for_zzmi(self):
        for settings, ib, vb in ((None, "aaaa1111", "bbbb1111"),
                                 ({"track_region_hashes": True}, "aaaa1111", "bbbb1111"),
                                 ({"track_region_hashes": False}, "aaaa0000", "bbbb0000")):
            cap = Capture(mesh_doc(settings))
            d = cap.draw(0)
            self.assertEqual((d["ib"]["hash"], d["vb"][0]["hash"], cap.ib_hash(cap.raw_draws[0])), (ib, vb, ib), settings)
            self.assertEqual((d["ib"]["resource_hash"], d["vb"][0]["resource_hash"]), ("aaaa0000", "bbbb0000"))


class Switch(Folder):
    """Which game draws a mod replaces, and the state of the mods switch as the draws show it (ZZMI prints nothing)."""

    def setUp(self):
        super().setUp()
        self.write("d3dx.ini", ZZMI_D3DX)
        self.write("Mods/m/mod.ini", "\n".join([
            "[TextureOverrideBodyIB]", "handling = skip", "hash = 11111111",            # skip before the hash
            "[TextureOverrideFace]", "hash = 22222222", "run = CommandList\\ZZMI\\SetTextures",   # textures only
            "[TextureOverridePart]", "hash = 33333333", "match_first_index = 300", "handling = skip",
            "[TextureOverrideOdd]", "hash = 44444444", "match_priority = 1", "handling = skip", ""]))
        self.session = tools.Session(self.root, self.root / "out", self.root / "probe.dll")

    def frame(self, game: list[str], xxmi_mesh: int = 0, overlay: int = 1, first_index: int = 0) -> Capture:
        hashes = ["11111111", "22222222", "33333333", "44444444", "55555555"]
        resources = [{"kind": "buffer", "size": 64, "known": True, "hash": h} for h in hashes]
        draws = []
        for h in game:
            draws.append({"i": len(draws), "t": 4, "o": "game", "ic": 30, "fi": first_index, "topo": 4,
                          "ib": [hashes.index(h), 57, 0, -1], "sh": [0, -1, -1, -1, 1, -1]})
        for _ in range(xxmi_mesh):       # a mod's mesh, drawn with the game's shaders
            draws.append({"i": len(draws), "t": 4, "o": "xxmi", "ic": 30, "fi": 0, "topo": 4, "ib": [4, 57, 0, -1],
                          "sh": [0, -1, -1, -1, 1, -1]})
        for _ in range(overlay):         # XXMI's text, drawn with its own shaders
            draws.append({"i": len(draws), "t": 4, "o": "xxmi", "ic": 6, "fi": 0, "topo": 4, "ib": [4, 57, 0, -1],
                          "sh": [7, -1, -1, -1, 8, -1]})
        doc = {"frame": 10, "detail": "light", "resources": resources, "xxmi_settings": {"track_region_hashes": False},
               "draws": draws}
        return Capture(doc)

    def summary(self, cap):
        return mods_toggle.drawn_summary(self.session, cap)

    def test_the_index_records_which_sections_skip_the_game_draw(self):
        refs = {r.section: r for r in mods.scan_mods(self.root).refs}
        self.assertEqual({name: r.skips for name, r in refs.items()},
                         {"TextureOverrideBodyIB": True, "TextureOverrideFace": False, "TextureOverridePart": True,
                          "TextureOverrideOdd": True})

    def test_only_draws_a_skip_section_matches_count(self):
        drawn = self.summary(self.frame(["11111111", "22222222", "33333333", "44444444"]))
        # the face only swaps textures; the part section wants first index 300; match_priority cannot be judged
        self.assertEqual(drawn["game_draws_of_meshes_that_mods_override"], 1)
        self.assertEqual(self.summary(self.frame(["33333333"], first_index=300))["game_draws_of_meshes_that_mods_override"], 1)

    def test_xxmi_overlay_text_is_not_a_mod_mesh(self):
        self.assertEqual(self.summary(self.frame(["22222222"], overlay=3))["mesh_draws_by_xxmi"], 0)
        self.assertEqual(self.summary(self.frame(["22222222"], xxmi_mesh=2))["mesh_draws_by_xxmi"], 2)

    def test_the_state_from_the_draws(self):
        off = self.summary(self.frame(["11111111", "22222222"]))
        on_drawing = self.summary(self.frame(["22222222"], xxmi_mesh=1))
        on_hiding = self.summary(self.frame(["22222222"]))
        self.assertIs(mods_toggle.draws_evidence(off, 2)["mods_enabled"], False)
        self.assertIs(mods_toggle.draws_evidence(on_drawing, 2)["mods_enabled"], True)
        # A mod that only hides: nothing to see, unless the frame before the key had the game drawing it.
        self.assertIsNone(mods_toggle.draws_evidence(on_hiding, 2)["mods_enabled"])
        evidence = mods_toggle.draws_evidence(on_hiding, 2, off, 1)
        self.assertIs(evidence["mods_enabled"], True)
        self.assertEqual(evidence["before_key"], {"frame": 1, "game_draws_of_meshes_that_mods_override": 1})
        # The game and a mod both drawing it is no clear picture.
        mixed = self.summary(self.frame(["11111111"], xxmi_mesh=1))
        self.assertIsNone(mods_toggle.draws_evidence(mixed, 2, off, 1)["mods_enabled"])


def skinning_doc() -> dict:
    """Zenless Zone Zero's way: a Draw streams the posed vertices into a buffer the indexed draws read as vb0."""
    res = [{"kind": "buffer", "size": 120, "known": True, "hash": h, "bind": 1} for h in ("00000001", "00000002", "00000003")]
    res += [{"kind": "buffer", "size": 120, "known": True, "hash": "0000000a", "bind": 17},        # stream-output target
            {"kind": "buffer", "size": 36, "known": True, "hash": "0000000b", "bind": 2},          # index buffer
            {"kind": "buffer", "size": 48 * 4, "known": True, "hash": "0000000c", "bind": 8}]      # bone palette
    shaders = [{"known": True, "hash": h} for h in ("1000000000000001", "2000000000000002", "3000000000000003")]
    pose = {"i": 0, "t": 1, "o": "game", "vc": 3, "fv": 0, "topo": 1, "il": -1,
            "vb": [[0, 0, 40, 0, -1], [1, 1, 20, 0, -1], [2, 2, 32, 0, -1]], "so": [[0, 3]],
            "sh": [0, -1, -1, 1, -1, -1], "srv": [[0, 0, 5]]}
    lit = {"i": 1, "t": 2, "o": "game", "ic": 9, "fi": 0, "fv": 0, "topo": 4, "il": -1, "ib": [4, 42, 0, -1],
           "vb": [[0, 3, 40, 0, -1], [1, 1, 20, 0, -1], [2, 2, 32, 0, -1], [3, 3, 40, 0, -1]],
           "sh": [2, -1, -1, -1, 2, -1]}
    second = dict(lit, i=2, ic=3, fi=9)
    return {"frame": 5, "detail": "full", "resources": res, "shaders": shaders, "regions": [],
            "xxmi_settings": {"track_region_hashes": False}, "draws": [pose, lit, second]}


class XxmiLog(unittest.TestCase):
    def test_set_lines_in_xxmis_syntax_with_stream_output(self):
        cap = Capture(skinning_doc())
        state: dict = {}
        lines = []
        for n in range(len(cap)):
            lines += [line if line.startswith(" ") else f"{n:06d} {line}"
                      for line in contents.xxmi_state_lines(cap, cap.draw(n), cap.raw_draws[n], state)]
            lines.append(f"{n:06d} {contents.call_line(cap, cap.draw(n))}")
        text = "\n".join(lines)
        self.assertIn("000000 SOSetTargets(NumBuffers:1, ppSOTargets:0x0000000000000000, pOffsets:0x0000000000000000)\n"
                      "       0: resource=0x0000000000000004 hash=0000000a", text)
        self.assertIn("000000 GSSetShader(pShader:0x0000000000000002, ppClassInstances:0x0000000000000000, "
                      "NumClassInstances:0) hash=2000000000000002", text)
        self.assertIn("000001 IASetIndexBuffer(pIndexBuffer:0x0000000000000005, Format:42, Offset:0) hash=0000000b", text)
        self.assertIn("000001 SOSetTargets(NumBuffers:0", text)          # the visible draw streams nothing out
        self.assertNotIn("000002 IASet", text)                           # nothing changed for the second part
        self.assertIn("000000 Draw(VertexCount:3, StartVertexLocation:0)", text)
        folder = tempfile.TemporaryDirectory(prefix="zzmi-log-", dir=ROOT / "sandbox" / "build")
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "log.txt"
        path.write_text(text + "\n", encoding="utf-8")
        draws = replay_log(path)
        self.assertEqual([d["call"] for d in draws], [0, 1, 2])
        self.assertEqual(draws[0]["vb"], {0: "00000001", 1: "00000002", 2: "00000003"})
        self.assertEqual(draws[1]["vb"], {0: "0000000a", 1: "00000002", 2: "00000003", 3: "0000000a"})
        self.assertEqual((draws[2]["ib"], draws[2]["vs"], draws[2]["ps"]),
                         (("0000000b", "42", "0"), "3000000000000003", "3000000000000003"))

    def test_copies_name_both_resources(self):
        cap = Capture(skinning_doc())
        lines = contents.xxmi_event_lines(cap, {"type": "CopyResource", "dst": 3, "src": 0, "a": [0, 0, 0, 0]})
        self.assertEqual(lines, ["CopyResource(pDstResource:0x0000000000000004, pSrcResource:0x0000000000000001)",
                                 "       Src: resource=0x0000000000000001 hash=00000001",
                                 "       Dst: resource=0x0000000000000004 hash=0000000a"])

    def test_dump_tool_writes_the_xxmi_style(self):
        build = ROOT / "sandbox" / "build"
        tmp = tempfile.TemporaryDirectory(prefix="zzmi-dump-", dir=build)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "Mods").mkdir()
        probe = FakeProbe()
        session = tools.Session(root, root / "out", root / "probe.dll")
        session.client = probe
        with patch.object(tools, "Session", return_value=session), \
                patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe")):
            server = tools.build_server(root, root / "out", root / "probe.dll")
            self.assertFalse(call(server, "efmi_capture_frame", contents="inputs")[1])
            text, error = call(server, "efmi_dump_frame", log="xxmi")
            self.assertFalse(error, text)
            log = Path(json.loads(text)["log"]).read_text(encoding="utf-8")
            self.assertIn("000000 IASetVertexBuffers(StartSlot:0, NumBuffers:1", log)
            self.assertIn("000002 CSSetUnorderedAccessViews(StartSlot:0, NumUAVs:1", log)
            self.assertNotIn(" bind ", log)
            readable = Path(json.loads(call(server, "efmi_dump_frame", folder=str(root / "readable"))[0])["log"])
            self.assertNotIn("IASetVertexBuffers", readable.read_text(encoding="utf-8"))
            self.assertIn("log must be one of", call(server, "efmi_dump_frame", log="other")[0])


if __name__ == "__main__":
    unittest.main()
