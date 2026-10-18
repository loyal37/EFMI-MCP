"""The dataflow analysis of pixel shader assembly that names diffuse, normal map and light map slots
(server/efmi_mcp/shader_semantics.py, used by the slot table).

Roles follow what the instructions do with a sample, not register numbers: a register permutation
keeps every role; a plain two-channel sample, an overwritten component, a dependent lookup or a
scalar tint is not enough; both sides of a branch count; loops, unsupported control flow and an
early return withhold roles; a gather does not pretend to read RGBA.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

from efmi_mcp.shader_semantics import analyze  # noqa: E402

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
