"""Archive live EFMI inputs for LoyalTools' merged-skeleton extractor.

One archive is one captured frame: capture.json and cb-snapshots.bin (the
probe document with draw-time constant buffers), buffers/<resource-id>.buf
(whole geometry and skeleton buffers), textures/<resource-id>.dds, a
screenshot and readback.json. CBs are draw-time snapshots; every other
resource is read back later, each shared resource once, and skeleton
addressing is checked against a second capture.

Vertex streams that the game itself rewrites every few frames (the positions
of a CPU-posed component) are the exception: see _read_volatile.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import time
from pathlib import Path

from .capture_model import Capture
from .decode import make_dds, texture_to_png
from .hashes import crc32c, hex32


DYNAMIC = 2       # D3D11_USAGE_DYNAMIC
ATTEMPTS = 6      # per archive, when rewritten vertex data was lost to the game's next write


class CaptureCancelled(RuntimeError):
    """The caller asked to stop between two steps of a collection."""


class VolatileGeometry(ValueError):
    """Vertex data the game keeps rewriting was overwritten before it was read; another frame may succeed."""


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


def select_draws(cap: Capture):
    """The draws worth archiving, and what was left out of them.

    A model that can be extracted has at least one mesh skinned on the GPU.
    Draws that share their per-object constants with no such mesh are scenery:
    in the open world several hundred props, and the game's dynamic batches,
    whose index data is rewritten every frame. Of an object that does have
    one, a draw whose index data the game rewrites cannot be read back later;
    it is left out and listed, so the object is not taken for complete.
    """
    pairs = candidates(cap)
    skinned = {cfg["instance"] for _, cfg in pairs if cfg["gpu_posed"]}
    kept, scenery, rewritten = [], [], []
    for d, cfg in pairs:
        if cfg["instance"] not in skinned:
            scenery.append(cfg["instance"])
        elif cap.resource(d["ib"]["res"]).get("usage") == DYNAMIC:
            rewritten.append({"draw": d["index"], "instance": cfg["instance"], "ib_hash": d["ib"]["hash"],
                              "index_count": d["args"].get("index_count")})
        else:
            kept.append((d, cfg))
    return kept, {"draws_of_objects_without_gpu_skinning": len(scenery),
                  "objects_without_gpu_skinning": len(set(scenery)),
                  "rewritten_index_data": rewritten}


def skeleton_views(pairs) -> dict:
    """The views through which the archived draws read their skeleton pool, as the capture recorded them.

    LoyalTools applies a mesh's bone offset to the whole vs-t0 buffer. That is what the game's shader does
    as long as the view starts at the buffer's first element, which this states rather than assumes.
    """
    views = [d["tex"]["vs-t0"].get("view") for d, _ in pairs]
    if not all(views):
        return {"recorded": False,
                "note": "This capture has no view parameters (a probe older than 0.1.5); LoyalTools reads vs-t0 "
                        "from the buffer's first element."}
    firsts = sorted({view.get("first_element", 0) for view in views})
    return {"recorded": True, "first_elements": firsts, "starts_at_buffer_start": firsts == [0],
            "note": "LoyalTools reads vs-t0 from the buffer's first element"
                    + ("." if firsts == [0] else "; these views start elsewhere, which would shift every bone offset.")}


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


def _index_bounds(data: bytes, binding: dict, args: dict) -> tuple[int, int]:
    """Lowest and highest vertex index a draw uses."""
    width = 2 if binding["format"] == 57 else 4      # DXGI_FORMAT_R16_UINT, otherwise R32_UINT
    start = binding["offset"] + args.get("first_index", 0) * width
    values = memoryview(data)[start:start + args["index_count"] * width].cast("H" if width == 2 else "I")
    return min(values), max(values)


def _read_volatile(client, cap: Capture, pairs, volatile: set, buffers: dict, output: Path, report: dict) -> dict:
    """Vertex streams that live in buffers the game rewrites every few frames.

    The game keeps such data (the positions of a CPU-posed component) in a ring
    of dynamic buffers: a range stays as it was drawn for only a few frames,
    too short for a read after the full capture. So one more, light capture is
    taken and the ranges are read at once: one request per buffer, because
    with several characters in view a second request would already come too
    late. The probe's write history and that capture's draw-time hash prove
    that the bytes are what its draw read. They are the same vertices in the
    pose of that later frame, and are stored where the main frame bound them;
    the rest of such a buffer is left zero.
    """
    wanted = {}     # (draw identity, slot) -> where the main frame bound it and the bytes the extractor may read
    for d, _ in pairs:
        args = d["args"]
        base = args.get("first_vertex", 0)
        identity = (d["ib"]["hash"], args["index_count"], args.get("first_index", 0), base)
        for slot, binding in d.get("vb", {}).items():
            if slot >= 3 or binding["res"] not in volatile or (identity, slot) in wanted:
                continue
            low, high = _index_bounds(buffers[d["ib"]["res"]], d["ib"], args)
            first = low * binding["stride"]      # relative to the binding offset
            wanted[(identity, slot)] = {"res": binding["res"], "offset": binding["offset"], "stride": binding["stride"],
                                        "first": first, "end": first + (base + high - low + 1) * binding["stride"]}

    doc, blob = client.capture(detail="light", cb_data=False, timeout_ms=8000)
    light = Capture(doc, blob)
    drawn = {}
    for d in light.draws():
        if d["origin"] == "game" and d.get("ib", {}).get("hash"):
            args = d["args"]
            drawn.setdefault((d["ib"]["hash"], args.get("index_count"), args.get("first_index", 0),
                              args.get("first_vertex", 0)), d)
    plan, spans = [], {}
    for (identity, slot), want in wanted.items():
        binding = drawn.get(identity, {}).get("vb", {}).get(slot)
        region = (binding or {}).get("region")
        if (not binding or binding["stride"] != want["stride"] or not region or region["state"] != "ok"
                or not region["hash"] or light.resource(binding["res"]).get("usage") != DYNAMIC):
            raise VolatileGeometry(f"The mesh with ib {identity[0]} was not drawn the same way in the frame taken "
                                   "for its rewritten vertex data")
        start = min(region["offset"], binding["offset"] + want["first"])
        end = max(region["offset"] + region["size"], binding["offset"] + want["end"])
        plan.append((identity, slot, want, binding, region, start, end))
        low, high = spans.get(binding["res"], (start, end))
        spans[binding["res"]] = (min(low, start), max(high, end))
    reads, slots = {}, []
    for rid, (low, high) in spans.items():
        meta, data = client.read_buffer(light.frame, rid, offset=low, size=high - low)
        if len(data) != high - low or meta.get("unchanged_since_capture") is not True:
            raise VolatileGeometry(f"Vertex data in buffer {rid} was rewritten by the game "
                                   f"{meta.get('read_frame', light.frame) - light.frame} frame(s) after the capture, "
                                   "before it could be read")
        reads[rid] = (meta, data, low)
    for identity, slot, want, binding, region, start, end in plan:
        meta, whole, low = reads[binding["res"]]
        data = whole[start - low:end - low]
        hashed = data[region["offset"] - start:region["offset"] - start + region["size"]]
        if hex32(crc32c(hashed)) != region["hash"]:
            raise VolatileGeometry(f"Vertex data of ib {identity[0]} (vb{slot}) is not what its draw read in the "
                                   "frame taken for it")
        want.update(data=data, shift=start - binding["offset"])
        slots.append({"ib": identity[0], "index_count": identity[1], "slot": slot, "bytes": len(data),
                      "stride": want["stride"], "read_frame": meta.get("read_frame"),
                      "draw_time_hash": region["hash"], "hashed_bytes": region["size"]})

    sparse = {rid: bytearray(cap.resource(rid)["size"]) for rid in volatile}
    ranges = {rid: [] for rid in volatile}
    for (identity, slot), want in wanted.items():
        at, data = want["offset"] + want["shift"], want["data"]
        if at < 0 or at + len(data) > len(sparse[want["res"]]):
            raise ValueError(f"Rewritten vertex data of ib {identity[0]} does not fit where the main frame bound it")
        sparse[want["res"]][at:at + len(data)] = data
        ranges[want["res"]].append([at, len(data)])
    for rid, data in sparse.items():
        data = bytes(data)
        buffers[rid] = data
        (output / "buffers" / f"{rid}.buf").write_bytes(data)
        report["buffers"][str(rid)] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "skeleton": False,
                                       "volatile": True, "ranges": sorted(ranges[rid]),
                                       "meta": {"captured_frame": light.frame, "ranges_identical_to_draw_time": True}}
    return {"frame": light.frame, "main_frame": cap.frame, "slots": slots,
            "note": "These vertex streams are rewritten by the game every few frames (CPU-posed components). Their bytes "
                    "are what the same draw read in the frame given here, not in the main frame: the same vertices in "
                    "a pose a few frames later."}


def archive_sample(client, output: Path, deferred_skeleton: bool = False, on_stage=None) -> dict:
    """Capture one frame through an open probe connection and archive its inputs.

    The caller keeps and closes the connection; the capture's resources are
    released here. ``on_stage(name)`` is called as each step starts. When
    vertex data the game rewrites was lost to its next write, the archive is
    started over with a new frame, a few times.
    """
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Archive output must be a new or empty directory")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _archive_once(client, output, deferred_skeleton, on_stage, attempt)
        except VolatileGeometry:
            if attempt == ATTEMPTS:
                raise
            if output.exists():
                shutil.rmtree(output)   # only what this call wrote: the folder was new or empty
            if on_stage:
                on_stage(f"rewritten vertex data changed before it was read; attempt {attempt + 1} of {ATTEMPTS}")


def _archive_once(client, output: Path, deferred_skeleton: bool, on_stage, attempt: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    (output / "buffers").mkdir(exist_ok=True)
    (output / "textures").mkdir(exist_ok=True)
    report = {"buffers": {}, "textures": {}, "errors": [], "attempt": attempt,
              "timing": "CB: draw snapshot; IB/VB/SRV/textures: later readback",
              "validation_scope": "deferred_skeleton_static" if deferred_skeleton else "unchanged_resources",
              "srv_view": None}
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
        pairs, left_out = select_draws(cap)
        report["left_out"] = left_out
        if not pairs:
            scenery = left_out["draws_of_objects_without_gpu_skinning"]
            raise ValueError("No indexed character candidates with instance CB and structured vs-t0"
                             + (f" ({scenery} draws belong to objects without a mesh skinned on the GPU)" if scenery else ""))
        report["frame"] = cap.frame
        report["srv_view"] = skeleton_views(pairs)
        report["eligible_draws"] = [d["index"] for d, _ in pairs]
        report["instances"] = [{"draw": d["index"], **cfg} for d, cfg in pairs]
        skeletons = {cfg["skeleton_resource"] for _, cfg in pairs}
        geometry = {d["ib"]["res"] for d, _ in pairs}
        geometry.update(v["res"] for d, _ in pairs for slot, v in d.get("vb", {}).items() if slot < 3)
        # Buffers the game rewrites cannot be read whole or this late; their
        # vertex ranges are taken separately at the end (_read_volatile). No
        # index data is among them: select_draws left those draws out.
        volatile = {rid for rid in geometry if cap.resource(rid).get("usage") == DYNAMIC}
        stage("save_capture_and_select")
        begin("buffers")
        buffers = {}
        # Copy each shared skeleton pool first, in one request for all components.
        for rid in sorted(skeletons) + sorted(geometry - skeletons - volatile):
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
                if not region or region["state"] != "ok" or not region["hash"] or region["res"] in volatile:
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
        # A mesh has to keep its skeleton pool while its buffers are read. Its
        # instance constants may differ: they start with the world matrix, and
        # things in view move. Meshes that come into or leave the view are no
        # concern of the ones that were captured.
        pools = lambda a: {k: sorted({v[1] for v in values}) for k, values in a.items()}
        instances = lambda a: {k: sorted({tuple(v[:2]) for v in values}) for k, values in a.items()}
        both = sorted(before_addressing.keys() & after_addressing.keys())
        repooled = [k for k in both if pools(before_addressing)[k] != pools(after_addressing)[k]]
        report["skeleton_addressing"]["moved_meanwhile"] = [
            k for k in both if instances(before_addressing)[k] != instances(after_addressing)[k]]
        if repooled:
            raise ValueError("The skeleton resource of a drawn mesh changed during resource reads: " + ", ".join(repooled[:6]))
        skeleton_intact = all(report["buffers"][str(rid)]["meta"].get("unchanged_since_capture") is True
                              for rid in skeletons)
        report["skeleton_unchanged_since_capture"] = skeleton_intact
        if not deferred_skeleton and not skeleton_intact:
            raise ValueError("Skeleton readback is not a verified draw-time snapshot; static extraction needs independent samples")
        if volatile:
            begin("rewritten vertex data")
            report["volatile_geometry"] = _read_volatile(client, cap, pairs, volatile, buffers, output, report)
            stage("volatile_vertex_readback")
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
