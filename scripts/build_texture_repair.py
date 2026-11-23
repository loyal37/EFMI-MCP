"""Build a portable Python script and optional standalone Windows executable."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import zlib
from efmi_texture_repair import make_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "dist/texture-repair/efmi_texture_repair.py")
    parser.add_argument("--exe", action="store_true", help="Also build a one-file Windows EXE using installed PyInstaller")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.exe and os.name != "nt":
        parser.error("Windows EXE packaging must run on Windows")
    table = json.loads(args.table.read_text(encoding="utf-8"))
    catalog = make_catalog(table)
    data = base64.b85encode(zlib.compress(json.dumps(catalog, separators=(",", ":")).encode(), 9)).decode()
    source = Path(__file__).with_name("efmi_texture_repair.py").read_text(encoding="utf-8")
    assert source.count('CATALOG_B85 = ""') == 1
    source = source.replace('CATALOG_B85 = ""', "CATALOG_B85 = " + repr(data))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source, encoding="utf-8")
    print(json.dumps(dict(output=str(args.output), game_version=catalog["game_version"],
                          known_meshes=len(catalog["meshes"]), eligible_meshes=sum(bool(v) for v in catalog["meshes"].values()),
                          roles=sum(map(len, catalog["meshes"].values())),
                          bytes=args.output.stat().st_size)))
    if args.exe:
        work = Path(__file__).resolve().parents[1] / "dist/texture-repair-build"
        work.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYINSTALLER_CONFIG_DIR"] = str(work / "cache")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--noupx",
                        "--clean", "--noconfirm", "--name", args.output.stem,
                        "--distpath", str(args.output.parent), "--workpath", str(work / "work"),
                        "--specpath", str(work), str(args.output)], check=True, env=env, cwd=work)
        exe = args.output.with_suffix(".exe")
        for name, mode in (("Preview.cmd", "--dry-run"), ("Restore.cmd", "--restore")):
            (exe.parent / name).write_bytes(("@echo off\r\n"
                + '"%~dp0' + exe.name + '" ' + mode + "\r\npause\r\n").encode("ascii"))
        print(json.dumps(dict(executable=str(exe), bytes=exe.stat().st_size)))


if __name__ == "__main__":
    main()
