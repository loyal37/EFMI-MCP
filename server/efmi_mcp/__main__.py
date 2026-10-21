"""Entry point: python -m efmi_mcp [--efmi-root PATH] [--out PATH]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .tools import build_server

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_EFMI = r"D:\XXMI-Launcher-Portable-v2.1.6\EFMI"


def main() -> int:
    ap = argparse.ArgumentParser(prog="efmi_mcp", description="MCP server for live XXMI data (EFMI, ZZMI)")
    ap.add_argument("--efmi-root", default=os.environ.get("EFMI_ROOT", DEFAULT_EFMI),
                    help="the XXMI game folder (EFMI, ZZMI; read only) used until a probe is connected: mods are "
                         "listed and hashes matched in the installation of the connected game")
    ap.add_argument("--out", default=os.environ.get("EFMI_MCP_OUT", str(PROJECT / "captures")),
                    help="where exported meshes, textures and shader listings are written")
    ap.add_argument("--probe-dll", default=str(PROJECT / "dist" / "efmi_probe.dll"),
                    help="path shown in the instructions for loading the probe")
    ap.add_argument("--loyaltools-root", default=os.environ.get("EFMI_LOYALTOOLS", ""),
                    help="LoyalTools source tree used (read only) for model extraction and LoD mapping")
    ap.add_argument("--blender", default=os.environ.get("EFMI_BLENDER", ""),
                    help="blender.exe that has the LoyalTools add-on installed")
    args = ap.parse_args()

    # stdout carries the protocol: anything else must go to stderr.
    server = build_server(Path(args.efmi_root), Path(args.out), Path(args.probe_dll),
                          Path(args.loyaltools_root) if args.loyaltools_root else None, args.blender)
    print(f"efmi-mcp: {len(server.tools)} tools, XXMI folder {args.efmi_root}", file=sys.stderr)
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
