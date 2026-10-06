"""Archive live EFMI inputs for LoyalTools' merged-skeleton extractor.

Command-line entry point; the collection itself lives in efmi_mcp.model_capture.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from efmi_mcp.model_capture import addressing, archive_sample, candidates, instance_config  # noqa: F401
from efmi_mcp.probe_client import ProbeClient


def archive(pid: int, output: Path, deferred_skeleton: bool = False):
    client = ProbeClient.connect(pid=pid)
    try:
        return archive_sample(client, output, deferred_skeleton)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--deferred-skeleton", action="store_true",
                        help="Collect static-editing inputs; independent samples must validate merged VG mappings")
    args = parser.parse_args()
    result = archive(args.pid, args.archive, args.deferred_skeleton)
    print(json.dumps({"frame": result["frame"], "draws": len(result["eligible_draws"]),
                      "buffers": len(result["buffers"]), "textures": len(result["textures"]),
                      "geometry": result["geometry_region_checks"],
                      "durations_ms": result["durations_ms"],
                      "skeleton_addresses_stable": result["skeleton_addressing"]["stable"]}, indent=2))
