"""What chosen draws paint: the render target just before and just after each of them.

A capture lists a thousand draws and what is bound for each, but not what any of
them contributes to the picture. The probe (0.1.5) can copy the target around
the draws it is told to watch while it records a frame. This module chooses
those draws from an earlier capture, takes the frame, reads the copies back and
says per draw how many pixels changed and where.
"""
from __future__ import annotations

import io
from pathlib import Path

from . import decode
from .capture_model import Capture, OUT_DSV
from .mcp_stdio import Image, ToolError

FEATURE = "rt_snapshots"
MAX_PREVIEWS = 6
PREVIEW_SIZE = 512
BUDGET_MB = 512         # of copied pixels per call; they sit in the game's memory until they have been read
NOT_COPIED = {
    "multisampled": "not copied: the target is multisampled",
    "over_budget": "not copied: over the pixel budget (use box, or select fewer draws)",
    "not_2d": "not copied: the target is not a 2D texture",
    "failed": "not copied: the copy could not be made",
}


# ---------------------------------------------------------------- choosing draws
def matcher_of(cap: Capture, draw: dict, with_shaders: bool = True) -> dict:
    """How the probe recognises this draw again while the next frame is issued."""
    matcher: dict = {}
    ib = draw.get("ib")
    if ib:
        resource = cap.resource(ib["res"]) or {}
        if resource.get("ptr"):
            matcher["ib"] = resource["ptr"]
            if resource.get("known") and resource.get("sn"):
                matcher["sn"] = resource["sn"]      # the same object, not just one at that address
        if draw["args"].get("index_count"):
            matcher["ic"] = draw["args"]["index_count"]
            matcher["fi"] = draw["args"].get("first_index", 0)
    if with_shaders or not matcher:
        for stage in ("vs", "ps"):
            if draw["shaders"].get(stage):
                matcher[stage] = draw["shaders"][stage]
    if draw.get("origin") in ("game", "xxmi"):
        matcher["o"] = draw["origin"]
    return matcher


def select(cap: Capture | None, hash_text: str, draws: list[int] | None) -> tuple[list[dict], str, set[str]]:
    """The matchers for a selection, how it reads in the result, and the index data hashes it stands for.

    The last is empty when the selection is not one of meshes (a shader, or draws
    without an index buffer): there is then nothing to hold the found draws against.
    """
    matchers: list[dict] = []
    hashes: set[str] = set()

    def add(draw: dict, with_shaders: bool) -> None:
        matcher = matcher_of(cap, draw, with_shaders)
        if matcher and matcher not in matchers:
            matchers.append(matcher)
        hashes.add((draw.get("ib") or {}).get("hash") or "")

    if draws:
        if cap is None:
            raise ToolError("draws are numbers of an existing capture: call efmi_capture_frame first")
        for index in draws:
            if not 0 <= index < len(cap):
                raise ToolError(f"draw {index} is not in capture {cap.frame} (0..{len(cap) - 1})")
            add(cap.draw(index), True)
        return (matchers, f"draws {draws} of capture {cap.frame}, found again by index buffer, range and shaders",
                hashes if "" not in hashes else set())
    wanted = hash_text.strip().lower().removeprefix("0x")
    if len(wanted) not in (8, 16) or any(digit not in "0123456789abcdef" for digit in wanted):
        raise ToolError("give draws (numbers of a capture) or hash: 8 hex digits for an index buffer, 16 for a shader")
    if len(wanted) == 16:
        return [{"sh": wanted}], f"every draw whose vertex or pixel shader is {wanted}", set()
    if cap is None:
        raise ToolError("an index buffer hash is looked up in an existing capture: call efmi_capture_frame first")
    for draw in cap.draws():
        ib = draw.get("ib")
        if ib and wanted in (ib["hash"], ib["resource_hash"]):
            # Every pass that draws this mesh, whatever its shaders.
            add(draw, False)
    if not matchers:
        raise ToolError(f"no draw of capture {cap.frame} has index buffer hash {wanted}")
    return (matchers, f"every draw of the mesh(es) with index buffer hash {wanted} (taken from capture {cap.frame})",
            hashes if "" not in hashes else set())


# ---------------------------------------------------------------- pixels
def changed_region(before: bytes, after: bytes, width: int, height: int, row_bytes: int):
    """(changed pixel count, [left, top, right, bottom] or None, mask) from the raw bytes of two copies."""
    try:
        import numpy as np
    except Exception:
        return None, None, None
    if len(before) != len(after) or not width or not height or row_bytes % width or len(after) < row_bytes * height:
        return None, None, None
    pixel = row_bytes // width
    a = np.frombuffer(before, np.uint8)[:row_bytes * height].reshape(height, width, pixel)
    b = np.frombuffer(after, np.uint8)[:row_bytes * height].reshape(height, width, pixel)
    mask = (a != b).any(axis=2)
    count = int(mask.sum())
    if not count:
        return 0, None, mask
    rows = np.flatnonzero(mask.any(axis=1))
    columns = np.flatnonzero(mask.any(axis=0))
    return count, [int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1], mask


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=1)
    return out.getvalue()


def _highlight(image, mask):
    """The picture after the draw with everything it did not touch dimmed."""
    import numpy as np
    from PIL import Image as PilImage

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    dimmed = (rgb * 0.25).astype(np.uint8)
    dimmed[mask] = rgb[mask]
    return PilImage.fromarray(dimmed, "RGB")


def _preview(image, box):
    """The changed area with a margin, at a size worth sending."""
    left, top, right, bottom = box
    margin = max(8, (right - left) // 6, (bottom - top) // 6)
    crop = image.crop((max(0, left - margin), max(0, top - margin),
                       min(image.width, right + margin), min(image.height, bottom + margin)))
    scale = PREVIEW_SIZE / max(crop.size)
    if scale < 1:
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    return crop


# ---------------------------------------------------------------- the tool
def capture_draw_outputs(session, *, hash: str = "", draws: list[int] | None = None, capture="latest",
                         max_draws: int = 8, before: bool = True, targets: str = "first", depth: bool = False,
                         box: list[int] | None = None, preview: bool = True) -> list:
    if targets not in ("first", "all"):
        raise ToolError("targets must be 'first' or 'all'")
    if box is not None and (len(box) != 4 or box[2] <= box[0] or box[3] <= box[1] or min(box) < 0):
        raise ToolError("box must be [left, top, right, bottom] in pixels of the render target")
    status = session.call(lambda c: c.status())
    if FEATURE not in (status.get("features") or ()):
        raise ToolError(f"Probe {status.get('probe')} cannot copy render targets around draws; this needs 0.1.5. "
                        f"Restart the game to load the updated {session.probe_dll}.")
    reference = session.capture(capture) if (session.latest is not None or capture not in (None, "", "latest")) else None
    matchers, chosen, selected_hashes = select(reference, hash, draws)
    request = {"match": matchers, "max": max_draws, "before": before, "all": targets == "all", "depth": depth,
               "budget_mb": BUDGET_MB}
    if box is not None:
        request["box"] = box
    doc, blob = session.call(lambda c: c.capture(detail="full", cb_data=False, rt=request))
    cap = Capture(doc, blob)
    session.add_capture(cap)
    try:
        return _report(session, cap, chosen, selected_hashes, max_draws, preview)
    finally:
        # The copies sit in the game's memory; everything else of the capture stays readable.
        try:
            session.call(lambda c: c.release_rt(cap.frame))
        except ToolError:
            pass                # the probe frees them with the capture at the latest


def _report(session, cap: Capture, chosen: str, selected_hashes: set[str], max_draws: int, preview: bool) -> list:
    folder = session.frame_dir(cap) / "draw-outputs"
    stats = cap.stats
    matched, copied, depth_only = stats.get("rt_matched", 0), stats.get("rt_draws", 0), stats.get("rt_depth_only", 0)
    lines = [f"capture {cap.frame}: {len(cap)} draws; watched: {chosen}",
             f"draws that matched: {matched}; targets copied for {copied} of them"
             + (f" (the first {max_draws} in frame order: raise max_draws, or select narrower)"
                if matched - depth_only > copied else "")
             + f"; {stats.get('rt_bytes', 0) / 2 ** 20:.1f} MiB of pixels"
             + (f"; {stats['rt_over_budget']} cop{'ies' if stats['rt_over_budget'] > 1 else 'y'} left out, "
                "over the pixel budget (use box)" if stats.get("rt_over_budget") else "")]
    if depth_only:
        lines.append((f"{depth_only} matching draws have no colour target (shadow or depth passes) and were not counted"
                      if depth_only > 1 else
                      "1 matching draw has no colour target (a shadow or depth pass) and was not counted")
                     + ": depth=true copies the depth target of such draws as well.")
    if not cap.rts:
        if not matched:
            lines.append("None of the watched draws was issued in this frame (the mesh may be off screen, or a reload "
                         "replaced its buffers: capture again and select anew).")
        return ["\n".join(lines)]

    # One entry per draw and target: its copy before and its copy after.
    pairs: dict[tuple, dict] = {}
    for number, snap in enumerate(cap.rts):
        pairs.setdefault((snap["draw"], snap["kind"], snap["slot"]), {})["after" if snap["when"] else "before"] = (number, snap)
    images: list = []
    results = []
    for (draw_index, kind, slot), pair in sorted(pairs.items()):
        draw = cap.draw(draw_index)
        target = "oD" if kind == OUT_DSV else f"o{slot}"
        entry: dict = {"draw": draw_index, "call": draw["call"], "origin": draw["origin"], "target": target}
        if draw.get("ib"):
            entry["ib"] = draw["ib"]["hash"]
            entry["index_count"] = draw["args"].get("index_count")
            if selected_hashes and entry["ib"] and entry["ib"] not in selected_hashes:
                # Found by buffer object and range, but that range holds other bytes now.
                entry["other_index_data"] = True
        entry["shaders"] = {stage: draw["shaders"][stage] for stage in ("vs", "ps") if draw["shaders"].get(stage)}
        read: dict[str, tuple] = {}
        for when in ("before", "after"):
            if when not in pair:
                continue
            number, snap = pair[when]
            if snap["state"] != "ok":
                entry[when] = NOT_COPIED.get(snap["state"], snap["state"])
                continue
            meta, data = session.call(lambda c, n=number: c.read_rt(cap.frame, n))
            read[when] = (meta, data, snap)
        if "after" in read:
            meta, data, snap = read["after"]
            resource = cap.resource(snap["res"]) or {}
            entry["target_resource"] = {"hash": resource.get("hash") if resource.get("known") else None,
                                        "size": [resource.get("w"), resource.get("h")], "format": snap["fmt_name"],
                                        "mip": snap["mip"], "slice": snap["slice"]}
            entry["copied_area"] = snap["box"]
            folder.mkdir(parents=True, exist_ok=True)
            stem = f"draw{draw_index:05d}-{target}"
            image = decode._texture_to_image(meta["width"], meta["height"], meta["format"], data)
            if image is None:
                path = folder / f"{stem}-after.dds"
                path.write_bytes(decode.make_dds(meta["width"], meta["height"], meta["format"], data))
            else:
                path = folder / f"{stem}-after.png"
                path.write_bytes(_png(image))
            entry["after"] = str(path)
            if "before" in read:
                count, where, mask = changed_region(read["before"][1], data, meta["width"], meta["height"], meta["row_bytes"])
                if count is None:
                    entry["changed_pixels"] = "not computed (numpy is needed)"
                else:
                    entry["changed_pixels"] = count
                    entry["changed_share_of_area"] = round(count / (meta["width"] * meta["height"]), 5)
                    if where:
                        # In pixels of the whole target, also when only a box of it was copied.
                        entry["changed_box"] = [where[0] + snap["box"][0], where[1] + snap["box"][1],
                                                where[2] + snap["box"][0], where[3] + snap["box"][1]]
                        if image is not None:
                            marked = _highlight(image, mask)
                            path = folder / f"{stem}-changed.png"
                            path.write_bytes(_png(marked))
                            entry["changed_image"] = str(path)
                            if preview and len(images) < MAX_PREVIEWS:
                                images.append(Image(_png(_preview(marked, where))))
                                entry["preview"] = len(images)
                before_image = decode._texture_to_image(meta["width"], meta["height"], meta["format"], read["before"][1])
                if before_image is not None:
                    path = folder / f"{stem}-before.png"
                    path.write_bytes(_png(before_image))
                    entry["before"] = str(path)
        results.append(entry)

    for entry in results:
        head = f"#{entry['draw']} {entry['call']} {entry['origin']}"
        if entry.get("ib"):
            head += f" ib={entry['ib'] or '-'} ic={entry['index_count']}" + session.mod_note(entry["ib"])
        head += "".join(f" {stage}={value}" for stage, value in entry["shaders"].items())
        lines.append(head)
        if entry.get("other_index_data"):
            lines.append("    NOTE: found by index buffer object and range, but the index data there hashes differently "
                         "than in the capture the selection came from: the game rewrote that buffer, so this may be "
                         "another mesh.")
        resource = entry.get("target_resource")
        if resource:
            lines.append(f"    {entry['target']}: {resource['hash'] or 'not created by the game (back buffer?)'} "
                         f"{resource['size'][0]}x{resource['size'][1]} {resource['format']}"
                         + (f" mip {resource['mip']}" if resource["mip"] else "")
                         + (f" slice {resource['slice']}" if resource["slice"] else ""))
        if isinstance(entry.get("changed_pixels"), str):
            lines.append(f"    changed pixels: {entry['changed_pixels']}")
        elif isinstance(entry.get("changed_pixels"), int):
            if entry["changed_pixels"]:
                lines.append(f"    changed {entry['changed_pixels']} pixels ({entry['changed_share_of_area'] * 100:.2f}% of the "
                             f"copied area) within {entry['changed_box']} (left, top, right, bottom)")
            elif entry["target"] == "oD":
                lines.append("    changed nothing in this target (depth writes are off, or nothing passed the depth test)")
            else:
                lines.append("    changed nothing in this target (culled, clipped, occluded or masked out, or it wrote "
                             "the values that were there)")
        for key, label in (("before", "before"), ("after", "after"), ("changed_image", "what it touched"),
                           ("preview", "preview image")):
            value = entry.get(key)
            if value is not None:
                lines.append(f"    {label}: {value}")
    lines.append("The pictures are the target as it was at that moment of the frame, not the finished frame. "
                 "Pixels a draw wrote with the value they already had do not count as changed.")
    if any(entry["target"] == "oD" for entry in results):
        lines.append("Depth pictures are scaled to each picture's own value range: go by the changed area, not by "
                     "brightness.")
    return ["\n".join(lines)] + images
