"""Offline conversion, per-draw behavior, call graph and restore regressions."""
import codecs
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import efmi_texture_repair as repair


def catalog():
    return dict(schema=1, game_version="fixture", meshes={
        "12345678:300": {"diffuse": dict(slot=14, format="BC7_UNORM_SRGB", dhash="0123456789abcdef"),
                         "normal": dict(slot=15, format="BC5_UNORM", dhash="fedcba9876543210")},
        "aaaaaaaa:600": {"diffuse": dict(slot=16, format="BC7_UNORM_SRGB", dhash="1111222233334444")},
        "bbbbbbbb:150": {},
    })


def document(body, name="mod.ini"):
    return repair.Document(Path(name), body.encode("utf-8"))


ENTRY = "[TextureOverrideBody]\nhash = 12345678\nmatch_index_count = 300\n"


class RepairTests(unittest.TestCase):
    def test_frozen_default_root_is_beside_exe_not_extraction_folder(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            root = Path(tmp).resolve()
            path = root / "mod.ini"
            path.write_text(ENTRY + "ps-t14 = ResourceDiffuse\n")
            report = root / "preview.json"
            with patch.object(repair.sys, "frozen", True, create=True), \
                    patch.object(repair.sys, "executable", str(root / "repair.exe")), \
                    patch.object(repair, "load_catalog", return_value=catalog()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(repair.main(["--dry-run", "--report", str(report)]), 0)
            result = json.loads(report.read_text())
            self.assertEqual(result["scanned"], 1)
            self.assertEqual(result["changed_files"], [str(path)])
            self.assertFalse(repair.receipt_paths(path)[0].exists())

    def convert(self, body):
        doc = document(body)
        changes, reports = repair.plan([doc], catalog())
        self.assertIn(doc.path, changes, reports)
        text = changes[doc.path][1].decode()
        controls = lambda value: [line for line in value.splitlines() if re.match(r"\s*(?:if\s|elif\s|else\b|endif\b)", line)]
        self.assertEqual(controls(text), controls(body))
        original_runs = [line.strip() for line in body.splitlines() if re.match(r"\s*(?:post\s+)?run\s*=", line)]
        result_runs = [line.strip() for line in text.splitlines() if re.match(r"\s*(?:post\s+)?run\s*=", line)]
        standard = {"run = SnapshotInitialTextures", "run = RestoreInitialTextures", "post run = RestoreInitialTextures"}
        remaining = iter(original_runs)
        self.assertTrue(all(any(old == line for old in remaining) for line in result_runs if line not in standard))
        old_names = {s.name for s in doc.sections}
        self.assertTrue(all(s.name in old_names or s.name.startswith("Filter_") for s in document(text).sections))
        self.assertNotIn("Resource_EFMIRepair_", text)
        self.assertNotIn("CommandList_EFMIRepair_", text)
        return text, reports

    def test_fixed_multidraw_preserves_resources_and_draw_order(self):
        text, reports = self.convert(ENTRY + "handling = skip\nps-t14 = ResourceRedDiffuse\ndrawindexed = 30,0,0\n"
                               "if $variant == 1\nps-t14 = ResourceBlueDiffuse\ndrawindexed = 60,30,0\nendif\n")
        self.assertIn("ps-t[Filter_Component0_Diffuse] = ref ResourceRedDiffuse", text)
        self.assertIn("ps-t[Filter_Component0_Diffuse] = ref ResourceBlueDiffuse", text)
        self.assertLess(text.index("ResourceRedDiffuse"), text.index("drawindexed = 30"))
        self.assertLess(text.index("ResourceBlueDiffuse"), text.index("drawindexed = 60"))
        self.assertEqual(text.count("run = SnapshotInitialTextures"), 1)
        self.assertNotIn("post run = RestoreInitialTextures", text)
        self.assertEqual(reports[0]["material_restores"], 2)
        self.assertIn("drawindexed = 60,30,0\nrun = RestoreInitialTextures\nendif", text)

    def test_texture_only_restores_in_post_phase_and_handles_eof(self):
        text, _ = self.convert(ENTRY + "ps-t14 = ResourceDiffuse")
        self.assertIn("ps-t[Filter_Component0_Diffuse] = ref ResourceDiffuse\n\npost run = RestoreInitialTextures", text)
        self.assertEqual(text.count("run = SnapshotInitialTextures"), 1)

    def test_efmittl_multiple_consumers_preserve_branch_and_draw_api(self):
        body = ENTRY + r"""Resource\EFMITTL\Diffuse = ref ResourceRed
run = CommandList\EFMITTL\SetTextures
drawindexed = 30,0,0
if $color == 1
    Resource\EFMITTL\Diffuse = ref ResourceBlue
else
    Resource\EFMITTL\Diffuse = null
endif
Resource\EFMITTL\Mask = ref ResourceMask
run = CommandList\EFMITTL\SetTextures
$\EFMITTL\count = 60
run = CommandList\EFMITTL\Draw
"""
        text, _ = self.convert(body)
        self.assertEqual(text.count("ps-t[Filter_Component0_Diffuse]"), 2)
        self.assertIn("    ps-t[Filter_Component0_Diffuse] = ref ResourceBlue", text)
        self.assertIn(r"Resource\EFMITTL\Mask = ref ResourceMask", text)
        self.assertIn(r"run = CommandList\EFMITTL\Draw", text)
        self.assertNotIn(r"run = CommandList\EFMITTL\SetTextures", text)
        self.assertNotIn("if @", text)
        self.assertNotIn(" = null", text)

    def test_rabbitfx_auxiliary_and_effect_run_survive(self):
        body = ENTRY + r"""Resource\RabbitFx\Diffuse = ref ResourceRed
Resource\RabbitFx\DiscardMap = ref ResourceCutout
Resource\RabbitFx\Rainmap = ref ResourceRain
run = CommandList\RabbitFx\SetTextures
Resource\RabbitFx\FXMap = ref ResourceFX
run = CommandList\RabbitFx\Run
drawindexed = 300,0,0
"""
        text, _ = self.convert(body)
        self.assertIn(r"run = CommandList\RabbitFx\SetTextures", text)
        self.assertIn(r"run = CommandList\RabbitFx\Run", text)
        self.assertIn(r"Resource\RabbitFx\DiscardMap = ref ResourceCutout", text)
        self.assertNotIn(r"Resource\RabbitFX\Diffuse = null", text)
        self.assertIn("ps-t[Filter_Component0_Diffuse]", text)

    def test_rabbitfx_basic_only_removes_set_not_run(self):
        text, _ = self.convert(ENTRY + r"""Resource\RabbitFX\Diffuse = ref ResourceRed
run = CommandList\RabbitFX\SetTextures
run = CommandList\RabbitFX\Run
""")
        self.assertNotIn(r"run = CommandList\RabbitFX\SetTextures", text)
        self.assertIn(r"run = CommandList\RabbitFX\Run", text)

    def test_nested_run_and_callback_main_lod_context(self):
        body = r"""[TextureOverride_EntryPoint_Component7]
hash = 12345678
match_index_count = 300
CommandList\EFMIv1\Callback_Component_DrawCustom = ref CommandListDraw
run = CommandListInstances
[TextureOverride_EntryPoint_Component7_LOD1]
hash = bbbbbbbb
match_index_count = 150
CommandList\EFMIv1\Callback_Component_DrawCustom = ref CommandListDraw
run = CommandListInstances
[CommandListInstances]
run = CommandList\EFMIv1\Component_DrawInstances
[CommandListDraw]
run = CommandList\EFMIv1\OverrideTextures
run = CommandListMaterial
drawindexed = 300,0,0
[CommandListMaterial]
ps-t14 = ResourceDiffuse
"""
        text, reports = self.convert(body)
        self.assertIn("Filter_Component7_Diffuse", text)
        self.assertEqual(text.count("run = SnapshotInitialTextures"), 1)
        self.assertEqual(text.count("run = RestoreInitialTextures"), 1)
        self.assertIn("[CommandListDraw]\nrun = SnapshotInitialTextures\nrun = CommandList\\EFMIv1\\OverrideTextures", text)
        self.assertIn("drawindexed = 300,0,0\n\nrun = RestoreInitialTextures", text)
        self.assertNotIn("post run = RestoreInitialTextures", text)
        material = next(s for s in document(text).sections if s.name == "CommandListMaterial")
        self.assertFalse(any(v == "RestoreInitialTextures" for _, _, v in material.commands()))
        self.assertEqual(len([r for r in reports if r["status"] == "converted"]), 1)
        self.assertIn(r"run = CommandList\EFMIv1\OverrideTextures", text)

    def test_same_namespace_cross_file_run(self):
        a = document("namespace = Example\n" + ENTRY + "run = CommandListPaint\n", "a.ini")
        b = document("namespace = Example\n[CommandListPaint]\nps-t14 = ResourceDiffuse\n", "b.ini")
        changes, reports = repair.plan([a, b], catalog())
        self.assertEqual(len(changes), 2, reports)
        self.assertIn(b"ps-t[Filter_Component0_Diffuse]", changes[b.path][1])

    def test_shared_helper_different_ib_is_not_guessed(self):
        body = ENTRY + "run = CommandListPaint\n[TextureOverrideOther]\nhash = aaaaaaaa\nmatch_index_count = 600\nrun = CommandListPaint\n[CommandListPaint]\nps-t14 = ResourceDiffuse\n"
        changes, reports = repair.plan([document(body)], catalog())
        self.assertFalse(changes)
        self.assertTrue(all(r["status"] == "skipped" for r in reports))

    def test_unknown_present_caller_blocks_shared_material(self):
        body = ENTRY + "run = CommandListPaint\n[Present]\nrun = CommandListPaint\n[CommandListPaint]\nps-t14 = ResourceDiffuse\n"
        self.assertFalse(repair.plan([document(body)], catalog())[0])

    def test_interface_inputs_must_be_consumed_on_every_branch(self):
        assign = "Resource\\EFMITTL\\Diffuse = ref ResourceRed\n"
        consume = "run = CommandList\\EFMITTL\\SetTextures\n"
        for tail in ("if $choice\n" + consume + "endif\n",
                     "if $choice\n" + consume + "else\n" + assign + "endif\n"):
            self.assertFalse(repair.plan([document(ENTRY + assign + tail)], catalog())[0])
        self.convert(ENTRY + assign + "if $choice\n" + consume + "else\n" + consume + "endif\n")
        self.convert(ENTRY + "if $choice\n" + assign + consume + "endif\n")

    def test_unrelated_interface_consumption_and_leak(self):
        assign = "Resource\\EFMITTL\\Diffuse = ref ResourceRed\n"
        consume = "run = CommandList\\EFMITTL\\SetTextures\n"
        body = ENTRY + assign + consume + "[Present]\nif $choice\n" + assign
        self.convert(body + consume + "endif\n")
        self.assertFalse(repair.plan([document(body + "endif\n")], catalog())[0])

    def test_shared_interface_condition_is_not_specialized(self):
        body = ENTRY + "Resource\\EFMITTL\\Diffuse = ref ResourceRed\nrun = CommandListCheck\n"
        body += "run = CommandList\\EFMITTL\\SetTextures\n[Present]\nrun = CommandListCheck\n"
        body += "[CommandListCheck]\nif @Resource\\EFMITTL\\Diffuse\n$seen = 1\nendif\n"
        self.assertFalse(repair.plan([document(body)], catalog())[0])

    def test_existing_snapshot_lifecycle_is_preserved(self):
        text, _ = self.convert(ENTRY + "run = SnapshotInitialTextures\nps-t14 = ResourceDiffuse\npost run = RestoreInitialTextures\n")
        self.assertEqual(text.count("run = SnapshotInitialTextures"), 1)
        self.assertEqual(text.count("post run = RestoreInitialTextures"), 1)

    def test_incomplete_standard_lifecycle_adds_only_missing_half(self):
        for before, after in (("run = SnapshotInitialTextures\n", ""), ("", "post run = RestoreInitialTextures\n")):
            text, _ = self.convert(ENTRY + before + "ps-t14 = ResourceDiffuse\n" + after)
            self.assertEqual(text.count("run = SnapshotInitialTextures"), 1)
            self.assertEqual(text.count("post run = RestoreInitialTextures"), 1)

    def test_conditional_or_misplaced_existing_lifecycle_is_skipped(self):
        for body in (ENTRY + "ps-t14 = ResourceDiffuse\nrun = SnapshotInitialTextures\n",
                     ENTRY + "ps-t14 = ResourceDiffuse\nrun = RestoreInitialTextures\n",
                     ENTRY + "if $enabled\nrun = SnapshotInitialTextures\nps-t14 = ResourceDiffuse\nendif\n"):
            self.assertFalse(repair.plan([document(body)], catalog())[0])

    def test_marker_only_root_does_not_get_lifecycle_calls(self):
        text, _ = self.convert(ENTRY + "ps-t14 = ResourceDiffuse\n[TextureOverrideMarker]\nhash = 12345678\nfilter_index = 1\n")
        marker = next(s for s in document(text).sections if s.name == "TextureOverrideMarker")
        self.assertFalse(any(k == "run" or k == "post run" for _, k, _ in marker.commands()))

    def test_direct_input_cancellation_and_delayed_draw_are_skipped(self):
        assign = "Resource\\EFMITTL\\Diffuse = ref ResourceRed\n"
        consume = "run = CommandList\\EFMITTL\\SetTextures\n"
        for middle in ("Resource\\EFMITTL\\Diffuse = null\n", "drawindexed = 30,0,0\n",
                       "Resource\\EFMITTL\\Diffuse = ref ResourceBlue\n"):
            self.assertFalse(repair.plan([document(ENTRY + assign + middle + consume)], catalog())[0])

    def test_interface_condition_is_not_rewritten_even_with_single_owner(self):
        body = ENTRY + "Resource\\EFMITTL\\Diffuse = ref ResourceRed\nif @Resource\\EFMITTL\\Diffuse\n$seen = 1\nendif\n"
        body += "run = CommandList\\EFMITTL\\SetTextures\n"
        self.assertFalse(repair.plan([document(body)], catalog())[0])

    def test_old_version_marker_requires_restore(self):
        body = "; EFMI texture repair 1.0.0; game fixture\n" + ENTRY + "ps-t14 = ResourceDiffuse\n"
        changes, report = repair.plan([document(body)], catalog())
        self.assertFalse(changes)
        self.assertIn("restore", report[0]["reason"])

    def test_recursive_or_unresolved_run_skipped(self):
        for tail in ("run = CommandListMissing\n", "run = CommandListLoop\n[CommandListLoop]\nrun = CommandListLoop\n"):
            changes, _ = repair.plan([document(ENTRY + "ps-t14 = ResourceDiffuse\n" + tail)], catalog())
            self.assertFalse(changes)

    def test_post_phase_helper_is_not_treated_as_pre_draw_material(self):
        body = ENTRY + "post run = CommandListPaint\n[CommandListPaint]\nps-t14 = ResourceDiffuse\n"
        changes, reports = repair.plan([document(body)], catalog())
        self.assertFalse(changes)
        self.assertIn("Post-phase", reports[0]["reason"])

    def test_role_ambiguity_does_not_partially_convert_interface(self):
        body = ENTRY + r"""Resource\EFMITTL\Diffuse = ref ResourceA
Resource\EFMITTL\LightMap = ref ResourceB
run = CommandList\EFMITTL\SetTextures
"""
        changes, report = repair.plan([document(body)], catalog())
        self.assertFalse(changes)
        self.assertIn("lightmap", report[0]["reason"])

    def test_existing_filter_conflict_skipped(self):
        body = ENTRY.replace("TextureOverrideBody", "TextureOverride_Component0") + "ps-t14 = ResourceDiffuse\n[Filter_Component0_Diffuse]\nmatch_format = BC5_UNORM\nmatch_dhash = 0000000000000000\n"
        self.assertFalse(repair.plan([document(body)], catalog())[0])
        body = body.replace("BC5_UNORM", "BC7_UNORM_SRGB").replace("0000000000000000", "0123456789abcdef")
        self.assertFalse(repair.plan([document(body + "match_width = 32\n")], catalog())[0])

    def test_preserve_encoding_bom_and_crlf(self):
        source = (ENTRY + "; \u6d4b\u8bd5\nps-t14 = ResourceDiffuse\n").replace("\n", "\r\n")
        for encoding, bom in [("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("gb18030", b"")]:
            doc = repair.Document(Path("mod.ini"), bom + source.encode(encoding))
            changes, _ = repair.plan([doc], catalog())
            data = changes[doc.path][1]
            self.assertTrue(data.startswith(bom))
            decoded = data[len(bom):].decode(encoding)
            self.assertIn("; \u6d4b\u8bd5\r\n", decoded)
            self.assertNotIn("\n", decoded.replace("\r\n", ""))

    def test_idempotent_and_backup_restore_exact(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            path = Path(tmp) / "mod.ini"
            raw = codecs.BOM_UTF8 + (ENTRY + "ps-t14 = ResourceDiffuse\n").encode()
            path.write_bytes(raw)
            changes, _ = repair.plan([repair.Document(path, raw)], catalog())
            repair.apply_changes(changes, "fixture")
            backup, receipt = repair.receipt_paths(path)
            self.assertEqual(backup.read_bytes(), raw)
            self.assertFalse(backup.name.endswith(".ini"))
            self.assertFalse(repair.plan([repair.Document(path, path.read_bytes())], catalog())[0])
            restored, errors = repair.restore(Path(tmp))
            self.assertEqual(restored, [str(path)])
            self.assertFalse(errors)
            self.assertEqual(path.read_bytes(), raw)
            repair.apply_changes(changes, "fixture")
            self.assertEqual(backup.read_bytes(), raw)
            path.write_bytes(path.read_bytes() + b"; later user edit\n")
            self.assertTrue(repair.restore(Path(tmp))[1])
            self.assertTrue(path.read_bytes().endswith(b"; later user edit\n"))

    def test_transaction_failure_rolls_back_already_written_ini(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            a, b = Path(tmp) / "a.ini", Path(tmp) / "b.ini"
            for p in (a, b):
                p.write_bytes(b"original")
            real = repair.atomic_write
            def failing(path, data):
                if path == b and data == b"converted":
                    raise OSError("simulated failure")
                real(path, data)
            with patch.object(repair, "atomic_write", side_effect=failing):
                with self.assertRaises(OSError):
                    repair.apply_changes({a: (b"original", b"converted"), b: (b"original", b"converted")}, "fixture")
            self.assertEqual(a.read_bytes(), b"original")
            self.assertEqual(b.read_bytes(), b"original")
            self.assertFalse(repair.restore(Path(tmp))[1])

    def test_scan_disabled_and_uppercase_but_not_backup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            root = Path(tmp)
            sub = root / "DISABLED_Example"
            sub.mkdir()
            for p in [root / "a.ini", sub / "b.INI", sub / "b.INI.efmi-texture-repair.bak"]:
                p.write_bytes(b"")
            self.assertEqual(len(list(repair.walk_files(root, ".ini"))), 2)

    def test_report_validation_precedes_any_mutation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            root = Path(tmp)
            source = root / "mod.ini"
            source.write_text(ENTRY + "ps-t14 = ResourceDiffuse\n")
            cat = root / "catalog.json"
            cat.write_text(json.dumps(catalog()))
            before = source.read_bytes()
            for destination in (source, cat, repair.receipt_paths(source)[1]):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = repair.main(["--root", str(root), "--catalog", str(cat), "--report", str(destination)])
                self.assertEqual(code, 1)
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse(repair.receipt_paths(source)[0].exists())

    def test_catalog_rejects_invalid_material_definitions(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as tmp:
            path = Path(tmp) / "catalog.json"
            for field, value in (("slot", -1), ("dhash", "0123456789abcdef\n[Present]"),
                                 ("format", "BC7_UNORM\nrun = bad")):
                data = catalog()
                data["meshes"]["12345678:300"]["diffuse"][field] = value
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    repair.load_catalog(path)

    def test_catalog_all_groups_not_mod_whitelist(self):
        def row(ib, basis="dataflow", hashes=("0123456789abcdef",)):
            return dict(ib=ib, index_count=300, main=dict(roles=dict(diffuse=dict(slot=14, basis=basis,
                dhashes=[dict(format="BC7_UNORM_SRGB", dhash=h) for h in hashes]))))
        table = dict(version="fixture", characters=[], probable_groups=[dict(meshes=[row("12345678")])],
                     scenes=[dict(meshes=[row("aaaaaaaa")])], unassigned=[row("bbbbbbbb", "order"),
                         row("cccccccc", hashes=("0123456789abcdef", "1111111111111111"))])
        result = repair.make_catalog(table)
        self.assertIn("diffuse", result["meshes"]["12345678:300"])
        self.assertIn("diffuse", result["meshes"]["aaaaaaaa:300"])
        self.assertEqual(result["meshes"]["bbbbbbbb:300"], {})
        self.assertEqual(result["meshes"]["cccccccc:300"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
