"""Archive live EFMI inputs for LoyalTools' merged-skeleton extractor.

CBs are draw-time snapshots. Other resources are later readbacks; each shared
resource is copied once, and skeleton addressing is checked in another capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from efmi_mcp.capture_model import Capture
from efmi_mcp.decode import make_dds, texture_to_png
from efmi_mcp.hashes import crc32c, hex32
from efmi_mcp.probe_client import ProbeClient


def instance_config(cap: Capture, draw: dict):
    srv = cap.resource(draw.get("tex", {}).get("vs-t0", {}).get("res", -1))
    if not srv or srv.get("kind") != "buffer" or srv.get("stride") != 16:
        return None
    for slot, cb in draw.get("cb", {}).items():
        if not slot.startswith("vs-") or cb["num_constants"] != 4096:
            continue
        data = cap.snap_bytes(cb["snap"])
        if data is not None and len(data) >= 96:
            return {
                "slot": slot, "skeleton_resource": srv["id"],
                "skeleton_pointer": srv["ptr"],
                "instance": hashlib.blake2b(data[:64], digest_size=8).hexdigest(),
                "skeleton_offset": struct.unpack_from("<I", data, 80)[0],
                "gpu_posed": bool(struct.unpack_from("<i", data, 76)[0] & -17),
            }
    return None


def candidates(cap: Capture):
    return [(d, cfg) for d in cap.draws()
            if d["origin"] == "game" and d["type"] in (2, 4)
            and d.get("topology") == "trianglelist" and d.get("ib", {}).get("hash")
            and (cfg := instance_config(cap, d)) is not None]


def addressing(cap: Capture):
    result = {}
    for d, cfg in candidates(cap):
        if not cfg["gpu_posed"]:
            continue
        args = d["args"]
        key = ":".join(map(str, (d["ib"]["hash"], args["index_count"],
                                  args.get("first_index", 0), args.get("first_vertex", 0))))
        value = (cfg["instance"], cfg["skeleton_pointer"], cfg["skeleton_offset"])
        result.setdefault(key, set()).add(value)
    return {k: sorted(v) for k, v in result.items()}


def archive(pid: int, output: Path, deferred_skeleton: bool = False):
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Archive output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "buffers").mkdir(exist_ok=True)
    (output / "textures").mkdir(exist_ok=True)
    report = {"buffers": {}, "textures": {}, "errors": [],
              "timing": "CB: draw snapshot; IB/VB/SRV/textures: later readback",
              "validation_scope": "deferred_skeleton_static" if deferred_skeleton else "unchanged_resources",
              "srv_view": "Probe 0.1.1 lacks SRV view offsets; LoyalTools assumes full-buffer vs-t0"}
    started = last_stage = time.perf_counter()
    durations = report["durations_ms"] = {}

    def stage(name):
        nonlocal last_stage
        now = time.perf_counter()
        durations[name] = round((now - last_stage) * 1000, 3)
        last_stage = now

    def save_capture(name, snapshot_name, document, snapshots):
        # Compact JSON uses the stdlib C encoder and retains every field. Large
        # draw documents do not need Python's much slower indented encoder.
        (output / name).write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        (output / snapshot_name).write_bytes(snapshots)

    client = ProbeClient.connect(pid=pid)
    try:
        status = client.status()
        (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        stage("connect_status")
        doc, blob = client.capture(detail="full", cb_data=True, timeout_ms=8000)
        stage("capture")
        cap = Capture(doc, blob)
        save_capture("capture.json", "cb-snapshots.bin", doc, blob)
        pairs = candidates(cap)
        if not pairs:
            raise ValueError("No indexed character candidates with instance CB and structured vs-t0")
        report["frame"] = cap.frame
        report["eligible_draws"] = [d["index"] for d, _ in pairs]
        report["instances"] = [{"draw": d["index"], **cfg} for d, cfg in pairs]
        skeletons = {cfg["skeleton_resource"] for _, cfg in pairs}
        geometry = {d["ib"]["res"] for d, _ in pairs}
        geometry.update(v["res"] for d, _ in pairs for slot, v in d.get("vb", {}).items() if slot < 3)
        stage("save_capture_and_select")
        buffers = {}
        # Copy each shared skeleton pool first, in one request for all components.
        for rid in sorted(skeletons) + sorted(geometry - skeletons):
            resource = cap.resource(rid)
            meta, data = client.read_buffer(cap.frame, rid, size=resource["size"])
            if len(data) != resource["size"]:
                raise ValueError(f"Incomplete buffer {rid}")
            buffers[rid] = data
            (output / "buffers" / f"{rid}.buf").write_bytes(data)
            report["buffers"][str(rid)] = {"bytes": len(data), "meta": meta,
                "sha256": hashlib.sha256(data).hexdigest(), "skeleton": rid in skeletons}
            if rid in geometry and meta.get("unchanged_since_capture") is not True:
                raise ValueError(f"Geometry buffer {rid} has not been verified unchanged since capture")
        stage("buffer_readback_and_save")
        checks = {}
        for d, _ in pairs:
            bindings = [d["ib"], *(v for slot, v in d.get("vb", {}).items() if slot < 3)]
            for binding in bindings:
                region = binding.get("region")
                if not region or region["state"] != "ok" or not region["hash"]:
                    continue
                key = (region["res"], region["offset"], region["size"], region["hash"])
                if key in checks:
                    continue
                chunk = buffers[region["res"]][region["offset"]:region["offset"] + region["size"]]
                got = hex32(crc32c(chunk))
                checks[key] = got == region["hash"]
        report["geometry_region_checks"] = {"checked": len(checks), "matched": sum(checks.values()),
            "failures": [list(k) for k, ok in checks.items() if not ok]}
        if not all(checks.values()):
            raise ValueError("Geometry changed after capture; refusing mixed-frame extraction")
        stage("geometry_crc")
        tex_ids = set()
        for d, _ in pairs:
            for slot, binding in d.get("tex", {}).items():
                r = cap.resource(binding["res"])
                if (slot.startswith("ps-") and r and r.get("kind") == "tex2d"
                        and r.get("known") and r.get("w") == r.get("h") and r.get("w", 0) >= 256
                        and r.get("array", 1) == 1 and r.get("samples", 1) == 1):
                    tex_ids.add(r["id"])
        for rid in sorted(tex_ids):
            meta, data = client.read_texture(cap.frame, rid)
            dds = make_dds(meta["width"], meta["height"], meta["format"], data)
            if len(dds) < 256 * 1024:
                continue
            (output / "textures" / f"{rid}.dds").write_bytes(dds)
            report["textures"][str(rid)] = {"bytes": len(dds), "meta": meta,
                                           "sha256": hashlib.sha256(dds).hexdigest()}
        stage("texture_readback_and_save")
        back = next((d["out"]["o0"]["res"] for d in reversed(list(cap.draws()))
                     if "o0" in d.get("out", {}) and
                     [cap.resource(d["out"]["o0"]["res"]).get(k) for k in ("w", "h")] == cap.resolution), None)
        if back is not None:
            meta, data = client.read_texture(cap.frame, back)
            png = texture_to_png(meta["width"], meta["height"], meta["format"], data)
            if png:
                (output / "current-screen.png").write_bytes(png)
        stage("screenshot_readback_and_png")
        # This capture validates draw identity (IB hash + arguments), instance
        # CB and skeleton resource; VB hashes are already checked above.
        after_doc, after_blob = client.capture(detail="full", cb_data=True, hash_vb=False, timeout_ms=8000)
        stage("validation_capture")
        save_capture("validation-capture.json", "validation-cb-snapshots.bin", after_doc, after_blob)
        before_addressing = addressing(cap)
        after_addressing = addressing(Capture(after_doc, after_blob))
        report["skeleton_addressing"] = {
            "before": before_addressing, "after": after_addressing,
            "stable": before_addressing == after_addressing,
            "validation_frame": after_doc["frame"],
        }
        # Addresses may move in the game's shared pool. Static extraction can be
        # validated against independent samples in loyal_bridge; it must not be
        # mistaken for reconstructing the animation pose of this capture frame.
        identity = lambda a: {k: sorted({tuple(v[:2]) for v in values}) for k, values in a.items()}
        if identity(before_addressing) != identity(after_addressing):
            raise ValueError("Character identity or skeleton resource changed during resource reads")
        skeleton_intact = all(report["buffers"][str(rid)]["meta"].get("unchanged_since_capture") is True
                              for rid in skeletons)
        report["skeleton_unchanged_since_capture"] = skeleton_intact
        if not deferred_skeleton and not skeleton_intact:
            raise ValueError("Skeleton readback is not a verified draw-time snapshot; static extraction needs independent samples")
        report["complete"] = True
        stage("save_validation_and_check")
    except Exception as exc:
        report["errors"].append(str(exc))
        raise
    finally:
        durations["total_before_release"] = round((time.perf_counter() - started) * 1000, 3)
        (output / "readback.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        try:
            client.release()
        finally:
            client.close()
    return report


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
