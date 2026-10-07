"""Offline checks of the model workflow's own logic.

No game, no Blender and no LoyalTools: export inspection, deployment rules,
the run-time comparison (against a fake capture) and job handling. The steps
that belong to LoyalTools and Blender are exercised by model_offline_integration.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import blender_runner, loyal_bridge, mod_runtime, model_capture, model_workflow, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import crc32c, hex32  # noqa: E402
from efmi_mcp.model_jobs import JobManager  # noqa: E402
from efmi_mcp.model_workflow import WorkflowError, sha256_file  # noqa: E402
from efmi_mcp.probe_client import ProbeNotFound  # noqa: E402

INDICES = struct.pack("<6I", 0, 1, 2, 2, 1, 3)
POSITIONS = bytes(range(64))            # 4 vertices, stride 16
INI = """[TextureOverride_EntryPoint_Component0]
hash = aaaaaaaa
match_first_index = 0
match_index_count = 6
run = CommandList_Draw_Component0

[CommandList_Draw_Component0]
ib = Resource_Mesh_Index
vb0 = Resource_Mesh_Position
drawindexed = 6,0,0

[Resource_Mesh_Index]
type = Buffer
format = DXGI_FORMAT_R32_UINT
filename = Meshes\\mesh-Index.buf

[Resource_Mesh_Position]
type = Buffer
stride = 16
filename = Meshes\\mesh-Position.buf
"""
PROFILE = {"components": [{"unique_str": "aaaaaaaa-6-0", "ib_hash": "aaaaaaaa", "index_count": 6,
                           "first_index": 0, "lods": []}]}


class Scratch(unittest.TestCase):
    def setUp(self):
        build = ROOT / "sandbox" / "build"
        build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="model-test-", dir=build)
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def write_mod(self, name="export", ini=INI, indices=INDICES, positions=POSITIONS) -> Path:
        mod = self.tmp / name / "mod"
        (mod / "Meshes").mkdir(parents=True)
        (mod / "test.ini").write_text(ini, encoding="utf-8")
        (mod / "Meshes" / "mesh-Index.buf").write_bytes(indices)
        (mod / "Meshes" / "mesh-Position.buf").write_bytes(positions)
        return mod

    def manifest(self, mod: Path, export_id="e1") -> dict:
        inspection = model_workflow.inspect_export(mod, PROFILE)
        return {"export_id": export_id, "valid": not inspection["errors"], "project": "test", "mod_dir": str(mod),
                "entry_points": inspection["entry_points"], "draws": inspection["draws"],
                "files": [{"path": p.relative_to(mod).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
                          for p in sorted(mod.rglob("*")) if p.is_file()]}


class InspectExportTests(Scratch):
    def test_complete_export_passes_and_records_what_each_draw_reads(self):
        result = model_workflow.inspect_export(self.write_mod(), PROFILE)
        self.assertEqual(result["errors"], [])
        draw = result["draws"][0]
        self.assertEqual((draw["ib_file"], draw["vb0_file"]), ("Meshes/mesh-Index.buf", "Meshes/mesh-Position.buf"))
        self.assertEqual((draw["max_index"], draw["vertex_count"]), (3, 4))
        self.assertEqual(result["entry_points"][0]["index_files"], ["Meshes/mesh-Index.buf"])
        self.assertEqual(result["entry_point_coverage"]["full_detail"], ["aaaaaaaa-6-0"])
        self.assertEqual(result["entry_point_coverage"]["missing"], [])

    def test_missing_referenced_file_is_an_error(self):
        mod = self.write_mod()
        (mod / "Meshes" / "mesh-Position.buf").unlink()
        self.assertTrue(any("missing file" in e for e in model_workflow.inspect_export(mod)["errors"]))

    def test_size_that_does_not_fit_the_stride_is_an_error(self):
        errors = model_workflow.inspect_export(self.write_mod(positions=POSITIONS + b"\0"))["errors"]
        self.assertTrue(any("not a multiple of 16" in e for e in errors))

    def test_draw_range_beyond_the_index_buffer_is_an_error(self):
        errors = model_workflow.inspect_export(self.write_mod(ini=INI.replace("drawindexed = 6,0,0", "drawindexed = 9,0,0")))["errors"]
        self.assertTrue(any("exceeds" in e for e in errors))

    def test_index_outside_the_vertex_buffer_is_an_error(self):
        errors = model_workflow.inspect_export(self.write_mod(indices=struct.pack("<6I", 0, 1, 2, 2, 1, 4)))["errors"]
        self.assertTrue(any("outside 4 vertices" in e for e in errors))

    def test_component_without_entry_point_is_reported(self):
        profile = {"components": PROFILE["components"] + [
            {"unique_str": "bbbbbbbb-9-0", "ib_hash": "bbbbbbbb", "index_count": 9, "first_index": 0, "lods": []}]}
        result = model_workflow.inspect_export(self.write_mod(), profile)
        self.assertEqual(result["entry_point_coverage"]["missing"], ["bbbbbbbb-9-0"])
        self.assertTrue(result["warnings"])

    def test_folder_without_ini_is_not_an_export(self):
        (self.tmp / "empty").mkdir()
        self.assertEqual(model_workflow.inspect_export(self.tmp / "empty")["errors"], ["no INI file"])


class DeploymentTests(Scratch):
    def setUp(self):
        super().setUp()
        self.efmi = self.tmp / "EFMI"
        (self.efmi / "Mods").mkdir(parents=True)
        self.target = mod_runtime.resolve_target(self.efmi, "", "test")
        self.backup = self.tmp / "backup"

    def test_default_target_is_a_folder_of_its_own_under_mods(self):
        self.assertEqual(self.target, (self.efmi / "Mods" / "EFMI-MCP" / "test").resolve())

    def test_targets_outside_mods_or_disabled_are_refused(self):
        for bad in (str(self.efmi / "Core" / "x"), str(self.efmi / "Mods"), "..\\Core\\x", "DISABLED test"):
            with self.subTest(target=bad), self.assertRaises(WorkflowError):
                mod_runtime.resolve_target(self.efmi, bad, "test")

    def test_new_deployment_is_identical_and_marked(self):
        manifest = self.manifest(self.write_mod())
        result = mod_runtime.deploy_export(manifest, self.target, self.backup)
        self.assertTrue(result["verified_identical"])
        self.assertEqual(result["copied"], 3)
        status = mod_runtime.deployment_status(self.target)
        self.assertEqual((status["managed"], status["export_id"], status["changed_or_missing"]), (True, "e1", []))

    def test_invalid_export_is_not_deployed(self):
        manifest = self.manifest(self.write_mod())
        manifest["valid"] = False
        with self.assertRaises(WorkflowError):
            mod_runtime.deploy_export(manifest, self.target, self.backup)
        self.assertFalse(self.target.exists())

    def test_export_changed_after_its_check_is_not_deployed(self):
        mod = self.write_mod()
        manifest = self.manifest(mod)
        (mod / "Meshes" / "mesh-Index.buf").write_bytes(INDICES[::-1])
        with self.assertRaises(WorkflowError):
            mod_runtime.deploy_export(manifest, self.target, self.backup)
        self.assertFalse(self.target.exists())

    def test_folder_not_created_by_this_tool_is_never_written(self):
        self.target.mkdir(parents=True)
        (self.target / "someone.ini").write_text("[Constants]\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            mod_runtime.deploy_export(self.manifest(self.write_mod()), self.target, self.backup)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["someone.ini"])

    def test_update_replaces_only_recorded_files_and_keeps_the_old_ones_outside_mods(self):
        mod_runtime.deploy_export(self.manifest(self.write_mod("a")), self.target, self.backup)
        (self.target / "notes.txt").write_text("mine", encoding="utf-8")
        newer = self.write_mod("b", positions=POSITIONS[::-1])
        (newer / "Meshes" / "mesh-Index.buf").rename(newer / "Meshes" / "other-Index.buf")
        (newer / "test.ini").write_text(INI.replace("mesh-Index", "other-Index"), encoding="utf-8")
        result = mod_runtime.deploy_export(self.manifest(newer, "e2"), self.target, self.backup)
        self.assertEqual(result["removed_previous"], ["Meshes/mesh-Index.buf"])
        self.assertEqual(sorted(result["replaced_previous"]), ["Meshes/mesh-Position.buf", "test.ini"])
        self.assertFalse((self.target / "Meshes" / "mesh-Index.buf").exists())
        self.assertEqual((self.backup / "Meshes" / "mesh-Position.buf").read_bytes(), POSITIONS)
        self.assertEqual((self.target / "notes.txt").read_text(encoding="utf-8"), "mine")
        status = mod_runtime.deployment_status(self.target)
        self.assertEqual((status["export_id"], status["files_not_from_this_tool"]), ("e2", ["notes.txt"]))

    def test_file_added_by_someone_else_is_not_overwritten(self):
        mod_runtime.deploy_export(self.manifest(self.write_mod("a")), self.target, self.backup)
        (self.target / "extra.ini").write_text("[Constants]\n", encoding="utf-8")
        newer = self.write_mod("b")
        (newer / "extra.ini").write_text("[Present]\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            mod_runtime.deploy_export(self.manifest(newer, "e2"), self.target, self.backup)
        self.assertEqual((self.target / "extra.ini").read_text(encoding="utf-8"), "[Constants]\n")

    def test_changed_deployed_file_shows_in_status(self):
        mod_runtime.deploy_export(self.manifest(self.write_mod()), self.target, self.backup)
        (self.target / "test.ini").write_text("edited", encoding="utf-8")
        self.assertEqual(mod_runtime.deployment_status(self.target)["changed_or_missing"], ["test.ini"])

    def test_remove_deletes_only_what_was_deployed(self):
        mod_runtime.deploy_export(self.manifest(self.write_mod()), self.target, self.backup)
        (self.target / "notes.txt").write_text("mine", encoding="utf-8")
        result = mod_runtime.remove_deployment(self.target)
        self.assertEqual((result["removed_files"], result["left_in_place"]), (3, ["notes.txt"]))
        (self.target / "notes.txt").unlink()
        mod_runtime.deploy_export(self.manifest(self.write_mod("again")), self.target, self.backup)
        mod_runtime.remove_deployment(self.target)
        self.assertFalse((self.efmi / "Mods" / "EFMI-MCP").exists())
        self.assertTrue((self.efmi / "Mods").is_dir())

    def test_remove_refuses_a_folder_it_did_not_deploy(self):
        self.target.mkdir(parents=True)
        with self.assertRaises(WorkflowError):
            mod_runtime.remove_deployment(self.target)


class FakeClient:
    def __init__(self, buffers):
        self.buffers = buffers
        self.reads = []

    def read_buffer(self, frame, rid, offset=0, size=0, **kwargs):
        self.reads.append(rid)
        return {}, self.buffers[rid]


def capture(draws, buffers) -> Capture:
    """A frame in the probe's compact format; buffers: {id: (bytes, origin)}."""
    resources = [{"kind": "buffer", "size": len(data), "known": True, "origin": origin, "hash": "00000000"}
                 for _, (data, origin) in sorted(buffers.items())]
    regions, raw = [], []
    for number, draw in enumerate(draws):
        entry = {"i": number, "t": 2, "o": draw["origin"], "ic": draw["count"], "fi": draw.get("first", 0),
                 "fv": draw.get("base", 0), "inst": 1, "topo": 4}
        rid = draw["ib"]
        offset, size = draw.get("first", 0) * 4, draw["count"] * 4
        digest = draw.get("ib_hash") or hex32(crc32c(buffers[rid][0][offset:offset + size]))
        regions.append([rid, offset, size, digest, "ok"])
        entry["ib"] = [rid, 42, 0, len(regions) - 1]
        if "vb" in draw:
            data = buffers[draw["vb"]][0]
            regions.append([draw["vb"], 0, len(data), draw.get("vb_hash") or hex32(crc32c(data)), "ok"])
            entry["vb"] = [[0, draw["vb"], 16, 0, len(regions) - 1]]
        raw.append(entry)
    return Capture({"frame": 500, "detail": "full", "resolution": [1280, 720], "resources": resources,
                    "regions": regions, "draws": raw, "stats": {}})


class VerifyCaptureTests(Scratch):
    def setUp(self):
        super().setUp()
        self.manifest_ = self.manifest(self.write_mod())

    def check(self, draws, buffers, baseline=None):
        client = FakeClient({rid: data for rid, (data, _) in buffers.items()})
        return mod_runtime.verify_capture(capture(draws, buffers), client, self.manifest_, baseline), client

    def test_mod_drawn_with_the_exported_bytes_passes(self):
        result, client = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6}],
                                    {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi")},
                                    {"TextureOverride_EntryPoint_Component0": 1})
        self.assertEqual(result["target_mod_status"], "passed")
        self.assertEqual(result["covered"], ["TextureOverride_EntryPoint_Component0"])
        self.assertEqual(result["mod_draws"][0]["vertex_buffers"], {"vb0": "Meshes/mesh-Position.buf"})
        self.assertEqual({info["whole_buffer"] for info in result["exported_buffers_seen"].values()}, {"identical"})
        self.assertEqual(sorted(client.reads), [0, 1])

    def test_vertex_buffer_recorded_with_an_older_hash_is_judged_by_its_bytes(self):
        result, client = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6, "vb_hash": "deadbeef"}],
                                    {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi")})
        self.assertEqual(result["target_mod_status"], "passed")
        self.assertEqual(result["mod_draws"][0]["vertex_buffers"], {"vb0": "Meshes/mesh-Position.buf"})
        self.assertEqual(result["recorded_hash_disagreements"],
                         [{"resource": 1, "file": "Meshes/mesh-Position.buf", "recorded_hash": "deadbeef"}])
        self.assertEqual(result["exported_buffers_not_seen"], [])

    def test_index_buffer_recorded_with_an_older_hash_is_judged_by_its_bytes(self):
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6, "ib_hash": "0badf00d"}],
                               {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi")})
        self.assertEqual((result["target_mod_status"], result["draws_of_this_mod"]), ("passed", 1))
        self.assertEqual(result["stale_or_foreign_same_size"], [])
        self.assertEqual(result["recorded_hash_disagreements"],
                         [{"resource": 0, "file": "Meshes/mesh-Index.buf", "recorded_hash": "0badf00d"}])

    def test_position_buffer_of_the_right_size_with_other_bytes_fails(self):
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6}],
                               {0: (INDICES, "xxmi"), 1: (bytes(reversed(POSITIONS)), "xxmi")})
        self.assertEqual(result["target_mod_status"], "failed")
        self.assertIn("vb0", result["reasons"][0])
        self.assertEqual(result["vertex_bindings_with_other_bytes"], [{"draw": 0, "slot": "vb0", "bytes": len(POSITIONS)}])

    def test_frames_before_the_mod_starts_drawing_are_not_judged(self):
        buffers = {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi"), 2: (INDICES, "game")}
        client = FakeClient({rid: data for rid, (data, _) in buffers.items()})
        skipped_only = capture([{"origin": "game", "ib": 2, "count": 6, "ib_hash": "bbbbbbbb"}], buffers)   # other scenery
        original = capture([{"origin": "game", "ib": 2, "count": 6, "ib_hash": "aaaaaaaa"}], buffers)       # not skipped yet
        drawn = capture([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6}], buffers)
        later, pauses = [original, drawn], []
        cap, result = mod_runtime.verify_when_drawn(skipped_only, lambda: later.pop(0), client, self.manifest_,
                                                    sleep=pauses.append)
        self.assertIs(cap, drawn)
        self.assertEqual((result["target_mod_status"], result["frames_looked_at"], len(pauses)), ("passed", 3, 2))
        # A mod that never draws is still reported as what the last frame shows, after a bounded number of looks.
        cap, result = mod_runtime.verify_when_drawn(skipped_only, lambda: skipped_only, client, self.manifest_,
                                                    looks=4, sleep=pauses.append)
        self.assertEqual((result["target_mod_status"], result["frames_looked_at"]), ("inconclusive", 4))

    def test_game_mesh_still_drawn_fails_even_if_the_mod_is_drawn_too(self):
        buffers = {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi"), 2: (INDICES, "game")}
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 6},
                                {"origin": "game", "ib": 2, "count": 6, "ib_hash": "aaaaaaaa"}], buffers)
        self.assertEqual(result["target_mod_status"], "failed")
        self.assertIn("TextureOverride_EntryPoint_Component0 (original_still_drawn)", result["not_covered"])

    def test_buffer_of_the_same_size_with_other_bytes_is_a_stale_deployment(self):
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "count": 6}], {0: (INDICES[::-1], "xxmi")})
        self.assertEqual(result["target_mod_status"], "failed")
        self.assertEqual(result["draws_of_this_mod"], 0)
        self.assertEqual(len(result["stale_or_foreign_same_size"]), 1)

    def test_other_mods_draws_alone_are_inconclusive(self):
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "count": 3}], {0: (b"\0" * 12, "xxmi")})
        self.assertEqual(result["target_mod_status"], "inconclusive")
        self.assertEqual(result["xxmi_draws_of_other_content"], 1)
        self.assertEqual(result["exported_buffers_not_seen"], ["Meshes/mesh-Index.buf", "Meshes/mesh-Position.buf"])

    def test_frame_without_xxmi_draws_is_inconclusive_not_failed(self):
        result, _ = self.check([], {})
        self.assertEqual(result["target_mod_status"], "inconclusive")
        self.assertIn("may not be in view", result["reasons"][0])

    def test_draw_arguments_other_than_the_ini_fail(self):
        result, _ = self.check([{"origin": "xxmi", "ib": 0, "vb": 1, "count": 3}],
                               {0: (INDICES, "xxmi"), 1: (POSITIONS, "xxmi")})
        self.assertEqual(result["target_mod_status"], "failed")
        self.assertEqual(result["argument_mismatches"][0]["drawn"], [3, 0, 0])

    def test_whole_buffer_differing_outside_the_drawn_range_fails(self):
        # The drawn range (first three indices) matches; the rest of the buffer does not.
        self.manifest_["draws"][0]["index_count"] = 3
        tail_changed = INDICES[:12] + b"\xff" * 12
        client = FakeClient({0: tail_changed})
        result = mod_runtime.verify_capture(capture([{"origin": "xxmi", "ib": 0, "count": 3}],
                                                    {0: (tail_changed, "xxmi")}), client, self.manifest_)
        self.assertEqual(result["exported_buffers_seen"]["Meshes/mesh-Index.buf"]["whole_buffer"], "different")
        self.assertEqual(result["target_mod_status"], "failed")


class JobTests(unittest.TestCase):
    def test_result_error_and_single_job_at_a_time(self):
        jobs = JobManager()
        gate = threading.Event()
        first = jobs.start("extract", "first", lambda job: gate.wait(5) and {"ok": True})
        with self.assertRaises(RuntimeError):
            jobs.start("export", "second", lambda job: None)
        gate.set()
        first.thread.join(5)
        self.assertEqual((first.status, first.describe()["result"]), ("done", {"ok": True}))
        failed = jobs.start("export", "boom", lambda job: 1 / 0)
        failed.thread.join(5)
        self.assertEqual(failed.status, "failed")
        self.assertIn("ZeroDivisionError", failed.describe()["error"])

    def test_cancel_stops_at_the_next_step(self):
        jobs = JobManager()

        def work(job):
            for number in range(200):
                job.step(f"step {number}")
                time.sleep(0.01)

        job = jobs.start("extract", "long", work)
        time.sleep(0.05)
        job.cancel()
        job.thread.join(5)
        self.assertEqual(job.status, "cancelled")

    def test_probe_is_refused_to_other_tools_while_a_job_uses_it(self):
        session = tools.Session(Path("."), Path("."), Path("probe.dll"))
        gate = threading.Event()
        job = session.jobs.start("extract", "sampling", lambda job: gate.wait(5))
        try:
            with self.assertRaises(tools.ToolError) as ctx:
                session.probe()
            self.assertIn(job.id, str(ctx.exception))
        finally:
            gate.set()
            job.thread.join(5)
        quiet = session.jobs.start("export", "no probe", lambda job: gate.wait(5), uses_probe=False)
        with patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("none")):
            with self.assertRaises(tools.ToolError) as ctx:
                session.probe()
        self.assertNotIn(quiet.id, str(ctx.exception))
        quiet.thread.join(5)


class VolatileVertexDataTests(Scratch):
    """Vertex streams the game rewrites every few frames (CPU-posed components)."""

    MAIN_AT, LIGHT_AT, STRIDE = 400, 120, 8

    def setUp(self):
        super().setUp()
        (self.tmp / "buffers").mkdir()
        self.indices = struct.pack("<6H", 1, 2, 3, 3, 2, 1)             # vertices 1..3
        self.stream = bytes(range(200, 232))                             # what the later frame holds: 4 vertices
        resources = [{"kind": "buffer", "size": 12, "usage": 0}, {"kind": "buffer", "size": 1024, "usage": 2}]
        self.main = Capture({"frame": 50, "resources": resources, "draws": []})
        binding = {"res": 1, "offset": self.MAIN_AT, "stride": self.STRIDE, "region": None}
        draw = {"index": 7, "args": {"index_count": 6, "first_index": 0, "first_vertex": 0},
                "ib": {"res": 0, "format": 57, "offset": 0, "hash": "aaaaaaaa"}, "vb": {0: binding, 3: dict(binding)}}
        self.pairs = [(draw, {})]
        self.intact, self.read_shift = True, 0
        hashed = self.stream[:16]                                        # XXMI's window: the first two vertices
        self.light = {"frame": 60, "resources": resources, "stats": {},
                      "regions": [[0, 0, 12, "aaaaaaaa", "ok"], [1, self.LIGHT_AT, 16, hex32(crc32c(hashed)), "ok"]],
                      "draws": [{"i": 0, "t": 4, "o": "game", "ic": 6, "fi": 0, "fv": 0, "inst": 1, "topo": 4,
                                 "ib": [0, 57, 0, 0], "vb": [[0, 1, self.STRIDE, self.LIGHT_AT, 1]]}]}
        self.reads = []

    def capture(self, **kwargs):
        self.assertEqual(kwargs["detail"], "light")
        return self.light, b""

    def read_buffer(self, frame, res, offset=0, size=0, **kwargs):
        self.reads.append((frame, res, offset, size))
        start = offset - self.LIGHT_AT
        data = bytes((value + self.read_shift) % 256 for value in self.stream[start:start + size])
        return {"read_frame": frame + 4, "unchanged_since_capture": self.intact}, data

    def read(self):
        report = {"buffers": {}}
        buffers = {0: self.indices}
        result = model_capture._read_volatile(self, self.main, self.pairs, {1}, buffers, self.tmp, report)
        return result, report, buffers

    def test_index_bounds_follow_format_and_first_index(self):
        data = b"\0" * 4 + struct.pack("<4I", 9, 4, 7, 5)
        self.assertEqual(model_capture._index_bounds(data, {"format": 42, "offset": 4}, {"index_count": 3, "first_index": 1}), (4, 7))
        self.assertEqual(model_capture._index_bounds(self.indices, {"format": 57, "offset": 0}, {"index_count": 6}), (1, 3))

    def test_ranges_are_read_from_a_light_capture_and_stored_where_the_main_frame_bound_them(self):
        result, report, buffers = self.read()
        # Vertices 1..3 are needed, the hashed window starts at vertex 0: one read covers both.
        self.assertEqual(self.reads, [(60, 1, self.LIGHT_AT, 32)])
        self.assertEqual((result["frame"], result["main_frame"]), (60, 50))
        self.assertEqual(result["slots"][0]["bytes"], 32)
        record = report["buffers"]["1"]
        self.assertEqual((record["volatile"], record["ranges"], record["bytes"]), (True, [[self.MAIN_AT, 32]], 1024))
        stored = (self.tmp / "buffers" / "1.buf").read_bytes()
        self.assertEqual(stored[self.MAIN_AT:self.MAIN_AT + 32], self.stream)
        self.assertEqual(stored[:self.MAIN_AT] + stored[self.MAIN_AT + 32:], bytes(1024 - 32))
        self.assertEqual(sha256_file(self.tmp / "buffers" / "1.buf"), record["sha256"])
        self.assertEqual(buffers[1], stored)

    def test_data_rewritten_before_the_read_is_not_accepted(self):
        self.intact = False
        with self.assertRaises(model_capture.VolatileGeometry):
            self.read()
        self.intact, self.read_shift = True, 1          # the probe saw no write, but the bytes are not the draw's
        with self.assertRaises(model_capture.VolatileGeometry):
            self.read()
        self.read_shift = 0
        self.light["draws"][0]["ic"] = 9                # the mesh is not drawn the same way in the later frame
        with self.assertRaises(model_capture.VolatileGeometry):
            self.read()

    def test_archive_starts_over_with_a_new_frame_and_gives_up_after_a_few(self):
        target = self.tmp / "sample"
        calls = []

        def once(client, output, deferred, on_stage, attempt):
            self.assertFalse(output.exists() and any(output.iterdir()), "leftovers of the failed attempt")
            output.mkdir(parents=True, exist_ok=True)
            (output / "partial.bin").write_bytes(b"x")
            calls.append(attempt)
            if attempt < 3:
                raise model_capture.VolatileGeometry("rewritten")
            return {"attempt": attempt}

        with patch.object(model_capture, "_archive_once", side_effect=once):
            self.assertEqual(model_capture.archive_sample(None, target), {"attempt": 3})
        self.assertEqual(calls, [1, 2, 3])
        with patch.object(model_capture, "_archive_once", side_effect=model_capture.VolatileGeometry("rewritten")) as always:
            with self.assertRaises(model_capture.VolatileGeometry):
                model_capture.archive_sample(None, self.tmp / "never")
        self.assertEqual(always.call_count, model_capture.ATTEMPTS)


class SampleValidationTests(unittest.TestCase):
    """Three samples: static geometry has to agree, bone merges have to be real."""

    A, B = "aaaaaaaa-6-0", "bbbbbbbb-6-0"

    def sample(self, frame, merged=True, apart=0.0, sha="s0", volatile=False):
        import numpy as np
        bone = np.eye(4, dtype=np.float32) + np.float32(frame * 1e-3)          # the pose differs from frame to frame
        palettes = {self.A: [bone.copy(), bone + 1], self.B: [bone + np.float32(apart), bone + 5]}
        stream = {"stride": 16, "bytes": 64, "sha256": None if volatile else sha, "layout": "POSITION"}
        if volatile:
            stream["volatile"] = True
        entry = {"vertex_count": 4, "index_count": 6, "index_sha256": "i0", "vbs": {"0": stream}, "vg_count": 2, "cpu_posed": False}
        signature = {self.A: {**entry, "canonical_sources": {"0": [self.A, 0], "1": [self.A, 1]}},
                     self.B: {**entry, "canonical_sources": {"0": [self.A, 0] if merged else [self.B, 0], "1": [self.B, 1]}}}
        payloads = {(self.A, "ib"): b"indices"}
        if not volatile:
            payloads[(self.A, "vb0")] = sha.encode()
        return {"signature": signature, "palettes": palettes, "frame": frame, "merged_bones": int(merged),
                "matrices": {(self.A, 0): palettes[self.A][0], (self.B, 0): palettes[self.B][0]},
                "geometry_payloads": payloads,
                "volatile_vertex_slots": [[self.A, "vb0"], [self.B, "vb0"]] if volatile else []}

    def validate(self, *samples):
        return loyal_bridge._validate_static_samples(list(samples), [Path(f"sample-{n}") for n in range(1, len(samples) + 1)])

    def test_merges_found_in_only_some_frames_are_accepted_when_the_bones_are_the_same(self):
        # The game's matrices of one bone differ in the last bit between components in some frames.
        result = self.validate(self.sample(1), self.sample(2, merged=False, apart=6e-8), self.sample(3, merged=False, apart=3e-8))
        self.assertEqual(result["merged_bones"], [1, 0, 0])
        self.assertEqual(result["changed_used_bone_matrices"], 2)

    def test_merge_of_bones_that_only_coincided_in_the_first_frame_is_refused(self):
        with self.assertRaises(loyal_bridge.BridgeError) as ctx:
            self.validate(self.sample(1), self.sample(2, merged=False, apart=0.01), self.sample(3))
        self.assertIn("not the same bone", str(ctx.exception))

    def test_different_static_geometry_is_refused(self):
        with self.assertRaises(loyal_bridge.BridgeError) as ctx:
            self.validate(self.sample(1), self.sample(2), self.sample(3, sha="s1"))
        self.assertIn("Static geometry or layout differ", str(ctx.exception))

    def test_rewritten_vertex_streams_are_compared_by_size_and_layout_only(self):
        result = self.validate(*(self.sample(frame, volatile=True) for frame in (1, 2, 3)))
        self.assertEqual(result["volatile_vertex_data"], [[self.A, "vb0"], [self.B, "vb0"]])

    def test_samples_of_one_frame_do_not_validate_anything(self):
        with self.assertRaises(loyal_bridge.BridgeError) as ctx:
            self.validate(self.sample(1), self.sample(1), self.sample(1))
        self.assertIn("distinct", str(ctx.exception))


class LookupTests(Scratch):
    """Which Blender and which LoyalTools a step uses."""

    def test_a_named_blender_that_is_missing_is_never_replaced_by_another_install(self):
        known = self.tmp / "blender.exe"
        known.write_bytes(b"")
        missing = str(self.tmp / "missing" / "blender.exe")
        with patch.object(blender_runner, "KNOWN_BLENDER_PATHS", (str(known),)), patch.dict(os.environ):
            os.environ.pop("EFMI_BLENDER", None)
            self.assertEqual(blender_runner.find_blender(""), known)
            with self.assertRaises(blender_runner.BlenderError):
                blender_runner.find_blender(missing)
            os.environ["EFMI_BLENDER"] = missing
            with self.assertRaises(blender_runner.BlenderError):
                blender_runner.find_blender("")

    def tree(self, name: str) -> Path:
        root = self.tmp / name
        (root / "extract").mkdir(parents=True)
        (root / "extract" / "dump_workspace_extractor.py").write_text("", encoding="utf-8")
        (root / "__init__.py").write_text('bl_info = {"version": (9, 8, 7)}\n', encoding="utf-8")
        return root

    def test_installed_addon_is_used_unless_a_folder_is_configured(self):
        addon, configured = self.tree("addon"), self.tree("configured")
        self.assertEqual(loyal_bridge.resolve_loyaltools(None, addon), (addon.resolve(), "add-on installed in Blender"))
        self.assertEqual(loyal_bridge.resolve_loyaltools(configured, addon), (configured.resolve(), "configured"))
        self.assertEqual(loyal_bridge.loyaltools_version(addon), "9.8.7")
        with self.assertRaises(loyal_bridge.BridgeError):
            loyal_bridge.resolve_loyaltools(self.tmp / "not-loyaltools", addon)

    def test_session_takes_the_addon_of_the_blender_it_is_asked_about(self):
        addons = {"a.exe": self.tree("addon-a"), "b.exe": self.tree("addon-b")}
        session = tools.Session(Path("."), Path("."), Path("probe.dll"), blender_path="a.exe")
        with patch.object(blender_runner, "find_blender", side_effect=lambda named="": Path(named)), \
                patch.object(blender_runner, "find_addon", side_effect=lambda blender: addons[blender.name]):
            self.assertEqual(session.loyaltools()["path"], str(addons["a.exe"].resolve()))
            self.assertEqual(session.loyaltools("b.exe")["path"], str(addons["b.exe"].resolve()))
        with patch.object(blender_runner, "find_blender", side_effect=blender_runner.BlenderError("not found")):
            with self.assertRaises(tools.ToolError):
                session.loyaltools("c.exe")


class ToolBoundaryTests(Scratch):
    def setUp(self):
        super().setUp()
        (self.tmp / "EFMI" / "Mods").mkdir(parents=True)
        self.server = tools.build_server(self.tmp / "EFMI", self.tmp / "out", self.tmp / "probe.dll")
        patcher = patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("test: no probe"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # These tests must not start Blender to ask which add-on it has.
        patcher = patch.object(tools.Session, "loyaltools", return_value={
            "path": str(self.tmp / "LoyalTools"), "version": "0.0.0", "origin": "test"})
        self.loyaltools = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, **arguments):
        reply = self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        return reply["result"]["content"][0]["text"], reply["result"]["isError"]

    def test_model_tools_are_registered_with_their_side_effects_declared(self):
        listed = {t["name"]: t for t in self.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        for name in ("efmi_list_models", "efmi_extract_model", "efmi_map_lod", "efmi_export_mod",
                     "efmi_verify_exported_mod", "efmi_model_job"):
            self.assertFalse(listed[name]["annotations"]["readOnlyHint"], name)
        self.assertTrue(listed["efmi_model_project"]["annotations"]["readOnlyHint"])

    def test_relative_project_path_and_unknown_project_are_refused(self):
        text, error = self.call("efmi_export_mod", project_dir="relative/project")
        self.assertTrue(error and "absolute" in text, text)
        text, error = self.call("efmi_verify_exported_mod", project_dir=str(self.tmp / "nothing"), action="status")
        self.assertTrue(error and "not a model project" in text, text)

    def test_job_tool_without_a_job_says_so(self):
        text, error = self.call("efmi_model_job")
        self.assertTrue(error and "No model job" in text, text)

    def test_extract_without_the_game_fails_as_a_job_and_leaves_no_project(self):
        target = self.tmp / "new-project"
        text, error = self.call("efmi_extract_model", output_dir=str(target), import_blender=False, wait_s=20)
        self.assertFalse(error, text)
        job = json.loads(text)
        self.assertEqual(job["status"], "failed")
        self.assertIn("probe is not running", job["error"])
        self.assertFalse((target / "workplace").exists())

    def test_extract_builds_the_workspace_with_the_addon_of_the_blender_that_imports_it(self):
        self.call("efmi_extract_model", output_dir=str(self.tmp / "other-blender"),
                  blender_path="X:/other/blender.exe", wait_s=20)
        self.loyaltools.assert_called_with("X:/other/blender.exe")

    def test_verify_with_deploy_dir_outside_mods_is_refused_before_anything_runs(self):
        project = self.tmp / "proj"
        (project / "workplace").mkdir(parents=True)
        (project / "workplace" / "EFMI_MergedSkeleton.json").write_text("{}", encoding="utf-8")
        text, error = self.call("efmi_verify_exported_mod", project_dir=str(project), deploy_dir=str(self.tmp / "elsewhere"))
        self.assertTrue(error and "inside" in text, text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
