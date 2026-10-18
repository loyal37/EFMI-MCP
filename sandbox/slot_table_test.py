"""The slot table built from a slot cache folder (server, no game and no probe needed).

Synthetic records stand for what the probe writes; which slots a pixel shader reads is given here
instead of being disassembled. Checked: only slots the shader reads are listed, records of the same
pass are merged and a slot whose texture differs between them is marked, material and global
textures are told apart, meshes go to the mod that names them, to a probable group or to a scene by
the frame they were first seen in, records of the old format and of XXMI's draws are left out, and
the files the command line writes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

from efmi_mcp import slot_table  # noqa: E402


def tex(slot: int, h: str, fmt: str = "BC7_UNORM_SRGB", w: int = 512) -> dict:
    return {"slot": slot, "kind": "tex2d", "hash": h, "origin": "game", "w": w, "h": w, "mips": 10, "array": 1,
            "format": fmt, "view_format": fmt, "view_dim": 4}


def rec(ib: str, count: int, ps: str, srv: list[dict], frame: int = 30, origin: str = "game", rt=None) -> dict:
    return {"fmt": 2, "frame": frame, "unix_ms": 0, "origin": origin, "draw_type": 2, "ib": ib, "ib_state": "ok",
            "ib_resource": "heap", "ib_format": "R16_UINT", "ib_offset": 0, "first_index": 0, "index_count": count,
            "base_vertex": 0, "instances": 1, "vs": "v" * 16, "ps": ps, "layout": ["POSITION0", "BLENDINDICES0"],
            "skinned": True, "vs_srv": [], "ps_srv": srv, "rt": rt if rt is not None else ["R11G11B10_FLOAT", "R10G10B10A2_UNORM"],
            "ds": "D32_FLOAT_S8X24_UINT", "key": f"{origin}-{ib}-{count}-{ps}-{len(srv)}"}


PS_MAIN = "a" * 16
PS_GB = "b" * 16
PS_ORDER = "c" * 16
PS_SHARED = "d" * 16
READS = {PS_MAIN: {12: ["sample"], 16: ["sample"], 17: ["sample"], 18: ["sample"], 19: ["sample"]},
         PS_GB: {0: ["sample"], 1: ["sample"]},
         PS_ORDER: {20: ["sample"], 21: ["sample"], 22: ["sample"]},
         PS_SHARED: {s: ["sample"] for s in range(12, 18)}}
# What the mod binds for its meshes: right for the main shader, and one binding that does not fit
# the game's texture there (a diffuse resource on a linear slot), which must not count.
ROLES = {("aa000000", 300): {"diffuse": {16}, "lightmap": {17}, "normal": {18}},
         ("aa000001", 301): {"diffuse": {17}}}


class SlotTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=HERE.parent / "captures")
        self.folder = Path(self.tmp.name)
        self.old = slot_table.SCENE_MESHES
        slot_table.SCENE_MESHES = 6
        ramp = tex(12, "ramp0000", "BC6H_UF16", w=1024)
        lines = []

        def char(prefix: str, frame: int, n: int) -> None:
            # a character's meshes share their material textures; the ramp is everyone's
            for i in range(n):
                srv = [ramp, tex(16, f"{prefix}d"), tex(17, f"{prefix}l", "BC7_UNORM"), tex(18, f"{prefix}n", "BC5_UNORM"),
                       tex(25, "leftover", "BC7_UNORM")]           # bound, but the shader does not read it
                lines.append(rec(f"{prefix}{i:06x}", 300 + i, PS_MAIN, srv, frame))
                lines.append(rec(f"{prefix}{i:06x}", 300 + i, PS_GB, [tex(0, f"{prefix}d"), tex(1, f"{prefix}n", "BC5_UNORM")],
                                 frame, rt=["a", "b", "c", "d", "e"]))

        char("aa", 30, 3)      # aa000000..aa000002, aa000000 and aa000001 named by a mod; aa000002 probable
        char("bb", 60, 3)      # no mod: a probable group
        char("cc", 90, 1)      # one mesh alone: unassigned
        char("dd", 120, 8)     # many at once: a scene
        # the same pass recorded twice, with another texture format in t19 the second time
        lines.append(rec("aa000000", 300, PS_MAIN, [tex(19, "aax", "BC7_UNORM_SRGB")], 30))
        lines.append(rec("aa000000", 300, PS_MAIN, [tex(19, "aay", "BC7_UNORM")], 31))
        lines.append(rec("ee000000", 50, PS_MAIN, [tex(16, "eed")], 200, origin="xxmi"))
        # a shader whose formats leave normal map and light map open (both linear BC7)
        lines.append(rec("gg000000", 900, PS_ORDER, [tex(20, "ggd"), tex(21, "ggl", "BC7_UNORM"), tex(22, "ggn", "BC7_UNORM")], 300))
        # a shader clear on one mesh and unclear on another, which has a stray sRGB texture early on
        lines.append(rec("hh000000", 700, PS_SHARED, [tex(15, "hhd"), tex(16, "hhl", "BC7_UNORM"), tex(17, "hhn", "BC5_UNORM")], 330))
        lines.append(rec("hh000001", 701, PS_SHARED, [tex(12, "hhx"), tex(13, "hhx2", "BC7_UNORM"), tex(14, "hhx3", "BC5_UNORM"), tex(15, "hhd2"), tex(16, "hhl2", "BC7_UNORM"),
                                                       tex(17, "hhn2", "BC5_UNORM")], 360))
        text = "\n".join(json.dumps(r, separators=(",", ":")) for r in lines)
        text = '{"frame":1,"origin":"game","ib":"ff000000","index_count":3,"key":"old"}\n' + text + "\n"
        (self.folder / "draws.jsonl").write_text(text, encoding="utf-8")
        (self.folder / "version.json").write_text(json.dumps({"key": "1.0.0+abc", "label": "1.0.0"}), encoding="utf-8")
        self.names = {("aa000000", 300): [("Mod A 1.0", "TextureOverride_EntryPoint_Component0")],
                      ("aa000001", 301): [("Mod A 1.0", "TextureOverride_EntryPoint_Component1")]}
        self.table = slot_table.build(self.folder, self.names, reads=READS.get, roles=ROLES)

    def tearDown(self) -> None:
        slot_table.SCENE_MESHES = self.old
        self.tmp.cleanup()

    def mesh(self, ib: str) -> dict:
        t = self.table
        rows = [m for c in t["characters"] for m in c["meshes"] + c["probably_also"]]
        rows += [m for g in t["probable_groups"] + t["scenes"] for m in g["meshes"]] + t["unassigned"]
        return next(m for m in rows if m["ib"] == ib)

    def test_old_records_and_xxmi_left_out(self) -> None:
        ibs = {m["ib"] for c in self.table["characters"] for m in c["meshes"]}
        self.assertNotIn("ff000000", json.dumps(self.table))
        self.assertNotIn("ee000000", json.dumps(self.table))
        self.assertEqual(self.table["version"], "1.0.0+abc")
        self.assertEqual(ibs, {"aa000000", "aa000001"})

    def test_only_read_slots_and_classes(self) -> None:
        main = next(p for p in self.mesh("aa000001")["passes"] if p["ps"] == PS_MAIN)
        slots = {s["slot"]: s for s in main["slots"]}
        self.assertEqual(sorted(slots), [12, 16, 17, 18])
        self.assertNotIn(25, slots)
        self.assertEqual(slots[12]["class"], "global")
        self.assertEqual({slots[s]["class"] for s in (16, 17, 18)}, {"material"})
        self.assertEqual(slots[18]["format"], "BC5_UNORM")
        self.assertEqual(main["read_but_unbound"], [19])
        self.assertEqual(self.mesh("aa000001")["passes"][0]["ps"], PS_MAIN, "the pass with most material slots first")

    def test_records_of_one_pass_merged(self) -> None:
        main = next(p for p in self.mesh("aa000000")["passes"] if p["ps"] == PS_MAIN)
        self.assertIn(19, [s["slot"] for s in main["slots"]])
        self.assertEqual(main["differs_between_records"], [19])

    def test_grouping(self) -> None:
        t = self.table
        a = t["characters"][0]
        self.assertEqual(a["name"], "Mod A 1.0")
        self.assertEqual([m["ib"] for m in a["probably_also"]], ["aa000002"])
        self.assertEqual(a["meshes"][0]["mod_sections"], ["TextureOverride_EntryPoint_Component0"])
        self.assertEqual([(g["first_frame"], len(g["meshes"])) for g in t["probable_groups"]], [(60, 3)])
        self.assertEqual([(g["first_frame"], len(g["meshes"])) for g in t["scenes"]], [(120, 8)])
        self.assertEqual([m["ib"] for m in t["unassigned"]], ["cc000000", "gg000000", "hh000000", "hh000001"])

    def roles(self, ib: str) -> tuple[dict, str]:
        p = slot_table.main_pass(self.mesh(ib))
        return {r: (v or {}).get("slot") for r, v in p["roles"].items()}, p["roles_basis"]

    def test_roles_from_the_mods_names(self) -> None:
        # the mod's bindings for its own mesh stand for every mesh drawn with that shader
        for ib in ("aa000000", "bb000001", "dd000003"):
            self.assertEqual(self.roles(ib), ({"diffuse": 16, "normal": 18, "lightmap": 17}, "mod"), ib)
        p = slot_table.main_pass(self.mesh("aa000000"))
        self.assertEqual(p["roles"]["normal"]["format"], "BC5_UNORM")
        self.assertNotIn("roles_conflict", p, "the binding that does not fit the game's texture does not vote")

    def test_roles_from_formats(self) -> None:
        table = slot_table.build(self.folder, self.names, reads=READS.get)       # no mod evidence
        mesh = next(m for c in table["characters"] for m in c["meshes"] if m["ib"] == "aa000001")
        p = slot_table.main_pass(mesh)
        self.assertEqual(p["ps"], PS_MAIN)
        self.assertEqual({r: v["slot"] for r, v in p["roles"].items()}, {"diffuse": 16, "normal": 18, "lightmap": 17})
        self.assertEqual(p["roles_basis"], "format")
        gb = next(q for q in mesh["passes"] if q["ps"] == PS_GB)
        self.assertEqual(({r: (v or {}).get("slot") for r, v in gb["roles"].items()}, gb["roles_basis"]),
                         ({"diffuse": None, "normal": None, "lightmap": None}, "none"))

    def test_roles_by_order_are_marked(self) -> None:
        self.assertEqual(self.roles("gg000000"), ({"diffuse": 20, "normal": 22, "lightmap": 21}, "order"))

    def test_roles_from_the_same_shader(self) -> None:
        self.assertEqual(self.roles("hh000000"), ({"diffuse": 15, "normal": 17, "lightmap": 16}, "format"))
        self.assertEqual(self.roles("hh000001"), ({"diffuse": 15, "normal": 17, "lightmap": 16}, "shader"))

    def test_markdown(self) -> None:
        md = slot_table.to_markdown(self.table)
        self.assertIn("### Mod A 1.0", md)
        self.assertIn("| IB | indices | pixel shader | diffuse | light map | normal map | known from |", md)
        self.assertIn(f"| `aa000000` | 300 | `{PS_MAIN}` | t16 BC7_UNORM_SRGB 512x512 | t17 BC7_UNORM 512x512 | "
                      "t18 BC5_UNORM 512x512 | mod names |", md)
        self.assertIn("uncertain (slot order or slots apart)", md)
        self.assertIn("formats of the same shader on other meshes", md)
        self.assertIn("Probably the same character too", md)
        self.assertIn("### Frame 60 (3 meshes)", md)
        self.assertNotIn("t25", md)
        self.assertNotIn("t12", md, "global textures are not one of the three")

    def test_command_line_writes_both_files(self) -> None:
        old_reader = slot_table.shader_reader
        slot_table.shader_reader = lambda folder: READS.get
        try:
            self.assertEqual(slot_table.main([str(self.folder), "--efmi-root", ""]), 0)
        finally:
            slot_table.shader_reader = old_reader
        table = json.loads((self.folder / "slot-table.json").read_text(encoding="utf-8"))
        self.assertEqual(table["characters"], [])          # no mods read: nothing is named
        self.assertTrue((self.folder / "slot-table.md").read_text(encoding="utf-8").startswith("# Slot table 1.0.0+abc"))

    def test_missing_shader_does_not_guess_roles(self) -> None:
        table = slot_table.build(self.folder, reads=lambda ps: None, roles=ROLES)
        for row in slot_table.query(table, limit=200)["rows"]:
            self.assertTrue(all(not any(p["roles"].values()) for p in row["passes"]))

    def test_texture_hashes_do_not_determine_roles(self) -> None:
        path = self.folder / "draws.jsonl"
        records = slot_table.load_records(self.folder)
        before = slot_table.query(slot_table.build(self.folder, reads=READS.get), limit=200)
        for r in records:
            for v in r["ps_srv"]:
                v.pop("hash", None)
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        after = slot_table.query(slot_table.build(self.folder, reads=READS.get), limit=200)
        self.assertEqual([[p["roles"] for p in m["passes"]] for m in before["rows"]],
                         [[p["roles"] for p in m["passes"]] for m in after["rows"]])

    def test_queries_page_without_files(self) -> None:
        before = sorted(self.folder.rglob("*"))
        result = slot_table.query(self.table, character="mod a", limit=1)
        self.assertEqual(result["matching_meshes"], 2)
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(len(slot_table.query(self.table, ib_hash="AA000000")["rows"]), 1)
        self.assertEqual(before, sorted(self.folder.rglob("*")))
        with self.assertRaises(ValueError):
            slot_table.query(self.table, limit=201)

    def test_cache_reuse_and_append_invalidation(self) -> None:
        from unittest.mock import patch
        cache = slot_table.TableCache()
        with patch.object(slot_table, "build", return_value=self.table) as build:
            cache.get(self.folder)
            cache.get(self.folder)
            self.assertEqual(build.call_count, 1)
            with (self.folder / "draws.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n")
            cache.get(self.folder)
            cache.get(self.folder, refresh=True)
            self.assertEqual(build.call_count, 3)

    def test_conflicting_formats_withhold_role(self) -> None:
        records = [rec("aa000000", 300, PS_MAIN, [tex(16, "a")]),
                   rec("aa000000", 300, PS_MAIN, [tex(16, "b", "BC7_UNORM")])]
        passes = {( "aa000000", 300): slot_table._mesh_passes(records, READS.get)}
        slot_table._assign_roles(passes, ROLES)
        p = passes[("aa000000", 300)][0]
        self.assertEqual(p["differs_between_records"], [16])
        self.assertIsNone(p["roles"]["diffuse"])

    def test_mcp_registration_and_query(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from efmi_mcp.mcp_stdio import Server
        server = Server("test", "0")
        slot_table.register(server, SimpleNamespace(efmi_root=None))
        self.assertTrue(server.tools["efmi_get_slot_table"].read_only)
        with patch.object(slot_table, "shader_reader", return_value=READS.get):
            result = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "efmi_get_slot_table", "arguments": {"folder": str(self.folder), "ib_hash": "aa000000"}}})
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["matching_meshes"], 1)

    def test_partial_append_and_shader_arrival(self) -> None:
        with (self.folder / "draws.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"fmt":2,')
        self.assertGreater(len(slot_table.load_records(self.folder)), 0)
        from unittest.mock import patch
        cache = slot_table.TableCache()
        with patch.object(slot_table, "build", return_value=self.table) as build:
            cache.get(self.folder)
            (self.folder / "shaders").mkdir()
            (self.folder / "shaders" / f"{PS_MAIN}-ps.bin").write_bytes(b"new")
            cache.get(self.folder)
            self.assertEqual(build.call_count, 2)

    def test_mod_evidence_follows_case_insensitive_commands(self) -> None:
        mod = self.folder / "Mods" / "RoleMod"
        mod.mkdir(parents=True)
        (mod / "test.ini").write_text("[TextureOverrideBody]\nhash = aa000000\nmatch_index_count = 300\n"
                                     "run = CommandListMaterial\n[commandlistmaterial]\n"
                                     "ps-t16 = ResourceDiffuseMap\nps-t18 = ResourceNormalMap\n"
                                     "ps-t17 = ResourceLightMap\nrun = COMMANDLISTMATERIAL\n", encoding="utf-8")
        self.assertEqual(slot_table.mod_roles(self.folder)[("aa000000", 300)], ROLES[("aa000000", 300)])
        self.assertEqual(slot_table.mod_names(self.folder)[("aa000000", 300)][0][0], "RoleMod")

    def test_conflicting_mod_votes_do_not_pick_a_winner(self) -> None:
        roles = {("aa000000", 300): {"diffuse": {16, 19}}}
        records = [rec("aa000000", 300, PS_MAIN, [tex(16, "a"), tex(19, "b")])]
        passes = {("aa000000", 300): slot_table._mesh_passes(records, READS.get)}
        slot_table._assign_roles(passes, roles)
        self.assertIsNone(passes[("aa000000", 300)][0]["roles"]["diffuse"])
        self.assertIn("diffuse", passes[("aa000000", 300)][0]["roles_conflict"])

    def test_auxiliary_only_mesh_has_no_primary(self) -> None:
        records = [rec("aa000000", 300, PS_GB, [tex(0, "d"), tex(1, "n", "BC5_UNORM")])]
        passes = {("aa000000", 300): slot_table._mesh_passes(records, READS.get)}
        slot_table._assign_roles(passes, {})
        row = {"passes": passes[("aa000000", 300)]}
        self.assertIsNone(slot_table.main_pass(row))
        self.assertEqual([s["slot"] for s in row["passes"][0]["slots"]], [0, 1])

    def test_early_srgb_is_not_primary_diffuse(self) -> None:
        slots = [dict(tex(n, str(n), fmt), **{"class": "material"}) for n, fmt in (
            (6, "BC7_UNORM_SRGB"), (16, "BC7_UNORM"), (17, "BC7_UNORM_SRGB"),
            (18, "BC7_UNORM"), (19, "BC7_UNORM"), (21, "BC7_UNORM_SRGB"),
            (22, "BC7_UNORM"), (23, "BC5_UNORM"))]
        roles, basis = slot_table._roles_from_formats(slots)
        self.assertEqual(roles, {"diffuse": 21, "lightmap": 22, "normal": 23})
        self.assertEqual(basis, "format")

    def test_reversed_mod_roles_are_rejected_not_swapped(self) -> None:
        records = [rec("aa000000", 300, PS_MAIN,
                       [tex(16, "d"), tex(17, "n", "BC7_UNORM"), tex(18, "l", "BC7_UNORM")])]
        passes = {("aa000000", 300): slot_table._mesh_passes(records, READS.get)}
        slot_table._assign_roles(passes, {("aa000000", 300): {"diffuse": {16}, "normal": {17}, "lightmap": {18}}})
        p = passes[("aa000000", 300)][0]
        self.assertFalse(any(p["roles"].values()))
        self.assertEqual(p["roles_rejected"]["candidates"], {"diffuse": 16, "normal": 17, "lightmap": 18})

    def test_different_primary_triples_remain_ambiguous(self) -> None:
        a = slot_table.main_pass(self.mesh("aa000001"))
        b = slot_table.main_pass(self.mesh("gg000000"))
        a = {**a, "roles_basis": "format"}
        self.assertIsNone(slot_table.main_pass({"passes": [a, b]}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
