"""Archive live EFMI inputs for LoyalTools' merged-skeleton extractor.

One archive is one captured frame: capture.json and cb-snapshots.bin (the
probe document with draw-time constant buffers), buffers/<resource-id>.buf
(whole geometry and skeleton buffers), textures/<resource-id>.dds, a
screenshot and readback.json. CBs are draw-time snapshots; every other
resource is read back later, each shared resource once, and skeleton
addressing is checked against a second capture.
"""
from __future__ import annotations

import hashlib
import json
import struct
import time
from pathlib import Path

from .capture_model import Capture
from .decode import make_dds, texture_to_png
from .hashes import crc32c, hex32


class CaptureCancelled(RuntimeError):
    """The caller asked to stop between two steps of a collection."""


def instance_config(cap: Capture, draw: dict):
    """The per-instance constants of a skinned character draw, or None."""
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
    """Game draws that look like skinned character components."""
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


def save_screenshot(client, cap: Capture, path: Path) -> bool:
    """Save the frame's final colour target as PNG. ``cap`` must be the probe's latest capture."""
    back = next((d["out"]["o0"]["res"] for d in reversed(list(cap.draws()))
                 if "o0" in d.get("out", {}) and
                 [cap.resource(d["out"]["o0"]["res"]).get(k) for k in ("w", "h")] == cap.resolution), None)
    if back is None:
        return False
    meta, data = client.read_texture(cap.frame, back)
    png = texture_to_png(meta["width"], meta["height"], meta["format"], data)
    if not png:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return True


def archive_sample(client, output: Path, deferred_skeleton: bool = False, on_stage=None) -> dict:
    """Capture one frame through an open probe connection and archive its inputs.

    The caller keeps and closes the connection; the capture's resources are
    released here. ``on_stage(name)`` is called as each step starts.
    """
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Archive output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "buffers").mkdir(exist_ok=True)
    (output / "textures").mkdir(exist_ok=True)
    report = {"buffers": {}, "textures": {}, "errors": [],
              "timing": "CB: draw snapshot; IB/VB/SRV/textures: later readback",
              "validation_scope": "deferred_skeleton_static" if deferred_skeleton else "unchanged_resources",
              "srv_view": "The probe does not record SRV view offsets; LoyalTools assumes full-buffer vs-t0"}
    started = last_stage = time.perf_counter()
    durations = report["durations_ms"] = {}

    def stage(name):
        nonlocal last_stage
        now = time.perf_counter()
        durations[name] = round((now - last_stage) * 1000, 3)
        last_stage = now

    def begin(name):
        if on_stage:
            on_stage(name)

    def save_capture(name, snapshot_name, document, snapshots):
        # Compact JSON uses the stdlib C encoder and retains every field. Large
        # draw documents do not need Python's much slower indented encoder.
        (output / name).write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        (output / snapshot_name).write_bytes(snapshots)

    try:
        begin("capture")
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
        begin("buffers")
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
        begin("textures")
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
        save_screenshot(client, cap, output / "current-screen.png")
        stage("screenshot_readback_and_png")
        begin("validation")
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
        except Exception:
            pass  # A broken pipe is reported by the failing step above.
    return report


def collect_samples(client, source_dir: Path, count: int = 3, interval_s: float = 0.5,
                    on_stage=None, cancelled=None) -> list[Path]:
    """Archive ``count`` independent frames as source_dir/sample-1..n.

    Static extraction compares these: the geometry has to be identical and the
    pose has to differ, so the samples are spaced in time.
    """
    source_dir = Path(source_dir)
    archives = []
    for number in range(1, count + 1):
        if cancelled and cancelled():
            raise CaptureCancelled("cancelled before sample " + str(number))
        if number > 1:
            time.sleep(interval_s)
        target = source_dir / f"sample-{number}"
        archive_sample(client, target, deferred_skeleton=True,
                       on_stage=(lambda step, n=number: on_stage(f"sample {n}/{count}: {step}")) if on_stage else None)
        archives.append(target)
    return archives
