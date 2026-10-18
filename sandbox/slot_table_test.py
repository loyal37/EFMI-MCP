"""The slot table built from a slot cache folder (server, no game and no probe needed).

Synthetic records stand for what the probe writes; the dataflow analysis of each pixel shader is
given here instead of being computed from bytecode. Checked: only slots a shader reads count; the
three slots come from the dataflow, settled by position or formats where it names several, from the
formats where it names none, from the same shader on other meshes, and by order only as an
uncertain last resort; global textures are never one of them; the main pass is the lit one, its
variants are folded into the row and other lit passes that read the textures elsewhere are listed;
the mods are compared and never change a slot; meshes are grouped by mod, by frame or not at all;
the Markdown, the query of the MCP tool, the command line and the cache that rebuilds when the
records change.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

from efmi_mcp import slot_table  # noqa: E402
from efmi_mcp.shader_semantics import analyze  # noqa: E402
from shader_semantics_test import ASSEMBLY  # noqa: E402


def tex(slot: int, h: str, fmt: str = "BC7_UNORM_SRGB", w: int = 512) -> dict:
    return {"slot": slot, "kind": "tex2d", "hash": h, "origin": "game", "w": w, "h": w, "mips": 10, "array": 1,
            "format": fmt, "view_format": fmt, "view_dim": 4}


def rec(ib: str, count: int, ps: str, srv: list[dict], frame: int = 30, origin: str = "game", rt=None) -> dict:
    return {"fmt": 2, "frame": frame, "unix_ms": 0, "origin": origin, "draw_type": 2, "ib": ib, "ib_state": "ok",
            "ib_resource": "heap", "ib_format": "R16_UINT", "ib_usage": "default", "ib_offset": 0, "first_index": 0,
            "index_count": count, "base_vertex": 0, "instances": 1, "vs": "v" * 16, "ps": ps,
            "layout": ["POSITION0", "BLENDINDICES0"], "skinned": True, "vs_srv": [], "ps_srv": srv,
            "rt": ["R11G11B10_FLOAT", "R10G10B10A2_UNORM"] if rt is None else rt, "ds": "D32_FLOAT_S8X24_UINT",
            "key": f"{origin}-{ib}-{count}-{ps}-{len(srv)}"}


def res(roles=(), shadow=False) -> dict:
    samples = [{"comparison": True, "dimension": "texture2d"}] if shadow else [{"comparison": False, "dimension": "texture2d"}]
    return {"role_candidates": list(roles), "samples": samples}


PS_LIT = "a" * 16       # lit: shadow map at t5; diffuse t16, light map t17, normal map t18; global ramp t12
PS_LIT_COPY = "e" * 16  # a patched copy of the same shader: same slots
PS_LIT_2 = "f" * 16     # another lit pass that reads the same textures two slots higher
PS_GB = "b" * 16        # G-buffer: diffuse t0, normal map t1, more slots than the lit pass but no shadows
PS_TWO = "c" * 16       # the dataflow calls both t20 and t24 diffuse; light map t21, normal map t22
PS_TIE = "d" * 16       # the dataflow calls t30 and t33 diffuse; t31 normal map, t32 light map: a tie
PS_FMT = "1" * 16       # no dataflow at all: formats decide
PS_GAP = "2" * 16       # no dataflow, formats apart: order only
PS_SEQ = "4" * 16       # the dataflow finds light map t71 and normal map t72 only: the sRGB t70 before them
PS_SHARED = "3" * 16    # formats clear on one mesh, open on the other: the same shader decides there
LIT = {5: res(shadow=True), 12: res(), 16: res(["diffuse"]), 17: res(["lightmap"]), 18: res(["normal"])}
READS = {
    PS_LIT: LIT, PS_LIT_COPY: LIT,
    PS_LIT_2: {5: res(shadow=True), 18: res(["diffuse"]), 19: res(["lightmap"]), 20: res(["normal"])},
    PS_GB: {0: res(["diffuse"]), 1: res(["normal"]), 2: res(), 3: res(), 4: res(), 6: res()},
    PS_TWO: {20: res(["diffuse"]), 21: res(["lightmap"]), 22: res(["normal"]), 24: res(["diffuse"])},
    PS_TIE: {30: res(["diffuse"]), 31: res(["normal"]), 32: res(["lightmap"]), 33: res(["diffuse"])},
    PS_FMT: {40: res(), 41: res(), 42: res()},
    PS_GAP: {50: res(), 53: res(), 57: res()},
    PS_SHARED: {60: res(), 61: res(), 62: res(), 63: res()},
    PS_SEQ: {70: res(), 71: res(["lightmap"]), 72: res(["normal"])},
}
READS = {ps: {"resources": resources} for ps, resources in READS.items()}   # what shader_semantics.analyze returns


def material(prefix: str, base: int, normal_fmt: str = "BC5_UNORM") -> list[dict]:
    return [tex(base, f"{prefix}d"), tex(base + 1, f"{prefix}l", "BC7_UNORM"), tex(base + 2, f"{prefix}n", normal_fmt)]


class SlotTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.saved = (slot_table.GLOBAL_COOCCURRENCE, slot_table.SCENE_MESHES)
        slot_table.GLOBAL_COOCCURRENCE = 6
        slot_table.SCENE_MESHES = 6
        ramp = tex(12, "ramp", w=1024)
        lines = []

        def character(prefix: str, frame: int, n: int) -> None:
            for i in range(n):
                ib, count = f"{prefix}{i:06x}", 300 + i
                lit = [tex(5, "shadow", "R32_FLOAT"), ramp] + material(prefix, 16) + [tex(25, "left", "BC7_UNORM")]
                lines.append(rec(ib, count, PS_LIT, lit, frame))
                lines.append(rec(ib, count, PS_GB, [tex(0, f"{prefix}d"), tex(1, f"{prefix}n", "BC5_UNORM"),
                                                    tex(2, "g2", "R8G8B8A8_UNORM"), tex(3, "g3", "R8G8B8A8_UNORM"),
                                                    tex(4, "g4", "R8G8B8A8_UNORM"), tex(6, "g6", "R8G8B8A8_UNORM")],
                                 frame, rt=["a", "b", "c", "d", "e"]))

        character("aa", 30, 3)     # aa000000/1 named by Mod A, aa000002 probably the same character
        character("bb", 60, 3)     # no mod: a probable group
        character("dd", 120, 8)    # many at once: a scene (the ramp now meets many materials: global)
        # aa000000 is also drawn by the patched copy and by a lit pass that reads the textures two slots higher
        lines.append(rec("aa000000", 300, PS_LIT_COPY, [tex(5, "shadow", "R32_FLOAT"), ramp] + material("aa", 16), 31))
        lines.append(rec("aa000000", 300, PS_LIT_2, [tex(5, "shadow", "R32_FLOAT")] + material("aa", 18), 31))
        lines.append(rec("cc000000", 900, PS_TWO, material("cc", 20) + [tex(24, "ccx", w=256)], 300))
        lines.append(rec("cc000001", 901, PS_TIE, [tex(30, "ctd"), tex(31, "ctn", "BC5_UNORM"), tex(32, "ctl", "BC7_UNORM"),
                                                   tex(33, "ctx", w=256)], 330))
        lines.append(rec("cc000002", 902, PS_FMT, material("cf", 40), 360))
        lines.append(rec("cc000003", 903, PS_GAP, [tex(50, "cgd"), tex(53, "cgl", "BC7_UNORM"), tex(57, "cgn", "BC5_UNORM")], 390))
        lines.append(rec("cc000004", 904, PS_SHARED, material("cs", 60), 420))
        lines.append(rec("cc000005", 905, PS_SHARED, [tex(60, "cs2d"), tex(61, "cs2l", "BC7_UNORM"), tex(62, "cs2n", "BC7_UNORM"),
                                                      tex(63, "cs2x", "BC7_UNORM")], 450))
        lines.append(rec("ee000000", 50, PS_LIT, material("ee", 16), 200, origin="xxmi"))
        lines.append(rec("cc000006", 906, PS_SEQ, material("cq", 70), 470))
        # a deferred mesh drawn only by the G-buffer pass, and one seen only in a depth pass
        lines.append(rec("gb000000", 60, PS_GB, [tex(0, "gbd"), tex(1, "gbn", "BC5_UNORM")], 480, rt=["a", "b", "c", "d", "e"]))
        lines.append(rec("dp000000", 61, PS_FMT, material("dp", 40), 510, rt=[]))
        text = '{"frame":1,"origin":"game","ib":"ff000000","index_count":3,"key":"old"}\n'
        text += "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in lines)
        text += '{"fmt":2,"frame":999,"origin":"game","ib":"9900'      # the line the probe is writing
        (self.folder / "draws.jsonl").write_text(text, encoding="utf-8")
        (self.folder / "version.json").write_text(json.dumps({"key": "1.0.0+abc", "label": "1.0.0"}), encoding="utf-8")
        self.names = {("aa000000", 300): [("Mod A 1.0", "TextureOverride_EntryPoint_Component0")],
                      ("aa000001", 301): [("Mod A 1.0", "TextureOverride_EntryPoint_Component1")]}
        # Mod A: right for aa000000, a wrong normal map slot for aa000001.
        self.declared = {("aa000000", 300): {"diffuse": {16}, "lightmap": {17}, "normal": {18}},
                         ("aa000001", 301): {"diffuse": {16}, "normal": {19}}}
        self.table = slot_table.build(self.folder, self.names, READS.get, self.declared)

    def tearDown(self) -> None:
        slot_table.GLOBAL_COOCCURRENCE, slot_table.SCENE_MESHES = self.saved
        self.tmp.cleanup()

    def row(self, ib: str) -> dict:
        return next(r for _, r in slot_table.all_rows(self.table) if r["ib"] == ib)

    def slots(self, ib: str) -> tuple[dict, dict]:
        m = self.row(ib)["main"]
        return ({r: (v or {}).get("slot") for r, v in m["roles"].items()},
                {r: v["basis"] for r, v in m["roles"].items() if v})

    def test_input(self) -> None:
        text = json.dumps(self.table)
        for ib in ("ff000000", "ee000000", "9900"):
            self.assertNotIn(ib, text)
        self.assertEqual(self.table["version"], "1.0.0+abc")
        self.assertEqual(self.table["schema"], 4)

    def test_only_read_slots_and_classes(self) -> None:
        p = next(q for q in self.row("aa000001")["passes"] if q["ps"] == PS_LIT)
        slots = {s["slot"]: s for s in p["slots"]}
        self.assertNotIn(25, slots, "bound but not read: left by an earlier draw")
        self.assertEqual(slots[12]["class"], "global")
        self.assertEqual(slots[5]["class"], "engine")
        self.assertEqual({slots[s]["class"] for s in (16, 17, 18)}, {"material"})
        self.assertTrue(p["lit"])

    def test_dataflow(self) -> None:
        self.assertEqual(self.slots("aa000001"), ({"diffuse": 16, "lightmap": 17, "normal": 18},
                                                  {"diffuse": "dataflow", "lightmap": "dataflow", "normal": "dataflow"}))
        m = self.row("aa000001")["main"]
        self.assertEqual(m["roles"]["normal"]["format"], "BC5_UNORM")

    def test_several_candidates(self) -> None:
        self.assertEqual(self.slots("cc000000")[0], {"diffuse": 20, "lightmap": 21, "normal": 22})
        self.assertEqual(self.slots("cc000000")[1]["diffuse"], "dataflow+position")
        self.assertEqual(self.slots("cc000001"), ({"diffuse": 30, "lightmap": 32, "normal": 31},
                                                  {"diffuse": "dataflow+format", "lightmap": "dataflow", "normal": "dataflow"}))

    def test_sequence_fills_a_gap(self) -> None:
        self.assertEqual(self.slots("cc000006"), ({"diffuse": 70, "lightmap": 71, "normal": 72},
                                                  {"diffuse": "sequence", "lightmap": "dataflow", "normal": "dataflow"}))
        self.assertIn("sequence (LoyalTools)", slot_table.to_markdown(self.table))

    def test_formats_order_and_shader(self) -> None:
        self.assertEqual(self.slots("cc000002"), ({"diffuse": 40, "lightmap": 41, "normal": 42},
                                                  {"diffuse": "format", "lightmap": "format", "normal": "format"}))
        self.assertEqual(set(self.slots("cc000003")[1].values()), {"order"})
        self.assertEqual(self.slots("cc000004")[1]["normal"], "format")
        self.assertEqual(self.slots("cc000005"), ({"diffuse": 60, "lightmap": 61, "normal": 62},
                                                  {"diffuse": "shader", "lightmap": "shader", "normal": "shader"}))

    def test_main_pass_variants_and_other_lit_passes(self) -> None:
        m = self.row("aa000000")["main"]
        self.assertEqual(m["ps"], PS_LIT, "the lit pass, not the G-buffer pass with more slots; of two lit "
                                          "passes alike, the one whose slots come first")
        self.assertEqual(m["also_ps"], [PS_LIT_COPY])
        self.assertEqual([o["ps"] for o in m["other_passes"]], [PS_LIT_2])
        self.assertEqual({r: v["slot"] for r, v in m["other_passes"][0]["roles"].items()},
                         {"diffuse": 18, "lightmap": 19, "normal": 20})

    def test_depth_and_gbuffer_passes_are_never_main(self) -> None:
        self.assertIsNone(self.row("gb000000")["main"])
        self.assertTrue(self.row("gb000000")["why_none"].startswith("G-buffer pass only"))
        self.assertIsNone(self.row("dp000000")["main"])
        self.assertEqual(self.row("dp000000")["why_none"], "depth passes only")
        self.assertFalse(self.row("cc000002")["main"]["lit"])
        md = slot_table.to_markdown(self.table)
        main_part, unlit_part = md.split("## Forward passes that do not sample shadows")
        self.assertNotIn("`cc000002`", main_part, "not lit: not in the main sections")
        self.assertIn(f"| `cc000002` | 902 | unassigned | `{PS_FMT}` | t40 BC7_UNORM_SRGB 512x512 |", unlit_part)
        self.assertIn("`aa000000`", main_part)
        self.assertIn("| `gb000000` | 60 | unassigned | G-buffer pass only", md)
        self.assertNotIn(f"`{PS_GB}`", md)

    def test_mod_check_never_changes_a_slot(self) -> None:
        check = self.row("aa000000")["mod_check"]
        self.assertTrue(all(c["agrees"] for c in check.values()))
        self.assertEqual(check["diffuse"]["wrong_for_other_passes"], [PS_LIT_2])
        wrong = self.row("aa000001")["mod_check"]
        self.assertFalse(wrong["normal"]["agrees"])
        self.assertEqual(wrong["normal"]["table"], 18)
        bare = slot_table.build(self.folder, reader=READS.get)
        strip = lambda t: [(r["ib"], r["main"]) for _, r in slot_table.all_rows(t)]  # noqa: E731
        by_ib = dict(strip(bare))
        for ib, main in strip(self.table):
            self.assertEqual(main, by_ib[ib], ib)
        self.assertEqual(self.table["counts"]["mod_checks"], {"agree": 4, "differ": 1, "wrong_for_another_lit_pass": 3})

    def test_grouping(self) -> None:
        t = self.table
        self.assertEqual([c["name"] for c in t["characters"]], ["Mod A 1.0"])
        self.assertEqual([r["ib"] for r in t["characters"][0]["probably_also"]], ["aa000002"])
        self.assertEqual([(g["first_frame"], len(g["meshes"])) for g in t["probable_groups"]], [(60, 3)])
        self.assertEqual([(g["first_frame"], len(g["meshes"])) for g in t["scenes"]], [(120, 8)])
        self.assertEqual(len(t["unassigned"]), 9)

    def test_markdown(self) -> None:
        md = slot_table.to_markdown(self.table)
        self.assertIn("| IB | indices | pixel shader | diffuse | light map | normal map | known from | mod check |", md)
        self.assertIn(f"| `aa000000` | 300 | `{PS_LIT}` (+1) | t16 BC7_UNORM_SRGB 512x512 | t17 BC7_UNORM 512x512 | "
                      "t18 BC5_UNORM 512x512 | dataflow | agrees; wrong for the other lit pass |", md)
        self.assertIn(f"| | | `{PS_LIT_2}` (other lit pass) | t18 BC7_UNORM_SRGB 512x512 |", md)
        self.assertIn("differs: normal mod t19", md)
        self.assertIn("order (uncertain)", md)
        self.assertIn("### Frame 60 (3 meshes)", md)
        self.assertNotIn("t12 ", md, "a global texture is never one of the three")
        self.assertNotIn("t25", md)

    def test_query(self) -> None:
        q = slot_table.query(self.table, character="mod a", limit=1)
        self.assertEqual(q["matching"], 3)
        self.assertEqual(q["next_offset"], 1)
        self.assertNotIn("passes", q["rows"][0])
        self.assertEqual(q["rows"][0]["group"], "Mod A 1.0")
        q = slot_table.query(self.table, ib_hash="AA000000", include_passes=True, reader=READS.get, include_evidence=True)
        self.assertEqual(q["matching"], 1)
        self.assertIn("passes", q["rows"][0])
        self.assertEqual(q["rows"][0]["evidence"]["normal"]["role_candidates"], ["normal"])
        with self.assertRaises(ValueError):
            slot_table.query(self.table, ib_hash="xyz")
        with self.assertRaises(ValueError):
            slot_table.query(self.table, limit=0)

    def test_real_analysis_end_to_end(self) -> None:
        # the assembly of shader_semantics_test: t7 tinted RGB, t2 a reconstructed normal map, t0 feeding
        # the lighting, t99 a comparison-sampled shadow map
        folder = self.folder / "real"
        folder.mkdir()
        views = [tex(7, "rd"), tex(2, "rn", "BC5_UNORM"), tex(0, "rl", "BC7_UNORM"), tex(99, "shadow", "R32_FLOAT")]
        (folder / "draws.jsonl").write_text(json.dumps(rec("ab000001", 120, "9" * 16, views), separators=(",", ":")) + "\n",
                                            encoding="utf-8")
        analysis = analyze(ASSEMBLY)
        table = slot_table.build(folder, reader=lambda ps: analysis)
        m = table["unassigned"][0]["main"]
        self.assertTrue(m["lit"])
        self.assertEqual({r: (v["slot"], v["basis"]) for r, v in m["roles"].items()},
                         {"diffuse": (7, "dataflow"), "lightmap": (0, "dataflow"), "normal": (2, "dataflow")})

    def test_mcp_tool(self) -> None:
        from types import SimpleNamespace
        from efmi_mcp.mcp_stdio import Server
        server = Server("test", "0")
        slot_table.register(server, SimpleNamespace(efmi_root=None))
        self.assertTrue(server.tools["efmi_get_slot_table"].read_only)
        saved = slot_table.shader_reader
        slot_table.shader_reader = lambda folder: READS.get
        try:
            def call(**arguments):
                return server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "efmi_get_slot_table", "arguments": arguments}})["result"]
            reply = call(folder=str(self.folder), ib_hash="aa000001")
            self.assertFalse(reply["isError"])
            body = json.loads(reply["content"][0]["text"])
            self.assertEqual((body["schema"], body["matching"]), (4, 1))
            self.assertEqual(body["rows"][0]["main"]["roles"]["normal"]["slot"], 18)
            self.assertTrue(call(folder=str(self.folder), ib_hash="nothex")["isError"])
        finally:
            slot_table.shader_reader = saved
        self.assertFalse((self.folder / "slot-table.json").exists(), "a query writes no files")

    def test_command_line(self) -> None:
        saved = slot_table.shader_reader
        slot_table.shader_reader = lambda folder: READS.get
        try:
            self.assertEqual(slot_table.main([str(self.folder), "--efmi-root", ""]), 0)
        finally:
            slot_table.shader_reader = saved
        table = json.loads((self.folder / "slot-table.json").read_text(encoding="utf-8"))
        self.assertEqual(table["characters"], [])
        self.assertTrue((self.folder / "slot-table.md").read_text(encoding="utf-8").startswith("# Slot table 1.0.0+abc"))

    def test_cache_rebuilds_when_records_change(self) -> None:
        saved = slot_table.shader_reader
        slot_table.shader_reader = lambda folder: READS.get
        try:
            cache = slot_table.TableCache()
            first = cache.get(self.folder, None)
            self.assertIs(cache.get(self.folder, None), first)
            path = self.folder / "draws.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + json.dumps(rec("ab000000", 77, PS_FMT, material("ab", 40), 500), separators=(",", ":")) + "\n")
            os.utime(path, (time.time() + 5, time.time() + 5))
            second = cache.get(self.folder, None)
            self.assertIsNot(second, first)
            self.assertIn("ab000000", [r["ib"] for _, r in slot_table.all_rows(second)])
        finally:
            slot_table.shader_reader = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
