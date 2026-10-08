"""Names for the fixed-function state and the view parameters a full capture records (probe 0.1.5).

The probe sends the D3D11 descriptions as numbers; this turns them into what an
agent can read without the SDK headers at hand.
"""
from __future__ import annotations

from .decode import format_name

BLEND = {1: "zero", 2: "one", 3: "src_color", 4: "inv_src_color", 5: "src_alpha", 6: "inv_src_alpha",
         7: "dest_alpha", 8: "inv_dest_alpha", 9: "dest_color", 10: "inv_dest_color", 11: "src_alpha_sat",
         14: "blend_factor", 15: "inv_blend_factor", 16: "src1_color", 17: "inv_src1_color",
         18: "src1_alpha", 19: "inv_src1_alpha"}
BLEND_OP = {1: "add", 2: "subtract", 3: "rev_subtract", 4: "min", 5: "max"}
COMPARISON = {1: "never", 2: "less", 3: "equal", 4: "less_equal", 5: "greater", 6: "not_equal",
              7: "greater_equal", 8: "always"}
STENCIL_OP = {1: "keep", 2: "zero", 3: "replace", 4: "incr_sat", 5: "decr_sat", 6: "invert", 7: "incr", 8: "decr"}
FILL = {2: "wireframe", 3: "solid"}
CULL = {1: "none", 2: "front", 3: "back"}
ADDRESS = {1: "wrap", 2: "mirror", 3: "clamp", 4: "border", 5: "mirror_once"}
_FILTER = {0x00: "min_mag_mip_point", 0x01: "min_mag_point_mip_linear", 0x04: "min_point_mag_linear_mip_point",
           0x05: "min_point_mag_mip_linear", 0x10: "min_linear_mag_mip_point",
           0x11: "min_linear_mag_point_mip_linear", 0x14: "min_mag_linear_mip_point",
           0x15: "min_mag_mip_linear", 0x55: "anisotropic"}
_REDUCTION = {0: "", 1: "comparison_", 2: "minimum_", 3: "maximum_"}

VIEW_KINDS = ["srv", "rtv", "dsv", "uav"]
_DIMENSIONS = {
    "srv": {1: "buffer", 2: "tex1d", 3: "tex1d_array", 4: "tex2d", 5: "tex2d_array", 6: "tex2d_ms",
            7: "tex2d_ms_array", 8: "tex3d", 9: "cube", 10: "cube_array", 11: "buffer"},
    "rtv": {1: "buffer", 2: "tex1d", 3: "tex1d_array", 4: "tex2d", 5: "tex2d_array", 6: "tex2d_ms",
            7: "tex2d_ms_array", 8: "tex3d"},
    "dsv": {1: "tex1d", 2: "tex1d_array", 3: "tex2d", 4: "tex2d_array", 5: "tex2d_ms", 6: "tex2d_ms_array"},
    "uav": {1: "buffer", 2: "tex1d", 3: "tex1d_array", 4: "tex2d", 5: "tex2d_array", 8: "tex3d"},
}
ALL = 0xFFFFFFFF      # "to the end" for mip, slice and element counts


def _name(table: dict, value: int) -> str:
    return table.get(value, str(value))


def filter_name(value: int) -> str:
    return _REDUCTION[(value >> 7) & 3] + _FILTER.get(value & 0x7F, f"0x{value & 0x7F:02x}")


def write_mask(mask: int) -> str:
    return "".join(letter for bit, letter in ((1, "r"), (2, "g"), (4, "b"), (8, "a")) if mask & bit) or "none"


# ---------------------------------------------------------------- state objects
def describe_blend(blend: dict) -> dict:
    """``blend``: {"state": the probe's description or None, "factor": [r, g, b, a], "sample_mask": int}."""
    state = blend.get("state")
    out: dict = {}
    if state is None:
        # No state object bound: the D3D11 defaults.
        out["default"] = True
        targets = [[0, 2, 1, 1, 2, 1, 1, 15]]
        independent = False
        out["alpha_to_coverage"] = False
    else:
        targets, independent = state["rt"], bool(state.get("ind"))
        out["alpha_to_coverage"] = bool(state.get("a2c"))
    described = []
    for enable, src, dst, op, src_a, dst_a, op_a, mask in targets:
        entry: dict = {"enabled": bool(enable), "write_mask": write_mask(mask)}
        if enable:
            entry.update(src=_name(BLEND, src), dst=_name(BLEND, dst), op=_name(BLEND_OP, op),
                         src_alpha=_name(BLEND, src_a), dst_alpha=_name(BLEND, dst_a), op_alpha=_name(BLEND_OP, op_a))
        described.append(entry)
    if independent:
        out["targets"] = described            # one entry per render target slot
    else:
        out["all_targets"] = described[0]
    out["blend_factor"] = blend.get("factor")
    out["sample_mask"] = f"0x{blend.get('sample_mask', 0xFFFFFFFF):08x}"
    return out


def _stencil_face(face: list) -> dict:
    fail, depth_fail, passed, func = face
    return {"func": _name(COMPARISON, func), "pass": _name(STENCIL_OP, passed), "fail": _name(STENCIL_OP, fail),
            "depth_fail": _name(STENCIL_OP, depth_fail)}


def describe_depth(depth: dict) -> dict:
    """``depth``: {"state": description or None, "stencil_ref": int}."""
    state = depth.get("state")
    if state is None:
        return {"default": True, "depth_test": True, "depth_write": True, "depth_func": "less", "stencil": False}
    # With the depth test off nothing is compared or written, and D3D11 reports the
    # write mask and function of such a state as their defaults whatever was asked for.
    out: dict = {"depth_test": bool(state["de"])}
    if state["de"]:
        out.update(depth_write=bool(state["dw"]), depth_func=_name(COMPARISON, state["df"]))
    if state["se"]:
        out["stencil"] = {"ref": depth.get("stencil_ref", 0), "read_mask": f"0x{state['srm']:02x}",
                          "write_mask": f"0x{state['swm']:02x}", "front": _stencil_face(state["ff"]),
                          "back": _stencil_face(state["bf"])}
    else:
        out["stencil"] = False
    return out


def describe_raster(state: dict | None) -> dict:
    if state is None:
        return {"default": True, "fill": "solid", "cull": "back", "front_ccw": False, "depth_clip": True,
                "scissor": False}
    return {"fill": _name(FILL, state["fill"]), "cull": _name(CULL, state["cull"]), "front_ccw": bool(state["ccw"]),
            "depth_bias": state["db"], "depth_bias_clamp": state["dbc"], "slope_scaled_depth_bias": state["ssdb"],
            "depth_clip": bool(state["dc"]), "scissor": bool(state["sc"]), "multisample": bool(state["ms"]),
            "antialiased_line": bool(state["aal"])}


def describe_sampler(state: dict | None) -> dict | None:
    if state is None:
        return None
    address = [_name(ADDRESS, state[key]) for key in ("u", "v", "w")]
    out = {"filter": filter_name(state["f"]), "address": "/".join(address), "lod": [state["minl"], state["maxl"]],
           "lod_bias": state["lb"]}
    # D3D11 reports what a sampler does not use as zero: the anisotropy of other
    # filters, the function of non-comparison filters, an unused border colour.
    if state["f"] & 0x7F == 0x55:
        out["max_anisotropy"] = state["ma"]
    if (state["f"] >> 7) & 3 == 1:
        out["comparison"] = _name(COMPARISON, state["cf"])
    if "border" in address:
        out["border_color"] = state["bc"]
    return out


# ---------------------------------------------------------------- views
def view_info(raw: list) -> dict:
    """[resource, kind, format, dimension, first, count, first slice, slice count, flags] as named fields."""
    res, kind_id, fmt, dim, first, count, first_slice, slices, flags = raw
    kind = VIEW_KINDS[kind_id] if 0 <= kind_id < len(VIEW_KINDS) else str(kind_id)
    dimension = _DIMENSIONS.get(kind, {}).get(dim, str(dim))
    out: dict = {"kind": kind, "format": format_name(fmt), "dimension": dimension}
    if dimension == "buffer":
        out["first_element"] = first
        out["num_elements"] = count
        if flags:
            names = {"srv": {1: "raw"}, "uav": {1: "raw", 2: "append", 4: "counter"}}.get(kind, {})
            out["flags"] = [name for bit, name in names.items() if flags & bit] or flags
    else:
        if kind == "srv":
            if "ms" not in dimension:
                out["most_detailed_mip"] = first
                out["mip_levels"] = "all" if count == ALL else count
        elif "ms" not in dimension:
            out["mip_slice"] = first
        if "array" in dimension or dimension == "tex3d" and kind != "srv":
            out["first_w" if dimension == "tex3d" else "first_face" if dimension == "cube_array" else "first_slice"] = first_slice
            out["w_size" if dimension == "tex3d" else "cubes" if dimension == "cube_array" else "array_size"] = \
                "all" if slices == ALL else slices
        if kind == "dsv" and flags:
            out["read_only"] = [name for bit, name in ((1, "depth"), (2, "stencil")) if flags & bit]
    return out


def view_is_plain(view: dict, resource: dict | None) -> bool:
    """True for a view that shows all of a texture, from its first mip, in the texture's own format.

    That is what a view made without a description does, and nothing a slot listing
    has to mention. Buffer views always say which elements they cover.
    """
    if not resource or view["dimension"] == "buffer" or view["format"] != resource.get("fmt_name"):
        return False
    if view.get("most_detailed_mip") or view.get("mip_slice") or view.get("read_only") or view.get("flags"):
        return False
    if view.get("mip_levels", "all") not in ("all", resource.get("mips")):
        return False
    layers = resource.get("array", 1)
    for first, size, total in (("first_slice", "array_size", layers), ("first_w", "w_size", resource.get("d")),
                               ("first_face", "cubes", layers // 6)):
        if first in view and (view[first] or view[size] not in ("all", total)):
            return False
    return True


def view_text(view: dict | None) -> str:
    """One line for a slot listing: the format the view reads its resource in and the part it shows."""
    if not view:
        return ""
    parts = [view["format"], view["dimension"]]
    if "first_element" in view:
        parts.append(f"elements {view['first_element']}..+{view['num_elements']}")
    if "most_detailed_mip" in view:
        parts.append(f"mips {view['most_detailed_mip']}..+{view['mip_levels']}")
    if "mip_slice" in view:
        parts.append(f"mip {view['mip_slice']}")
    for first, size, label in (("first_slice", "array_size", "slices"), ("first_w", "w_size", "w"),
                               ("first_face", "cubes", "faces from")):
        if first in view:
            parts.append(f"{label} {view[first]}..+{view[size]}")
    if view.get("flags"):
        parts.append("flags " + ",".join(map(str, view["flags"])) if isinstance(view["flags"], list) else f"flags {view['flags']}")
    if view.get("read_only"):
        parts.append("read-only " + "+".join(view["read_only"]))
    return " ".join(parts)


# ---------------------------------------------------------------- indirect arguments
def indirect_args(draw_type: int, data: bytes | None) -> dict | None:
    """The arguments an indirect call reads from its buffer, as they were when it was issued."""
    import struct

    if not data:
        return None
    if draw_type == 6 and len(data) >= 20:
        index_count, instances, first_index, base_vertex, first_instance = struct.unpack_from("<IIIiI", data)
        return {"index_count": index_count, "instance_count": instances, "first_index": first_index,
                "first_vertex": base_vertex, "first_instance": first_instance}
    if draw_type == 5 and len(data) >= 16:
        vertex_count, instances, first_vertex, first_instance = struct.unpack_from("<IIII", data)
        return {"vertex_count": vertex_count, "instance_count": instances, "first_vertex": first_vertex,
                "first_instance": first_instance}
    if draw_type == 9 and len(data) >= 12:
        return {"thread_groups": list(struct.unpack_from("<III", data))}
    return None
