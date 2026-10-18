"""Slot analysis regressions: native provenance first, deliberately misleading mods later."""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
from efmi_mcp import slot_table, slot_table_mods
from efmi_mcp.shader_semantics import analyze
from efmi_mcp.mcp_stdio import Server

PS = "a" * 16
ASSEMBLY = """ps_5_0
dcl_input_ps_siv linear noperspective v0.xyzw, position
dcl_input_ps linear v7.xy
dcl_output o0.xyzw
dcl_resource_texture2d (float,float,float,float) t7
dcl_resource_texture2d (float,float,float,float) t0
dcl_resource_texture2d (float,float,float,float) t2
dcl_resource_texture2d (float,float,float,float) t99
sample r0.xyzw, v7.xyxx, t7.xyzw, s0
mul r0.xyz, r0.xyzx, cb0[0].xyzx
sample r1.xyz, v7.xyxx, t2.xywz, s0
mul r1.x, r1.x, r1.z
mad r2.xy, r1.xyxx, l(2,2,0,0), l(-1,-1,0,0)
dp2 r2.z, r2.xyxx, r2.xyxx
min r2.z, r2.z, l(1)
add r2.z, -r2.z, l(1)
sqrt r2.z, r2.z
sample r3.xyzw, v7.xyxx, t0.xyzw, s0
mul r4.x, r3.x, r2.z
mad r4.y, r3.y, r2.x, cb0[1].x
sample_c r4.z, v0.xyxx, t99.xxxx, s1, v0.z
mul r4.x, r4.x, r4.z
mul r4.x, r4.x, r4.y
mul o0.xyz, r0.xyzx, r4.xxxx
mov o0.w, r0.w
ret
"""


def view(slot, fmt="BC7_UNORM", h="12345678"):
    return dict(slot=slot, kind="tex2d", format=fmt, view_format=fmt, view_dim=4, hash=h, w=512, h=512)


def record(ib="aa000000", ps=PS, origin="game", views=None, rt=None):
    return dict(fmt=2, ib=ib, index_count=300, ib_state="ok", origin=origin, frame=30, vs="b" * 16,
                ps=ps, ps_srv=views if views is not None else [view(s) for s in (0, 2, 7, 99)],
                rt=["R11G11B10_FLOAT"] if rt is None else rt, ds="D32_FLOAT")


class EvidenceTests(unittest.TestCase):
    def test_roles_follow_dataflow_not_register_order(self):
        a = analyze(ASSEMBLY)["resources"]
        self.assertEqual(a[7]["role_candidates"], ["diffuse"])
        self.assertEqual(a[0]["role_candidates"], ["lightmap"])
        self.assertEqual(a[2]["role_candidates"], ["normal"])
        self.assertTrue(a[2]["evidence"]["normal_reconstruction"])

    def test_arbitrary_register_permutation(self):
        original = analyze(ASSEMBLY)["resources"]
        for mapping in ({7: 1, 0: 90, 2: 0, 99: 20}, {7: 17, 0: 22, 2: 11, 99: 3}):
            code = re.sub(r"\bt(\d+)\b", lambda m: f"t{mapping[int(m[1])]}", ASSEMBLY)
            changed = analyze(code)["resources"]
            for source, target in mapping.items():
                self.assertEqual(original[source]["role_candidates"], changed[target]["role_candidates"])

    def test_plain_bc5_style_sample_is_not_enough(self):
        code = ASSEMBLY.replace("mad r2.xy, r1.xyxx, l(2,2,0,0), l(-1,-1,0,0)", "mov r2.xy, r1.xyxx")
        self.assertNotIn("normal", analyze(code)["resources"][2]["role_candidates"])

    def test_component_overwrite_kills_provenance(self):
        code = ASSEMBLY.replace("mul r1.x, r1.x, r1.z", "mov r1.xyz, v0.xyzx")
        self.assertNotIn("normal", analyze(code)["resources"][2]["role_candidates"])

    def test_branch_alternatives_both_survive(self):
        code = ASSEMBLY.replace("sample r1.xyz, v7.xyxx, t2.xywz, s0", "if_nz cb0[2].x\n"
                                "sample r1.xyz, v7.xyxx, t42.xywz, s0\nelse\n"
                                "sample r1.xyz, v7.xyxx, t2.xywz, s0\nendif")
        code = code.replace("dcl_output", "dcl_resource_texture2d (float,float,float,float) t42\ndcl_output")
        a = analyze(code)["resources"]
        self.assertEqual(a[2]["role_candidates"], ["normal"])
        self.assertEqual(a[42]["role_candidates"], ["normal"])

    def test_swizzle_and_mask_track_alpha_only_discard(self):
        code = "ps_5_0\ndcl_input_ps linear v7.xy\ndcl_resource_texture2d (float,float,float,float) t0\n"
        code += "sample r0.x, v7.xyxx, t0.wxyz, s0\nlt r0.x, r0.x, l(0.5)\ndiscard_nz r0.x\nret"
        r = analyze(code)["resources"][0]
        self.assertEqual(r["discard_channels"], ["w"])
        self.assertEqual(r["role_candidates"], [])

    def test_loop_local_evidence_is_not_certified(self):
        code = ASSEMBLY.replace("sample r1.xyz", "loop\nsample r1.xyz").replace("sqrt r2.z, r2.z", "sqrt r2.z, r2.z\nendloop")
        a = analyze(code)
        self.assertTrue(a["limitations"])
        self.assertNotIn("normal", a["resources"][2]["role_candidates"])

    def test_dependent_lookup_is_not_a_mesh_uv_color(self):
        code = ASSEMBLY.replace("sample r0.xyzw, v7.xyxx, t7.xyzw, s0", "sample r6.xy, v7.xyxx, t99.xyzw, s0\n"
                                "sample r0.xyzw, r6.xyxx, t7.xyzw, s0")
        self.assertEqual(analyze(code)["resources"][7]["role_candidates"], [])

    def test_scalar_tint_is_not_rgb_vector_evidence(self):
        code = ASSEMBLY.replace("cb0[0].xyzx", "cb0[0].xxxx")
        self.assertEqual(analyze(code)["resources"][7]["role_candidates"], [])

    def test_unsupported_control_flow_withholds_roles(self):
        code = ASSEMBLY.replace("sample r1.xyz", "switch cb0[0].x\ncase l(0)\nsample r1.xyz")
        code = code.replace("sqrt r2.z, r2.z", "sqrt r2.z, r2.z\nendswitch")
        a = analyze(code)
        self.assertTrue(a["limitations"])
        self.assertTrue(all(not r["role_candidates"] for r in a["resources"].values()))

    def test_early_return_withholds_roles(self):
        code = ASSEMBLY.replace("sample r1.xyz", "if_nz cb0[9].x\nret\nendif\nsample r1.xyz")
        a = analyze(code)
        self.assertTrue(all(not r["role_candidates"] for r in a["resources"].values()))

    def test_gather_does_not_pretend_to_read_rgba(self):
        code = ASSEMBLY.replace("sample r0.xyzw", "gather4 r0.xyzw")
        a = analyze(code)
        self.assertNotIn("diffuse", a["resources"][7]["role_candidates"])
        self.assertTrue(any("gather" in s for s in a["limitations"]))


class SlotTableTests(unittest.TestCase):
    def setUp(self):
        (HERE.parent / "captures").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=HERE.parent / "captures")
        self.folder = Path(self.tmp.name)
        self.analysis = analyze(ASSEMBLY)
        self.write([record()])

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records):
        (self.folder / "draws.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    def build(self):
        return slot_table.build(self.folder, reader=lambda _: self.analysis)

    def mod(self, diffuse=44, normal=45, light=46):
        path = self.folder / "Mods" / "Misleading"
        path.mkdir(parents=True, exist_ok=True)
        (path / "a.ini").write_text(f"[TextureOverrideBody]\nhash=aa000000\nmatch_index_count=300\n"
                                   f"ps-t{diffuse}=ResourceDiffuse\nps-t{normal}=ResourceNormal\nps-t{light}=ResourceLightMap\n",
                                   encoding="utf-8")

    def test_incorrect_mod_cannot_change_analysis(self):
        native = self.build()
        before = copy.deepcopy(native)
        self.mod()
        compared = slot_table.compare_mods(native, self.folder)
        self.assertEqual(native, before)
        self.assertEqual(compared["rows"], before["rows"])
        self.assertEqual(compared["native_analysis_sha256"], native["native_analysis_sha256"])
        checks = compared["mod_comparison"]["meshes"]["aa000000/300"]["checks"]
        self.assertEqual({c["status"] for c in checks}, {"differs_from_candidate"})
        self.mod(diffuse=7, normal=2, light=0)
        again = slot_table.compare_mods(native, self.folder)
        self.assertEqual(again["rows"], before["rows"])
        self.assertEqual({c["status"] for c in again["mod_comparison"]["meshes"]["aa000000/300"]["checks"]}, {"matches_candidate"})

    def test_hash_and_format_changes_do_not_assign_roles(self):
        first = self.build()["rows"][0]["primary"]
        self.write([record(views=[view(s, "R32_FLOAT", "ffffffff") for s in (0, 2, 7, 99)])])
        self.assertEqual(first, self.build()["rows"][0]["primary"])

    def test_pipeline_variants_do_not_merge(self):
        self.write([record(), record(rt=[]), record(ib="aa000001")])
        table = self.build()
        self.assertEqual(len(table["rows"][0]["passes"]), 2)
        self.assertEqual({p["category"] for p in table["rows"][0]["passes"]}, {"lighting_candidate", "depth_or_discard"})

    def test_color_only_lighting_is_not_promoted_to_primary(self):
        self.write([record(views=[view(s) for s in (7, 99)])])
        row = self.build()["rows"][0]
        self.assertEqual(row["passes"][0]["category"], "lighting_candidate")
        self.assertIsNone(row["primary"])

    def test_no_fabricated_binding_from_record_union(self):
        self.write([record(), record(views=[view(s) for s in (0, 2, 99)])])
        p = self.build()["rows"][0]["passes"][0]
        self.assertIsNone(p["roles"]["diffuse"])
        self.assertEqual(next(s for s in p["slots"] if s["slot"] == 7)["bound_records"], 1)

    def test_unknown_shader_never_uses_mod_labels(self):
        self.analysis = {"resources": {}, "error": "missing shader", "limitations": []}
        self.mod(diffuse=7, light=0, normal=2)
        t = slot_table.compare_mods(self.build(), self.folder)
        self.assertIsNone(t["rows"][0]["primary"])
        self.assertEqual({c["status"] for c in t["mod_comparison"]["meshes"]["aa000000/300"]["checks"]}, {"unresolved_native"})

    def test_stale_binding_and_mod_draw_removed(self):
        self.write([record(views=[view(s) for s in (0, 2, 7, 99, 100)]), record(ib="deadbeef", origin="xxmi")])
        t = self.build()
        self.assertEqual(t["meshes"], 1)
        self.assertNotIn(100, [s["slot"] for s in t["rows"][0]["passes"][0]["slots"]])

    def test_paging_validation_and_no_files(self):
        self.write([record(), record(ib="aa000001")])
        before = sorted(self.folder.rglob("*"))
        t = self.build()
        q = slot_table.query(t, limit=1, include_evidence=True)
        self.assertEqual(q["next_offset"], 1)
        self.assertEqual(q["matching_meshes"], 2)
        self.assertTrue(q["shader_evidence"])
        self.assertNotIn("passes", q["rows"][0])
        self.assertIn("passes", slot_table.query(t, include_passes=True)["rows"][0])
        self.assertEqual(slot_table.query(t, include_evidence=False)["shader_evidence"], {})
        self.assertEqual(before, sorted(self.folder.rglob("*")))
        with self.assertRaises(ValueError): slot_table.query(t, limit=201)

    def test_cache_reuse_append_shader_change_and_refresh(self):
        cache = slot_table.TableCache()
        with patch.object(slot_table, "build", return_value=self.build()) as build:
            cache.get(self.folder)
            cache.get(self.folder)
            self.assertEqual(build.call_count, 1)
            with (self.folder / "draws.jsonl").open("a") as f: f.write("\n")
            cache.get(self.folder)
            (self.folder / "shaders").mkdir()
            (self.folder / "shaders" / f"{PS}-ps.bin").write_bytes(b"new")
            cache.get(self.folder)
            cache.get(self.folder, refresh=True)
            self.assertEqual(build.call_count, 4)

    def test_mod_root_only_read_after_build(self):
        cache = slot_table.TableCache()
        native = self.build()
        calls = []
        with patch.object(slot_table, "build", side_effect=lambda *a, **k: calls.append("native") or native), \
             patch.object(slot_table, "compare_mods", side_effect=lambda *a: calls.append("compare") or native):
            cache.get(self.folder, self.folder)
        self.assertEqual(calls, ["native", "compare"])

    def test_record_append_reuses_unchanged_shader_analysis(self):
        (self.folder / "shaders").mkdir()
        shader = self.folder / "shaders" / f"{PS}-ps.bin"
        shader.write_bytes(b"compiled")
        cache = slot_table.TableCache()
        with patch.object(slot_table, "disassemble", return_value=ASSEMBLY) as disasm:
            cache.get(self.folder)
            self.write([record(), record(ib="aa000001")])
            self.assertEqual(cache.get(self.folder)["meshes"], 2)
            self.assertEqual(disasm.call_count, 1)
            shader.write_bytes(b"changed bytecode")
            cache.get(self.folder)
            self.assertEqual(disasm.call_count, 2)

    def test_mcp_contract(self):
        server = Server("test", "0")
        slot_table.register(server, SimpleNamespace(efmi_root=None))
        self.assertTrue(server.tools["efmi_get_slot_table"].read_only)
        with patch.object(slot_table, "shader_reader", return_value=lambda _: self.analysis):
            reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "efmi_get_slot_table", "arguments": {"folder": str(self.folder)}}})
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["schema"], 2)

    def test_explicit_cli_export(self):
        with patch.object(slot_table, "shader_reader", return_value=lambda _: self.analysis):
            self.assertEqual(slot_table.main([str(self.folder)]), 0)
        self.assertTrue((self.folder / "slot-table.json").is_file())
        self.assertTrue((self.folder / "slot-evidence.json").is_file())
        self.assertNotIn("passes", json.loads((self.folder / "slot-table.json").read_text())["rows"][0])
        md = (self.folder / "slot-table.md").read_text(encoding="utf-8")
        self.assertIn("t0 (candidate)", md)
        self.assertIn("t7 (candidate)", md)

    def test_old_format_and_partial_tail_are_ignored(self):
        with (self.folder / "draws.jsonl").open("a") as f:
            f.write('{"fmt":1}\n{"fmt":2,')
        self.assertEqual(len(slot_table.load_records(self.folder)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
