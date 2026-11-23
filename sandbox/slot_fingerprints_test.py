"""Fingerprint cache provenance, exact binding joins, ambiguity and capture regression."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from efmi_mcp import slot_fingerprints as fp, slot_table
from slot_table_test import rec, tex, READS, PS_LIT, PS_LIT_COPY, PS_GB


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "captures")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.version = "1.0+test"
        (self.folder / "version.json").write_text(json.dumps({"key": self.version}))
        self.textures = [tex(16, "12345678"), tex(17, "23456789", "BC7_UNORM"), tex(18, "34567890", "BC5_UNORM")]
        records = [rec("abcdef12", 123, ps, self.textures + [{"slot": 5, "kind": "tex2d", "format": "R32_FLOAT"}])
                   for ps in (PS_LIT, PS_LIT_COPY)]
        records.append(rec("abcdef12", 123, PS_GB, [tex(0, "12345678")], rt=["a", "b", "c"]))
        (self.folder / "draws.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
        self.table = slot_table.build(self.folder, reader=READS.get)

    def entry(self, value="0000000000000000", source="xxmi_log", **kwargs):
        return dict(ib="abcdef12", index_count=123, ps=PS_LIT, slot=16, texture="12345678",
                    format="BC7_UNORM_SRGB", dhash=value, source=source, mip=None, **kwargs)

    def role(self, table=None):
        return next(slot_table.all_rows(table or self.table))[1]["main"]["roles"]["diffuse"]

    def dump(self, texture="12345678", count=123, ps=PS_LIT, computed=False):
        p = self.folder / "dump"
        p.mkdir(exist_ok=True)
        header = "dhash fields are recomputed from exported pixels\n" if computed else ""
        (p / "log.txt").write_text(header + "000001 PSSetShaderResources(StartSlot:16, NumViews:1)\n"
                                  f"       16: view=0x1 resource=0x2 hash={texture} dhash=0000000000000000\n"
                                  f"000001 DrawIndexed(IndexCount:{count}, StartIndexLocation:0, BaseVertexLocation:0)\n")
        (p / f"000001-ib=abcdef12-vs={'1'*16}-ps={ps}.buf").write_bytes(b"")
        return p

    def test_original_dump_zero_and_repeated_import(self):
        entries, report = fp.collect_dump(self.table, self.dump())
        self.assertEqual(report["matched"], 1)
        self.assertEqual(fp.save(self.folder, self.version, entries)["added"], 1)
        self.assertEqual(fp.save(self.folder, self.version, entries)["added"], 0)
        table = slot_table.build(self.folder, reader=READS.get)
        self.assertEqual(self.role(table)["dhash"], "0000000000000000")
        self.assertTrue(self.role(table)["xxmi_cached"])
        self.assertIn("d=0000000000000000 (xxmi_log)", slot_table.to_markdown(table))

    def test_join_rejects_replacement_wrong_mesh_count_and_shader(self):
        for args in ({"texture": "ffffffff"}, {"count": 124}, {"ps": "9"*16}):
            entries, report = fp.collect_dump(self.table, self.dump(**args))
            self.assertEqual(entries, [])
            self.assertEqual(report["unmatched"], 1)

    def test_computed_dump_never_promoted_to_original(self):
        entries, _ = fp.collect_dump(self.table, self.dump(computed=True))
        self.assertEqual(entries[0]["source"], "exported_pixels")
        fp.annotate(self.table, entries)
        self.assertFalse(self.role()["xxmi_cached"])

    def test_original_precedence_and_conflicting_variants(self):
        es = [self.entry(), self.entry("f"*16, "captured_pixels")]
        fp.annotate(self.table, es)
        self.assertEqual(self.role()["dhash"], "0"*16)
        second = self.entry("1"*16); second["ps"] = PS_LIT_COPY
        fp.annotate(self.table, es + [second])
        self.assertEqual(self.role()["dhash_status"], "ambiguous")
        self.assertIsNone(self.role()["dhash"])
        self.assertEqual(len(self.role()["dhashes"]), 3)

    def test_cross_pass_texture_identity_and_no_other_mesh_leak(self):
        e = self.entry(); e.update(ps=PS_GB, slot=0)
        fp.annotate(self.table, [e])
        self.assertEqual(self.role()["dhash"], "0"*16)
        e["ib"] = "99999999"
        fp.annotate(self.table, [e])
        self.assertIsNone(self.role()["dhash"])

    def test_version_and_lock_refusal_preserve_existing_cache(self):
        fp.save(self.folder, self.version, [self.entry()])
        before = (self.folder / fp.NAME).read_bytes()
        with self.assertRaises(ValueError):
            fp.save(self.folder, "wrong", [self.entry("1"*16)])
        lock = self.folder / (fp.NAME + ".lock"); lock.touch()
        with self.assertRaises(ValueError):
            fp.save(self.folder, self.version, [self.entry("1"*16)])
        self.assertEqual(before, (self.folder / fp.NAME).read_bytes())

    def test_cache_invalidation_and_role_invariance(self):
        with patch.object(slot_table, "shader_reader", return_value=READS.get):
            cache = slot_table.TableCache()
            before = cache.get(self.folder, None)
            fp.save(self.folder, self.version, [self.entry()])
            after = cache.get(self.folder, None)
        self.assertIsNot(before, after)
        self.assertEqual(self.role(after)["slot"], self.role(before)["slot"])
        self.assertEqual(self.role(after)["basis"], self.role(before)["basis"])
        self.assertEqual(after["counts"], before["counts"])

    def test_mcp_import_and_live_version_guard(self):
        from types import SimpleNamespace
        from efmi_mcp.mcp_stdio import Server, ToolError
        server = Server("test", "0")
        session = SimpleNamespace(efmi_root=None, call=lambda fn: {"slot_cache":{"version":"another-version"}})
        with patch.object(slot_table, "shader_reader", return_value=READS.get):
            slot_table.register(server, session)
            tool = server.tools["efmi_cache_texture_dhashes"]
            self.assertFalse(tool.read_only)
            result = json.loads(tool.fn(folder=str(self.folder), frame_dump=str(self.dump())))
            self.assertEqual(result["added"], 1)
            queried = server.tools["efmi_get_slot_table"].fn(folder=str(self.folder), ib_hash="abcdef12")
            self.assertEqual(queried["rows"][0]["main"]["roles"]["diffuse"]["dhash"], "0"*16)
            with self.assertRaises(ToolError):
                tool.fn(folder=str(self.folder))

    def test_capture_mip_selection_dedup_and_exactness(self):
        class Cap:
            frame = 42
            def draws(self):
                d = dict(index=1, origin="game", ib={"hash":"abcdef12"}, args={"index_count":123},
                         shaders={"ps":PS_LIT}, tex={"ps-t16":{"res":0, "hash":"12345678", "mip_contents":[0,1,2],
                                                                  "view":{"format":"BC7_UNORM_SRGB"}}})
                return [d, copy.deepcopy(d), dict(d, origin="xxmi")]
            def resource(self, _):
                return dict(origin="game", kind="tex2d", array=1, fmt_name="BC7_UNORM_SRGB", w=1024, h=1024, mips=3)
            def content(self, cid):
                return {"id":cid, "mip":cid} if cid is not None else None
        reads = []
        def read(cid):
            reads.append(cid)
            return {"exact":True, "width":256, "height":256}, b"pixels"
        with patch("efmi_mcp.dhash.compute", return_value="0"*16):
            es, report = fp.collect_capture(self.table, Cap(), read)
            self.assertEqual(reads, [2])
            self.assertEqual(report["matched"], 2)
            self.assertEqual(es[0]["mip"], 2)
            self.assertEqual(es[0]["source"], "captured_pixels")
            bad, r = fp.collect_capture(self.table, Cap(), lambda cid: ({"exact":False}, b""))
            self.assertFalse(bad)
            self.assertTrue(r["errors"])
            absent, r = fp.collect_capture(self.table, Cap(), read, mip=8)
            self.assertFalse(absent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
