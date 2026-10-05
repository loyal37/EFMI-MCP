"""Builds sandbox/run: a private copy of the XXMI runtime plus the test program.

The real EFMI folder is only ever read. Everything the sandbox changes (a few
d3dx.ini settings so the test can run unattended) is changed in the copy.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_EFMI = Path(r"D:\XXMI-Launcher-Portable-v2.1.6\EFMI")

# Hotkeys XXMI would otherwise react to system-wide once the foreground check is
# off. Only the frame-analysis key is kept, moved to F24 (0x87), a key no
# keyboard has, so the test cannot collide with whatever the user is typing. It
# is bound without modifier conditions: with "no_modifiers" a Shift or Ctrl the
# user happens to press would look like a second key press and abort the dump.
SILENCED_KEYS = re.compile(
    r"^(previous_|next_|mark_|done_hunting|take_screenshot|toggle_hunting|show_original|"
    r"monitor_performance|freeze_performance_monitor|wipe_user_config|reload_fixes|reload_config)\w*\s*=",
    re.I,
)


def patch_ini(text: str, exe_name: str) -> str:
    out = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].lower()
        elif stripped and not stripped.startswith(";"):
            key = stripped.split("=", 1)[0].strip().lower()
            if section == "loader" and key == "target":
                line = f"target = {exe_name}"
            elif section == "hunting":
                if key == "hunting":
                    line = "hunting = 1"
                elif key == "analyse_frame":
                    line = "analyse_frame = 0x87"
                elif key == "analyse_options":
                    line = "analyse_options = dump_rt dump_tex dump_cb dump_vb dump_ib buf txt"
                elif SILENCED_KEYS.match(stripped):
                    line = ";sandbox: " + line
            elif section == "system" and key == "check_foreground_window":
                line = "check_foreground_window = 0"
            elif section == "logging" and key == "calls":
                line = "calls = 1"
        out.append(line)
    return "\r\n".join(out) + "\r\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--efmi", type=Path, default=DEFAULT_EFMI, help="EFMI folder to copy the runtime from (read only)")
    ap.add_argument("--run", type=Path, default=HERE / "run")
    ap.add_argument("--with-mods", type=Path, default=None, help="optional folder copied into the sandbox Mods directory")
    args = ap.parse_args()

    exe = HERE / "build" / "efmi_sandbox.exe"
    if not exe.exists():
        print(f"missing {exe}; run sandbox\\build.bat first", file=sys.stderr)
        return 1
    for name in ("d3d11.dll", "d3dcompiler_47.dll", "d3dx.ini"):
        if not (args.efmi / name).exists():
            print(f"{args.efmi / name} not found", file=sys.stderr)
            return 1

    run: Path = args.run
    efmi_copy = run / "EFMI"
    # Empty the copy rather than removing the folder itself, which fails when a
    # shell happens to be standing in it.
    efmi_copy.mkdir(parents=True, exist_ok=True)
    for child in efmi_copy.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copy2(exe, run / exe.name)
    for name in ("d3d11.dll", "d3dcompiler_47.dll"):
        shutil.copy2(args.efmi / name, efmi_copy / name)
    shutil.copytree(args.efmi / "Core", efmi_copy / "Core")
    for sub in ("Mods", "ShaderFixes", "ShaderCache"):
        (efmi_copy / sub).mkdir()
    if args.with_mods:
        shutil.copytree(args.with_mods, efmi_copy / "Mods" / args.with_mods.name)

    raw = (args.efmi / "d3dx.ini").read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    (efmi_copy / "d3dx.ini").write_bytes(patch_ini(text, exe.name).encode("utf-8"))

    print(f"sandbox ready: {run}")
    print(f"  runtime copied from {args.efmi} (untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
