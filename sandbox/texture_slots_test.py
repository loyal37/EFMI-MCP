"""Texture-slot evidence, batch efficiency and MCP contract without a running game."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from efmi_mcp import shader, tools
from efmi_mcp.capture_model import Capture
from efmi_mcp.probe_client import ProbeEndpoint, ProbeError

PS = "1234567890abcdef"
VS = "abcdef1234567890"
ASSEMBLY = """// A comment mentioning t99 must not create a reference.
ps_5_0
dcl_resource_texture2d (float,float,float,float) t0
dcl_resource_texture2d (float,float,float,float) t1
dcl_resource_texture2d (float,float,float,float) t2
dcl_resource_structured t3, 16
dcl_resource_texture2d (float,float,float,float) t4
dcl_resource_texture2d (float,float,float,float) t70
dcl_sampler s1, mode_default
sample_b_indexable(texture2d)(float,float,float,float) r0.xyz, v0.xyxx, t0.xyzw, s1, l(0)
if_nz r1.x
  sample_c_lz_indexable(texture2d)(float,float,float,float) r0.w, v0.xyxx, t70.xxxx, s1, l(0)
else
  ld_indexable(texture2d)(float,float,float,float) r0.w, l(0,0,0,0), t1.xxxx
endif
resinfo_uint r2.xyzw, l(0), t2.xyzw
ld_structured_indexable(structured_buffer, stride=16)(mixed,mixed,mixed,mixed) r3.xyzw, l(0), l(0), t3.xyzw
gather4_indexable(texture2d)(float,float,float,float) r4.xyzw, v0.xyxx, t0.xyzw, s1 // t98
ret
"""
VERTEX = """vs_5_0
dcl_resource_texture1d (float,float,float,float) t0
ld_indexable(texture1d)(float,float,float,float) r0.xyzw, l(0,0,0,0), t0.xyzw
ret
"""


def document():
    resources = [
        {"kind": "tex2d", "w": 64, "h": 32, "fmt_name": "R8G8B8A8_UNORM_SRGB", "known": True, "hash": "11111111", "origin": "game"},
        {"kind": "tex2d", "w": 32, "h": 32, "fmt_name": "BC5_UNORM", "known": True, "hash": "22222222", "origin": "xxmi"},
        {"kind": "buffer", "size": 256, "stride": 16, "known": True, "hash": "33333333"},
        {"kind": "buffer", "size": 12, "known": True, "hash": "44444444"},
        {"kind": "tex2d", "w": 128, "h": 64, "fmt_name": "R16G16B16A16_FLOAT", "known": False},
    ]
    draw = {"i": 0, "t": 2, "o": "xxmi", "ic": 6, "fi": 0, "ib": [3, 57, 0, 0],
            "sh": [0, -1, -1, -1, 1, -1],
            "srv": [[4, 0, 0], [4, 1, 1], [4, 2, 0], [4, 3, 2], [4, 4, 0], [4, 9, 1], [0, 0, 0]],
            "out": [[10, 0, 4]]}
    second = {**copy.deepcopy(draw), "i": 1, "fi": 6}
    return {"frame": 100, "detail": "full", "resources": resources,
            "regions": [[3, 0, 12, "aabbccdd", "ok"]],
            "shaders": [{"known": True, "type": "vs", "hash": VS}, {"known": True, "type": "ps", "hash": PS}],
            "draws": [draw, second]}


class FakeProbe:
    def __init__(self):
        self.connected = True
        self.endpoint = ProbeEndpoint(1234, "fake-never-opened")
        self.reads = []
        self.error = False

    def shader(self, h):
        self.reads.append(h)
        if self.error:
            raise ProbeError("shader unavailable")
        return {"type": "vs" if h == VS else "ps"}, (VERTEX if h == VS else ASSEMBLY).encode()


class AssemblyTests(unittest.TestCase):
    def test_references_are_not_declarations_or_comments(self):
        resources = shader.resource_usage(ASSEMBLY)["resources"]
        self.assertEqual(set(resources), {0, 1, 2, 3, 4, 70})
        self.assertEqual(len(resources[0]["instructions"]), 2)
        self.assertEqual(resources[4]["instructions"], [])

    def test_sample_load_query_and_gather_are_separate(self):
        resources = shader.resource_usage(ASSEMBLY)["resources"]
        self.assertEqual(resources[0]["instructions"][1]["access"], "sample")
        self.assertEqual(resources[1]["instructions"][0]["access"], "load")
        self.assertEqual(resources[2]["instructions"][0]["access"], "query")
        self.assertEqual(resources[3]["dimension"], "structured")

    def test_branch_fallbacks_and_line_evidence_are_retained(self):
        ref = shader.resource_usage(ASSEMBLY)["resources"][70]["instructions"][0]
        self.assertEqual(ref["samplers"], [1])
        self.assertEqual(ASSEMBLY.splitlines()[ref["line"] - 1].strip(), ref["text"])

    def test_missing_or_unsupported_profiles_are_errors(self):
        for text in ("not assembly", "ps_5_1\ndcl_resource_texture2d t0[0:5], space=0", "ps_6_0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                shader.resource_usage(text)

    def test_indexed_resources_are_not_mistaken_for_a_fixed_slot(self):
        for operand in ("t0[r0.x]", "t[r0.x]"):
            with self.subTest(operand=operand), self.assertRaisesRegex(ValueError, "indexed"):
                shader.resource_usage("ps_5_0\nld r0, r1, " + operand)

    def test_unknown_instruction_stays_a_reference(self):
        ref = shader.resource_usage("ps_4_1\nfuture_op r0, t8.x")["resources"][8]["instructions"][0]
        self.assertEqual(ref["access"], "reference")

    def test_sample_info_does_not_claim_to_read_texels(self):
        ref = shader.resource_usage("ps_5_0\nsample_info r0, t8")["resources"][8]["instructions"][0]
        self.assertEqual(ref["access"], "query")


class ToolTests(unittest.TestCase):
    def setUp(self):
        build = Path(__file__).resolve().parent / "build"
        build.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=build)
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(build.resolve()))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = tools.Session(self.root, self.root / "not-created", self.root / "probe.dll")
        self.probe = FakeProbe()
        self.session.client = self.probe
        self.session.add_capture(Capture(document()))
        with patch.object(tools, "Session", return_value=self.session):
            self.server = tools.build_server(self.root, self.session.out_dir, self.session.probe_dll)
        self.compiler = patch.object(shader, "disassemble", side_effect=lambda b: b.decode()).start()
        self.addCleanup(patch.stopall)

    def call(self, name="efmi_get_texture_slots", **args):
        result = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})["result"]
        text = result["content"][0]["text"]
        self.assertFalse(result["isError"], text)
        return json.loads(text) if name == "efmi_get_texture_slots" else text

    def error(self, **args):
        result = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "efmi_get_texture_slots", "arguments": args}})["result"]
        self.assertTrue(result["isError"], result)
        self.assertNotIn("internal error", result["content"][0]["text"])

    def test_default_returns_references_and_unbound_fallback(self):
        result = self.call()
        group = result["groups"][0]
        slots = {r["slot"]: r for r in group["textures"]}
        self.assertEqual(set(slots), {"ps-t0", "ps-t1", "ps-t2", "ps-t70"})
        self.assertEqual(slots["ps-t1"]["hash"], "22222222")
        self.assertEqual(slots["ps-t0"]["w"], 64)
        self.assertFalse(slots["ps-t70"]["bound"])
        self.assertTrue(all(r["reference"] == "referenced" for r in slots.values()))
        self.assertIn("o0", group["outputs"])

    def test_batch_groups_draws_without_losing_mesh_ranges(self):
        result = self.call()
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["draws"], [0, 1])
        self.assertEqual([m["args"]["first_index"] for m in result["groups"][0]["meshes"]], [0, 6])
        self.assertEqual(self.probe.reads, [PS])

    def test_warm_queries_and_shader_paging_share_cache_without_files(self):
        self.call()
        self.call(draws=[1])
        text = self.call("efmi_get_shader", hash=PS, first_line=3, max_lines=2)
        self.assertIn("dcl_resource_texture2d", text)
        self.assertEqual(self.probe.reads, [PS])
        self.assertEqual(self.compiler.call_count, 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_shader_export_is_explicit_and_can_follow_a_cached_query(self):
        self.call()
        self.call("efmi_get_shader", hash=PS, save=True)
        folder = self.session.out_dir / "shaders"
        self.assertEqual((folder / f"{PS}-ps.bin").read_bytes(), ASSEMBLY.encode())
        self.assertEqual((folder / f"{PS}-ps.asm").read_text(), ASSEMBLY)
        self.assertEqual(self.probe.reads, [PS])

    def test_unused_and_buffers_are_explicit(self):
        result = self.call(include_unused=True, include_buffers=True)
        slots = {r["slot"]: r for r in result["groups"][0]["textures"]}
        self.assertEqual(slots["ps-t4"]["reference"], "declared_only")
        self.assertEqual(slots["ps-t9"]["reference"], "bound_only")
        self.assertEqual(slots["ps-t3"]["kind"], "buffer")

    def test_all_stages_are_separate(self):
        result = self.call(stage="all")
        self.assertEqual({g["stage"] for g in result["groups"]}, {"ps", "vs"})
        self.assertEqual(set(self.probe.reads), {PS, VS})
        self.assertEqual(len(result["shaders"]), 2)

    def test_selection_filters_paging_and_hash_normalization(self):
        first = self.call(ib_hash="0xAABBCCDD", shader="0x" + PS.upper(), origin="xxmi", limit=1)
        self.assertEqual(first["matching_draws"], 2)
        self.assertEqual(first["next_offset"], 1)
        last = self.call(offset=1, limit=1)
        self.assertEqual(last["groups"][0]["draws"], [1])
        self.assertIsNone(last["next_offset"])
        self.assertEqual(self.call(ib_hash="44444444")["returned_draws"], 2)
        self.assertEqual(self.call(draws=[1, 1])["returned_draws"], 1)
        self.assertEqual(self.call(origin="game")["groups"], [])

    def test_validation_and_light_capture_are_actionable(self):
        for args in ({"draws": [-1]}, {"draws": [True]}, {"ib_hash": "bad"}, {"shader": "bad"},
                     {"stage": "bad"}, {"origin": "bad"}, {"limit": 0}, {"limit": 201},
                     {"offset": -1}, {"instruction_limit": 9}):
            with self.subTest(args=args):
                self.error(**args)
        self.session.captures[100].detail = "light"
        self.error()

    def test_unavailable_shader_is_unknown_and_retried_only_on_a_later_query(self):
        self.probe.error = True
        result = self.call()
        self.assertIn("shader unavailable", result["shaders"][PS]["error"])
        self.assertTrue(all(r["reference"] == "unknown" for r in result["groups"][0]["textures"]))
        self.assertEqual(self.probe.reads, [PS])
        self.probe.error = False
        result = self.call()
        self.assertIn("profile", result["shaders"][PS])
        self.assertEqual(self.probe.reads, [PS, PS])

    def test_disassembly_failure_preserves_bound_metadata(self):
        self.compiler.side_effect = OSError("compiler unavailable")
        result = self.call()
        self.assertIn("compiler unavailable", result["shaders"][PS]["error"])
        self.assertEqual(result["groups"][0]["textures"][0]["hash"], "11111111")

    def test_unsupported_assembly_does_not_claim_that_slots_are_unused(self):
        self.compiler.side_effect = lambda b: "ps_5_1\ndcl_resource_texture2d t0[0:5], space=0"
        result = self.call()
        self.assertIn("unsupported", result["shaders"][PS]["error"])
        self.assertEqual(result["groups"][0]["textures"][0]["reference"], "unknown")

    def test_connection_change_invalidates_analysis(self):
        self.call()
        replacement = FakeProbe()
        self.session.client = replacement
        self.call()
        self.assertEqual(replacement.reads, [PS])
        self.assertEqual(self.compiler.call_count, 2)

    def test_different_resources_prevent_group_merging(self):
        cap = self.session.captures[100]
        cap.raw_draws[1]["srv"][0][2] = 1
        result = self.call()
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(len(result["shaders"]), 1)

    def test_views_of_the_same_resource_are_kept_separate(self):
        cap = self.session.captures[100]
        cap.views.extend([[0, 0, 28, 4, 0, 1, 0, 0, 0], [0, 0, 28, 4, 1, 1, 0, 0, 0]])
        cap.raw_draws[0]["srvv"] = [0]
        cap.raw_draws[1]["srvv"] = [1]
        result = self.call()
        self.assertEqual(len(result["groups"]), 2)
        views = [g["textures"][0]["view"] for g in result["groups"]]
        self.assertNotEqual(views[0], views[1])

    def test_missing_pixel_shader_does_not_turn_stale_bindings_into_use(self):
        for draw in self.session.captures[100].raw_draws:
            draw["sh"][4] = -1
        self.assertEqual(self.call()["groups"][0]["textures"], [])
        slots = self.call(include_unused=True)["groups"][0]["textures"]
        self.assertTrue(all(r["reference"] == "no_shader" for r in slots))
        self.assertEqual(self.probe.reads, [])

    def test_unrecorded_shader_hash_preserves_unknown_bound_slots(self):
        self.session.captures[100].shaders[1]["known"] = False
        result = self.call()
        slots = result["groups"][0]["textures"]
        self.assertTrue(slots)
        self.assertTrue(all(r["reference"] == "unknown" for r in slots))
        self.assertEqual(self.probe.reads, [])

    def test_instruction_limit_does_not_hide_reference_counts(self):
        result = self.call(instruction_limit=0)
        slot = result["shaders"][PS]["resources"]["t0"]
        self.assertEqual(slot["instruction_count"], 2)
        self.assertEqual(slot["instructions"], [])


class CacheTests(unittest.TestCase):
    def test_lru_eviction_and_byte_budget(self):
        with patch.object(shader, "disassemble", return_value="ps_5_0"):
            cache = shader.ShaderCache(max_entries=2, max_bytes=64)
            load = lambda: ({"type": "ps"}, b"1234")
            cache.get("a", load)
            cache.get("b", load)
            cache.get("a", load)
            cache.get("c", load)
            self.assertEqual(list(cache.entries), ["a", "c"])
            self.assertLessEqual(cache.bytes_used, 64)
            cache.get("big", lambda: ({}, bytes(100)))
            self.assertNotIn("big", cache.entries)
            cache.clear()
            self.assertEqual(cache.bytes_used, 0)
            self.assertFalse(cache.entries)


if __name__ == "__main__":
    unittest.main(verbosity=1)
