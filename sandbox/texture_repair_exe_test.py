"""Exercise the real standalone EXE with no Python on its child PATH."""
import argparse
import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import efmi_texture_repair as repair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path, help="The native table embedded when building this EXE")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/texture-repair/efmi_texture_repair.exe")
    args = parser.parse_args()
    catalog = repair.load_catalog(args.table)
    mesh, roles = next((k, v) for k, v in catalog["meshes"].items() if "diffuse" in v)
    ib, count = mesh.split(":")
    source = codecs.BOM_UTF8 + (f"; \u6d4b\u8bd5\r\n[TextureOverrideFixture]\r\nhash = {ib}\r\n"
             f"match_index_count = {count}\r\nps-t{roles['diffuse']['slot']} = ResourceRedDiffuse\r\n").encode("utf-8")
    with tempfile.TemporaryDirectory(dir=ROOT / "captures", prefix="texture-repair-exe-") as tmp:
        folder = Path(tmp)
        portable, cwd, unpack = folder / "portable \u6d4b\u8bd5", folder / "other cwd", folder / "unpack"
        for path in (portable, cwd, unpack):
            path.mkdir()
        exe = portable / args.exe.name
        shutil.copy2(args.exe, exe)
        mod = portable / "DISABLED_Fixture" / "mod.INI"
        mod.parent.mkdir()
        mod.write_bytes(source)
        decoy = cwd / "decoy.ini"
        decoy.write_bytes(source)
        env = os.environ.copy()
        windows = Path(os.environ["SystemRoot"])
        env.update(PATH=str(windows / "System32") + os.pathsep + str(windows), TEMP=str(unpack), TMP=str(unpack))
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)

        def run(name, *options, expected=0):
            report = folder / (name + ".json")
            result = subprocess.run([str(exe), *options, "--report", str(report)], cwd=cwd, env=env,
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
            assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
            assert decoy.read_bytes() == source, "Unrelated working directory must stay untouched"
            return json.loads(report.read_text())

        preview = run("preview", "--dry-run")
        assert preview["game_version"] == catalog["game_version"]
        assert preview["scanned"] == 1 and preview["changed_files"] == [str(mod)]
        assert mod.read_bytes() == source and not repair.receipt_paths(mod)[0].exists()
        converted = run("convert")
        assert converted["changed_files"] == [str(mod)]
        data = mod.read_bytes()
        assert b"ps-t[Filter_Component0_Diffuse]" in data and data.startswith(codecs.BOM_UTF8)
        assert repair.receipt_paths(mod)[0].read_bytes() == source
        assert run("repeat")["changed_files"] == [] and mod.read_bytes() == data
        restored = run("restore", "--restore")
        assert restored["restored"] == [str(mod)] and not restored["errors"]
        assert mod.read_bytes() == source
        run("convert-again")
        mod.write_bytes(mod.read_bytes() + b"; later edit\r\n")
        edited = mod.read_bytes()
        assert run("preserve-edits", "--restore", expected=1)["errors"]
        assert mod.read_bytes() == edited
        explicit = run("explicit-root", "--root", str(cwd), "--dry-run")
        assert explicit["changed_files"] == [str(decoy)]
        assert not list(unpack.iterdir()), "One-file extraction should clean up after exit"
        print("PASS standalone EXE: embedded catalog, EXE-folder default, Unicode/disabled INIs, preview, conversion, repeat, exact restore, later-edit protection, explicit root")
        print("PASS child PATH has no Python; other working directory unchanged; private extraction files cleaned up")


if __name__ == "__main__":
    main()
