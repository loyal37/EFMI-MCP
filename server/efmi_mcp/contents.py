"""Contents of a frame as its calls saw them (probe 0.1.6): asking for them, listing writes, writing them out.

A capture taken with contents has the probe keep what each call read and wrote.
The probe copies nothing while the frame is drawn; it copies a resource the
moment the game is about to change it, and reads everything else where it is.
The contents stay available while the capture is the latest one.
"""
from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path

from . import decode, pipeline, slot_data
from .capture_model import STAGES, Capture
from .hashes import crc32c, hex32
from .mcp_stdio import ToolError

FEATURE = "contents"
BUFFERS = ["ib", "vb", "cb", "buf"]
INPUTS = BUFFERS + ["tex"]
LEVELS = ("none", "buffers", "inputs", "outputs")
BUDGET_MB = 1024

# XXMI's names for the arguments of each call, as its log.txt writes them.
_CALL_ARGS = {
    "Draw": (("VertexCount", "vertex_count"), ("StartVertexLocation", "first_vertex")),
    "DrawIndexed": (("IndexCount", "index_count"), ("StartIndexLocation", "first_index"), ("BaseVertexLocation", "first_vertex")),
    "DrawInstanced": (("VertexCountPerInstance", "vertex_count"), ("InstanceCount", "instance_count"),
                      ("StartVertexLocation", "first_vertex"), ("StartInstanceLocation", "first_instance")),
    "DrawIndexedInstanced": (("IndexCountPerInstance", "index_count"), ("InstanceCount", "instance_count"),
                             ("StartIndexLocation", "first_index"), ("BaseVertexLocation", "first_vertex"),
                             ("StartInstanceLocation", "first_instance")),
}


def request(level: str, matchers: list[dict] | None, first: int, last: int, depth: bool, budget_mb: float,
            strict: bool = False) -> dict | None:
    """The probe's capture option for a level.

    none; buffers (the index, vertex and constant buffers and the buffers bound as shader resources that
    every call read); inputs (also the textures); outputs (also what calls wrote).
    """
    if level in (None, "", "none"):
        return None
    if level not in LEVELS:
        raise ToolError("contents must be 'none', 'buffers', 'inputs' or 'outputs'")
    out: dict = {"inputs": BUFFERS if level == "buffers" else INPUTS, "events": True, "budget_mb": budget_mb}
    if strict:
        out["strict"] = True
    if level == "outputs":
        out["outputs"] = ["rt", "uav"] + (["depth"] if depth else [])
        match = [dict(m) for m in (matchers or [])]
        if first >= 0 or last >= 0:
            span = {key: value for key, value in (("first", first), ("last", last)) if value >= 0}
            match = [{**m, **span} for m in match] or [span]
        if match:
            out["match"] = match
        else:
            out["all"] = True
    return out


def summary(cap: Capture) -> list[str]:
    """What a capture taken with contents says about them, for the capture's summary."""
    st = cap.stats
    if not cap.has_contents:
        return []
    lines = [f"contents: the probe keeps what each call saw ({st.get('ct_snaps', len(cap.conts))} resource versions noted); "
             f"{st.get('ct_bytes', 0) / 1048576:.1f} MiB copied so far of a budget of {st.get('ct_budget', 0) / 1048576:.0f} MiB "
             "(a copy is made when the game is about to overwrite something a call saw)"]
    if st.get("ct_out_calls"):
        lines.append(f"  what they wrote is kept for {st['ct_out_calls']} calls (render targets, unordered access views)")
    if st.get("ct_lost"):
        lines.append(f"  WARNING: {st['ct_lost']} versions could not be kept (budget or memory); reading them says so")
    if "events" in st:
        lines.append(f"  {st['events']} writes, copies and clears between the calls are listed (efmi_list_writes)"
                     + (f"; {st['events_dropped']} more were dropped" if st.get("events_dropped") else ""))
    lines.append("  efmi_read_buffer / efmi_read_texture now return what a slot held when its call was issued, until the next "
                 "capture or three minutes without a request; efmi_dump_frame writes them to a folder")
    return lines


# ---------------------------------------------------------------- the writes between the calls
def _resource_text(cap: Capture, rid: int) -> str:
    r = cap.resource(rid)
    if not r:
        return "-"
    kind = r.get("kind")
    size = f"{r['size']} bytes" if kind == "buffer" else "x".join(str(r[k]) for k in ("w", "h", "d") if k in r)
    return f"res{rid} {r.get('hash') or 'unhashed'} {kind} {size}"


def event_text(cap: Capture, event: dict) -> str:
    """One write as a line: what was done to which resource."""
    kind, a = event["type"], event["a"]
    dst = _resource_text(cap, event["dst"]) if event["dst"] >= 0 else ""
    src = _resource_text(cap, event["src"]) if event["src"] >= 0 else ""
    span = lambda off, size: "to the end" if size >= 0xFFFFFFFF - off else f"[{off}, {off + size})"   # noqa: E731
    if kind == "Map":
        how = {1: "read", 2: "write", 3: "read-write", 4: "write-discard", 5: "write-no-overwrite"}.get(a[1], str(a[1]))
        return f"Map {dst} subresource {a[0]} {how}"
    if kind == "Unmap":
        return f"Unmap {dst} subresource {a[0]}"
    if kind == "UpdateSubresource":
        return f"UpdateSubresource {dst} subresource {a[0]} " + (f"box from {a[1]}, {a[2]} wide" if a[3] else "whole")
    if kind == "CopyResource":
        return f"CopyResource into {dst} from {src}"
    if kind == "CopySubresourceRegion":
        return f"CopySubresourceRegion into {dst} subresource {a[0]} at {a[1]} ({span(a[1], a[2])}) from {src} subresource {a[3]}"
    if kind == "CopyStructureCount":
        return f"CopyStructureCount into {dst} at byte {a[0]}"
    if kind == "ResolveSubresource":
        return f"ResolveSubresource into {dst} subresource {a[0]} from {src} subresource {a[1]} as {decode.format_name(a[2])}"
    if kind in ("ClearRenderTargetView", "ClearUnorderedAccessViewFloat", "ClearView"):
        return f"{kind} {dst} to {[round(v, 6) for v in event.get('values', [])]}"
    if kind == "ClearUnorderedAccessViewUint":
        return f"{kind} {dst} to {[int(v) for v in event.get('values', [])]}"
    if kind == "ClearDepthStencilView":
        what = "+".join(name for bit, name in ((1, "depth"), (2, "stencil")) if a[0] & bit)
        return f"{kind} {dst} {what}: depth {event.get('values', [0])[0]:g}, stencil {a[1]}"
    return f"{kind} {dst}".rstrip()


# ---------------------------------------------------------------- what is bound differently from call to call
def _named(cap: Capture, entry: dict, key: str = "hash") -> str:
    return entry.get(key) or f"res{entry['res']}"


def bindings_of(cap: Capture, d: dict) -> dict[str, str]:
    """Everything a call has bound, slot by slot, as short texts that can be compared between two calls."""
    out: dict[str, str] = {stage: h or "unhashed" for stage, h in d["shaders"].items()}
    if d["type"] < 8:
        out["topology"] = str(d.get("topology"))
        out["input layout"] = f"#{d.get('layout')}" if d.get("layout", -1) >= 0 else "none"
        if "ib" in d:
            ib = d["ib"]
            out["ib"] = f"{_named(cap, ib, 'resource_hash')} at {ib['offset']} {decode.format_name(ib['format'])}"
        for n, vb in d.get("vb", {}).items():
            out[f"vb{n}"] = f"{_named(cap, vb, 'resource_hash')} at {vb['offset']} stride {vb['stride']}"
    for name, cb in d.get("cb", {}).items():
        out[name] = f"{_named(cap, cb)} registers {cb['first_constant']}..+{cb['num_constants']}"
    for group in ("tex", "out"):
        for name, entry in d.get(group, {}).items():
            view = entry.get("view")
            plain = not view or pipeline.view_is_plain(view, cap.resource(entry["res"]))
            out[name] = _named(cap, entry) + ("" if plain else f" ({pipeline.view_text(view)})")
    for name, state in (d.get("samplers") or {}).items():
        described = pipeline.describe_sampler(state) or {}
        out[name] = f"{described.get('filter')} {described.get('address')}"
    if "viewport" in d:
        out["viewport"] = str(d.get("viewports") or d["viewport"])
    if "blend" in d:
        slots = [int(k[1:]) + 1 for k in d.get("out", {}) if k[0] == "o" and k[1:].isdigit()]
        for name, described in (("blend", pipeline.describe_blend(d["blend"], max(slots, default=0))),
                                ("depth-stencil", pipeline.describe_depth(d["depth_stencil"])),
                                ("rasterizer", pipeline.describe_raster(d["rasterizer"]))):
            out[name] = json.dumps(described, separators=(",", ":"), default=str)
        if d.get("scissors"):
            out["scissor rects"] = str(d["scissors"])
    return out


# ---------------------------------------------------------------- log.txt in XXMI's own syntax
# The Set* calls themselves are not recorded; these lines are worked out from the bindings of each
# call, written only where a binding differs from the call before it. Resource addresses are made up
# (0x + the capture's resource number + 1): one address per resource within the dump, as in XXMI's log.
_NULL = "0x0000000000000000"
_SHADER_CALLS = (("vs", "VSSetShader", "pVertexShader"), ("hs", "HSSetShader", "pHullShader"),
                 ("ds", "DSSetShader", "pDomainShader"), ("gs", "GSSetShader", "pShader"),
                 ("ps", "PSSetShader", "pPixelShader"))


def _address(rid: int | None) -> str:
    return f"0x{rid + 1:016X}" if rid is not None and rid >= 0 else _NULL


def _slot_line(cap: Capture, slot, rid: int, hash_text: str | None = None, view: bool = False) -> list[str]:
    """XXMI's line under a Set* call for one bound slot; none for a resource without a hash."""
    h = hash_text or cap.res_hash(rid)
    if not h:
        return []
    return [f"       {slot}: " + (f"view={_address(rid)} " if view else "") + f"resource={_address(rid)} hash={h}"]


def xxmi_state_lines(cap: Capture, d: dict, raw: dict, state: dict) -> list[str]:
    """The Set* lines XXMI would have logged before this call, given the state left by the calls before.

    ``state`` is updated in place. Stream-output targets are only known in full captures.
    """
    lines: list[str] = []

    def changed(key, value) -> bool:
        if key in state and state[key] == value:
            return False
        state[key] = value
        return True

    if d["type"] >= 8:
        if changed("cs", d["shaders"].get("cs")):
            h = d["shaders"].get("cs")
            lines.append(f"CSSetShader(pComputeShader:{_address(raw['sh'][5]) if h else _NULL}, ppClassInstances:{_NULL}, "
                         f"NumClassInstances:0)" + (f" hash={h}" if h else ""))
        uavs = {int(name[1:]): entry["res"] for name, entry in d.get("out", {}).items() if name[0] == "u"}
        if changed("cs-uav", tuple(sorted(uavs.items()))) and uavs:
            count = max(uavs) + 1
            lines.append(f"CSSetUnorderedAccessViews(StartSlot:0, NumUAVs:{count}, ppUnorderedAccessViews:{_NULL}, "
                         f"pUAVInitialCounts:{_NULL})")
            for slot in range(count):
                if slot in uavs:
                    lines += _slot_line(cap, slot, uavs[slot], view=True)
        return lines

    vbs = {int(slot): vb for slot, vb in d.get("vb", {}).items()}
    if changed("vb", tuple((slot, vb["res"], vb["offset"], vb["stride"]) for slot, vb in sorted(vbs.items()))) and vbs:
        count = max(vbs) + 1
        lines.append(f"IASetVertexBuffers(StartSlot:0, NumBuffers:{count}, ppVertexBuffers:{_NULL}, pStrides:{_NULL}, "
                     f"pOffsets:{_NULL})")
        for slot in range(count):
            if slot in vbs:
                lines += _slot_line(cap, slot, vbs[slot]["res"], vbs[slot]["hash"])
    ib = d.get("ib")
    if changed("ib", (ib["res"], ib["format"], ib["offset"]) if ib else None):
        if ib:
            h = ib["hash"] or ib["resource_hash"]
            lines.append(f"IASetIndexBuffer(pIndexBuffer:{_address(ib['res'])}, Format:{ib['format']}, Offset:{ib['offset']})"
                         + (f" hash={h}" if h else ""))
        else:
            lines.append(f"IASetIndexBuffer(pIndexBuffer:{_NULL}, Format:0, Offset:0)")
    if changed("topology", raw.get("topo")) and raw.get("topo") is not None:
        lines.append(f"IASetPrimitiveTopology(Topology:{raw['topo']})")
    if "so" in d or cap.detail == "full":
        targets = {int(name[2:]): entry["res"] for name, entry in d.get("so", {}).items()}
        if changed("so", tuple(sorted(targets.items()))):
            count = max(targets) + 1 if targets else 0
            lines.append(f"SOSetTargets(NumBuffers:{count}, ppSOTargets:{_NULL}, pOffsets:{_NULL})")
            for slot in range(count):
                if slot in targets:
                    lines += _slot_line(cap, slot, targets[slot])
    for stage, call, arg in _SHADER_CALLS:
        h = d["shaders"].get(stage)
        if changed(stage, h):
            sid = raw["sh"][STAGES.index(stage)] if raw.get("sh") else -1
            lines.append(f"{call}({arg}:{_address(sid) if h else _NULL}, ppClassInstances:{_NULL}, NumClassInstances:0)"
                         + (f" hash={h}" if h else ""))
    return lines


def xxmi_event_lines(cap: Capture, event: dict) -> list[str]:
    """A write between calls as XXMI logs it; copies with the Src/Dst lines that name both resources."""
    kind, a, dst, src = event["type"], event["a"], event["dst"], event["src"]
    dst_hash = cap.res_hash(dst) if dst >= 0 else None
    tail = f" hash={dst_hash}" if dst_hash else ""
    if kind == "CopyResource":
        return ([f"CopyResource(pDstResource:{_address(dst)}, pSrcResource:{_address(src)})"]
                + _slot_line(cap, "Src", src) + _slot_line(cap, "Dst", dst))
    if kind == "CopySubresourceRegion":
        return ([f"CopySubresourceRegion(pDstResource:{_address(dst)}, DstSubresource:{a[0]}, DstX:{a[1]}, DstY:0, DstZ:0, "
                 f"pSrcResource:{_address(src)}, SrcSubresource:{a[3]}, pSrcBox:{_NULL})"]
                + _slot_line(cap, "Src", src) + _slot_line(cap, "Dst", dst))
    if kind == "Map":
        return [f"Map(pResource:{_address(dst)}, Subresource:{a[0]}, MapType:{a[1]}, MapFlags:0, pMappedResource:{_NULL}){tail}"]
    if kind == "Unmap":
        return [f"Unmap(pResource:{_address(dst)}, Subresource:{a[0]}){tail}"]
    if kind == "UpdateSubresource":
        return [f"UpdateSubresource(pDstResource:{_address(dst)}, DstSubresource:{a[0]}, pDstBox:{_NULL}, pSrcData:{_NULL}, "
                f"SrcRowPitch:0, SrcDepthPitch:0){tail}"]
    return [f"{kind}(pResource:{_address(dst)}){tail}"]


def binding_changes(before: dict[str, str] | None, now: dict[str, str]) -> list[str]:
    """One line for every slot a call has bound differently from the call before it (derived, not observed calls)."""
    if before is None:
        return []
    lines = []
    for name, value in now.items():
        if before.get(name) != value:
            lines.append(f"bind {name}: {value}" + (f" (was {before[name]})" if name in before else ""))
    lines += [f"bind {name}: nothing (was {value})" for name, value in before.items() if name not in now]
    return lines


def previous_bindings(cap: Capture, index: int, compute: bool, reach: int = 256) -> dict[str, str] | None:
    """The bindings of the nearest earlier call of the same kind: graphics state lasts across dispatches and back."""
    for n in range(index - 1, max(-1, index - 1 - reach), -1):
        if (cap.raw_draws[n]["t"] >= 8) == compute:
            return bindings_of(cap, cap.draw(n))
    return None


# ---------------------------------------------------------------- writing a frame out
def _element_text(data: bytes, offset: int, fmt: int) -> str | None:
    """One vertex element as XXMI prints it: the stored numbers, floats with nine significant digits."""
    spec = decode._VERTEX_FORMATS.get(fmt)
    if not spec:
        return None
    code, n, norm = spec
    if offset + struct.calcsize("<" + code) * n > len(data):
        return None
    values = struct.unpack_from(f"<{n}{code}", data, offset)
    if norm:
        scale = decode._NORM[(code, norm)]
        # what a shader gets: a 32-bit float
        values = tuple(struct.unpack("<f", struct.pack("<f", max(v / scale, -1.0)))[0] for v in values)
    if norm or code in ("f", "e"):
        return ", ".join(f"{v:.9g}" for v in values)
    return ", ".join(str(v) for v in values)


def _vertex_txt(slot: int, vb: dict, layout: list[dict], data: bytes, first: int, count: int, d: dict, a: dict) -> str:
    """A vertex buffer the way XXMI's frame analysis writes its .txt: the layout, then the vertices a draw uses.

    `first` is the vertex the listing starts at (the base vertex of an indexed draw); the entries are
    numbered from 0, so that an index of the draw names its entry.
    """
    lines = [f"byte offset: {vb['offset']}"] if vb["offset"] else []
    lines += [f"stride: {vb['stride']}", f"first vertex: {first}", f"vertex count: {count}"]
    if "instance_count" in a:
        lines += [f"first instance: {a.get('first_instance', 0)}", f"instance count: {a['instance_count']}"]
    lines.append(f"topology: {d.get('topology')}")
    for n, e in enumerate(layout):
        lines += [f"element[{n}]:", f"  SemanticName: {e['semantic']}", f"  SemanticIndex: {e['index']}",
                  f"  Format: {decode.format_name(e['format'])}", f"  InputSlot: {e['slot']}",
                  f"  AlignedByteOffset: {e['offset']}",
                  f"  InputSlotClass: {'per-instance' if e['per_instance'] else 'per-vertex'}",
                  f"  InstanceDataStepRate: {e['step']}"]
    lines += ["", "vertex-data:"]
    mine = [e for e in layout if e["slot"] == slot]
    stride = vb["stride"]
    for v in range(count):
        at = vb["offset"] + (first + v) * stride
        lines.append("")
        for e in mine:
            values = _element_text(data, at + e["offset"], e["format"])
            if values is None:
                continue
            name = e["semantic"] + (str(e["index"]) if e["index"] else "")
            lines.append(f"vb{slot}[{v}]+{e['offset']:03d} {name}: {values}")
    return "\n".join(lines) + "\n"


def _index_txt(ib: dict, indices: list[int], d: dict, first_index: int) -> str:
    lines = [f"byte offset: {ib['offset']}", f"first index: {first_index}", f"index count: {len(indices)}",
             f"topology: {d.get('topology')}", f"format: DXGI_FORMAT_{decode.format_name(ib['format'])}", ""]
    step = 3 if d.get("topology") == "trianglelist" else 1
    lines += [" ".join(str(i) for i in indices[n:n + step]) for n in range(0, len(indices), step)]
    return "\n".join(lines) + "\n"


def _constants_txt(slot: int, data: bytes, first_register: int) -> str:
    floats = struct.unpack_from(f"<{len(data) // 4}f", data)
    return "".join(f"cb{slot}[{first_register + n // 4}].{'xyzw'[n % 4]}: {v:.9g}\n" for n, v in enumerate(floats))


def call_line(cap: Capture, d: dict) -> str:
    """A call as XXMI's log.txt writes it."""
    name, a = d["call"], dict(d["args"])
    if name in _CALL_ARGS:
        return f"{name}(" + ", ".join(f"{label}:{a.get(key, 0)}" for label, key in _CALL_ARGS[name]) + ")"
    if name == "Dispatch":
        x, y, z = a.get("thread_groups", [0, 0, 0])
        return f"Dispatch(ThreadGroupCountX:{x}, ThreadGroupCountY:{y}, ThreadGroupCountZ:{z})"
    if "indirect" in d:
        return f"{name}(pBufferForArgs:res{d['indirect']['res']}, AlignedByteOffsetForArgs:{d['indirect']['offset']})"
    return f"{name}()"


class _Writer:
    """Files of one dump: each distinct content once under deduped/, linked from the name of every call that has it."""

    def __init__(self, folder: Path, max_bytes: int):
        self.folder = folder
        self.deduped = folder / "deduped"
        self.deduped.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.bytes = 0
        self.files = 0
        self.by_key: dict[tuple, Path] = {}
        self.over_limit = 0

    def room(self, size: int) -> bool:
        if self.bytes + size > self.max_bytes:
            self.over_limit += 1
            return False
        return True

    def store(self, key: tuple, name: str, make) -> Path | None:
        """The deduped file for `key`, written by make() -> bytes the first time. None when the size limit is reached."""
        path = self.by_key.get(key)
        if path is None:
            data = make()
            if data is None or not self.room(len(data)):
                return None
            path = self.deduped / name.format(crc=hex32(crc32c(data)))
            if not path.exists():
                path.write_bytes(data)
                self.bytes += len(data)
            self.by_key[key] = path
        return path

    def link(self, target: Path | None, name: str) -> bool:
        if target is None:
            return False
        path = self.folder / name
        if path.exists():
            path.unlink()
        try:
            os.link(target, path)             # the way XXMI lays its folder out: one file, many names
        except OSError:
            path.write_bytes(target.read_bytes())
        self.files += 1
        return True


LOG_STYLES = ("readable", "xxmi")


def dump_frame(session, cap: Capture, *, first: int = 0, last: int = -1, kinds: list[str] | None = None,
               txt: bool = True, only_exact: bool = True, max_mb: int = 2048, max_seconds: float = 120.0,
               folder: str = "", log: str = "readable", files_for: set[int] | None = None) -> dict:
    """Writes the calls [first, last] of a capture as a folder laid out like an XXMI frame analysis dump.

    Buffers go out whole as .buf (with the .txt XXMI writes for index, vertex and constant
    buffers), textures as .dds holding the subresource a view shows, outputs as the call left
    them. Only bytes that are those of the call are written unless only_exact is False.
    log='xxmi' writes log.txt in XXMI's own syntax, Set* calls included (worked out from the
    bindings), for tools that read XXMI's frame analysis; 'readable' writes 'bind' lines instead.
    files_for: write files only for these calls; log.txt still lists every call of the range,
    with the writes between them, so what earlier calls did to a resource stays visible.
    """
    kinds = set(kinds or ["ib", "vb", "cb", "buf", "tex", "out"])
    unknown = kinds - {"ib", "vb", "cb", "buf", "tex", "out"}
    if unknown:
        raise ToolError(f"unknown kinds {sorted(unknown)}; choose from ib, vb, cb, buf, tex, out")
    if log not in LOG_STYLES:
        raise ToolError(f"log must be one of {', '.join(LOG_STYLES)}")
    total = len(cap)
    last = total - 1 if last < 0 else min(last, total - 1)
    if not 0 <= first <= last:
        raise ToolError(f"first/last must lie in 0..{total - 1}")
    root = Path(folder) if folder else session.frame_dir(cap) / "dump"
    root.mkdir(parents=True, exist_ok=True)
    writer = _Writer(root, int(max_mb) * 1048576)
    started = time.monotonic()
    skipped = {"not_exact": 0, "failed": 0, "outputs_not_kept": 0}
    problems: list[str] = []
    log_lines: list[str] = [f"analyse_options: efmi-mcp capture {cap.frame}",
                            "written by efmi-mcp from a live capture, laid out like XXMI's frame analysis; calls are numbered "
                            "as in the capture (from 0), and XXMI's own command list and variable trace is not in here"]
    if log == "xxmi":
        log_lines.append("the Set* lines are worked out from the bindings of the calls, where a binding differs from the "
                         "call before; the Set* calls themselves were not recorded, and resource addresses are made up "
                         "(one per resource of the capture)")
    else:
        log_lines.append("'bind' lines say what a call has bound differently from the call of its kind before it; they "
                         "are worked out from the bindings of the calls, the Set* calls themselves were not recorded")
    bound: dict[bool, dict | None] = {False: previous_bindings(cap, first, False), True: previous_bindings(cap, first, True)}
    state: dict = {}
    events = cap.events()
    by_call: dict[int, list[dict]] = {}
    for event in events:
        by_call.setdefault(event["before_call"], []).append(event)
    buffers: dict[tuple, tuple[bytes, dict]] = {}     # (resource, content) -> whole buffer and where it came from
    index_cache: dict[tuple, list[int]] = {}
    done = first - 1

    def whole_buffer(binding: dict):
        key = (binding["res"], binding.get("content"))
        if key not in buffers:
            buffers[key] = slot_data.read_slot(session, cap, binding, whole=True)
            # A frame has few distinct large buffers; keep a bounded number of megabytes of them at hand.
            while len(buffers) > 1 and sum(len(data) for data, _info in buffers.values()) > 256 * 1048576:
                del buffers[next(k for k in buffers if k != key)]
        return buffers[key]

    def usable(info: dict, what: str) -> bool:
        if info["exact"] or not only_exact:
            return True
        skipped["not_exact"] += 1
        if len(problems) < 12:
            problems.append(f"{what}: {info['note']}")
        return False

    def buffer_file(binding: dict, what: str) -> Path | None:
        try:
            data, info = whole_buffer(binding)
        except ToolError as exc:
            skipped["failed"] += 1
            if len(problems) < 12:
                problems.append(f"{what}: {exc}")
            return None
        if not usable(info, what):
            return None
        return writer.store(("buf", binding["res"], binding.get("content")), "{crc}.buf", lambda: data)

    for index in range(first, last + 1):
        if time.monotonic() - started > max_seconds:
            break
        d = cap.draw(index)
        prefix = f"{index:06d}"
        shaders = "".join(f"-{stage}={d['shaders'][stage]}" for stage in STAGES if d["shaders"].get(stage))
        if log == "xxmi":
            for event in by_call.get(index, []):
                lines = xxmi_event_lines(cap, event)
                log_lines += [f"{prefix} {lines[0]}"] + lines[1:]
            for line in xxmi_state_lines(cap, d, cap.raw_draws[index], state):
                log_lines.append(line if line.startswith(" ") else f"{prefix} {line}")
        else:
            for event in by_call.get(index, []):
                log_lines.append(f"{prefix} {event_text(cap, event)}")
            now = bindings_of(cap, d)
            log_lines += [f"{prefix} {line}" for line in binding_changes(bound[d["type"] >= 8], now)]
            bound[d["type"] >= 8] = now
        log_lines.append(f"{prefix} {call_line(cap, d)}")
        if files_for is not None and index not in files_for:
            done = index
            continue

        def named(slot: str, hash_text: str | None, resource_hash: str | None = None) -> str:
            mark = f"={hash_text}" + (f"({resource_hash})" if resource_hash and resource_hash != hash_text else "") if hash_text else ""
            return f"{prefix}-{slot}{mark}{shaders}"

        # ---- index and vertex buffers
        indices = None
        if "ib" in d and (kinds & {"ib", "vb"}):
            binding = slot_data.buffer_binding(cap, d, "ib")
            ib = d["ib"]
            stem = named("ib", ib["hash"] or ib["resource_hash"], ib["resource_hash"] if ib["hash"] else None)
            target = buffer_file(binding, f"call {index} ib")
            if target is not None and binding["exact_range"] and binding["size"]:
                key = (binding["res"], binding.get("content"), binding["offset"], binding["size"], ib["format"])
                if key not in index_cache:
                    data = whole_buffer(binding)[0]
                    index_cache[key] = decode.decode_indices(data[binding["offset"]:binding["offset"] + binding["size"]], ib["format"])
                indices = index_cache[key]
            if "ib" in kinds and writer.link(target, stem + ".buf") and txt and indices is not None:
                a = {**d["args"], **((d.get("indirect") or {}).get("args") or {})}
                # The same indices bound at another offset are another text: its header says how they were bound.
                text_file = writer.store(("ibtxt", ib["offset"], a.get("first_index", 0), d.get("topology")) + key, "{crc}-ib.txt",
                                         lambda: _index_txt(ib, indices, d, a.get("first_index", 0)).encode("ascii"))
                writer.link(text_file, stem + ".txt")
        if "vb" in kinds:
            layout = cap.layout(d.get("layout", -1)) or []
            a = {**d["args"], **((d.get("indirect") or {}).get("args") or {})}
            for slot, vb in d.get("vb", {}).items():
                binding = slot_data.buffer_binding(cap, d, f"vb{slot}")
                stem = named(f"vb{slot}", vb["hash"] or vb["resource_hash"], vb["resource_hash"] if vb["hash"] else None)
                target = buffer_file(binding, f"call {index} vb{slot}")
                if not writer.link(target, stem + ".buf") or not txt or not vb["stride"]:
                    continue
                elements = [e for e in layout if e["slot"] == slot]
                if not elements:
                    continue
                if all(e["per_instance"] for e in elements):
                    start, count = a.get("first_instance", 0), max(a.get("instance_count", 1), 1)
                elif indices:
                    # As XXMI lists them: from the base vertex, up to the highest index the draw uses.
                    start, count = a.get("first_vertex", 0), max(indices) + 1
                elif not a.get("index_count"):
                    start, count = a.get("first_vertex", 0), a.get("vertex_count", 0)
                else:
                    continue
                data = whole_buffer(binding)[0]
                base = vb["offset"] + start * vb["stride"]
                if base < 0 or base >= len(data):
                    continue
                count = max(0, min(count, (len(data) - base) // vb["stride"]))
                key = ("vbtxt", binding["res"], binding.get("content"), slot, vb["offset"], vb["stride"], start, count,
                       d.get("layout"), d.get("topology"), a.get("first_instance", 0), a.get("instance_count"))
                text_file = writer.store(key, "{crc}-vb" + str(slot) + ".txt",
                                         lambda: _vertex_txt(slot, vb, layout, data, start, count, d, a).encode("ascii", "replace"))
                writer.link(text_file, stem + ".txt")

        # ---- constant buffers
        if "cb" in kinds:
            for name, cb in d.get("cb", {}).items():
                binding = slot_data.buffer_binding(cap, d, name)
                stem = named(name, cb["hash"])
                target = buffer_file(binding, f"call {index} {name}")
                if writer.link(target, stem + ".buf") and txt:
                    data = whole_buffer(binding)[0]
                    register = int(name.split("cb")[1])
                    # XXMI writes every register of the buffer. So does this, up to the size a constant buffer
                    # can be bound whole; of a larger pool only the bound registers are written.
                    whole = len(data) <= 65536
                    window = data if whole else data[binding["offset"]:binding["offset"] + binding["size"]]
                    first_register = 0 if whole else cb["first_constant"]
                    key = ("cbtxt", binding["res"], binding.get("content"), first_register, len(window), register)
                    text_file = writer.store(key, "{crc}-cb" + str(register) + ".txt",
                                             lambda: _constants_txt(register, window, first_register).encode("ascii"))
                    writer.link(text_file, stem + ".txt")

        # ---- what is bound for reading, and what the call wrote
        groups = (("tex", d.get("tex", {})) if kinds & {"buf", "tex"} else ("tex", {}),
                  ("out", d.get("out", {})) if "out" in kinds else ("out", {}))
        for group, entries in groups:
            for name, entry in entries.items():
                resource = cap.resource(entry["res"]) or {}
                what = f"call {index} {name}"
                stem = named(name, entry["hash"])
                if resource.get("kind") == "buffer":
                    if group == "tex" and "buf" not in kinds:
                        continue
                    if group == "out" and entry.get("content") is None:
                        skipped["outputs_not_kept"] += 1           # what a call wrote is only known when it was kept
                        continue
                    writer.link(buffer_file(slot_data.buffer_binding(cap, d, name), what), stem + ".buf")
                    continue
                if group == "tex" and "tex" not in kinds:
                    continue
                if resource.get("kind") not in ("tex1d", "tex2d", "tex3d"):
                    continue
                content = entry.get("content")
                if content is None and (group == "out" or only_exact):
                    skipped["outputs_not_kept" if group == "out" else "not_exact"] += 1
                    continue
                key = ("tex", entry["res"], content) if content is not None else ("texlate", entry["res"])
                if key not in writer.by_key:
                    try:
                        if content is not None:
                            meta, data = session.call(lambda c: c.read_content(cap.frame, content))
                        else:
                            meta, data = session.call(lambda c: c.read_texture(cap.frame, entry["res"]))
                            meta["exact"] = False
                    except ToolError as exc:
                        skipped["failed"] += 1
                        if len(problems) < 12:
                            problems.append(f"{what}: {exc}")
                        continue
                    if not meta.get("exact", False) and only_exact:
                        skipped["not_exact"] += 1
                        continue
                    dds = decode.make_dds(meta["width"], meta["height"], meta["format"], data, depth=meta.get("depth", 0))
                    target = writer.store(key, "{crc}-" + meta["format_name"] + ".dds", lambda: dds)
                else:
                    target = writer.by_key[key]
                writer.link(target, stem + ".dds")
        if writer.over_limit:
            break                   # this call is incomplete: it is where to go on
        done = index

    (root / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    result = {"folder": str(root), "capture": cap.frame, "calls_written": [first, done] if done >= first else [],
              "files": writer.files, "distinct_contents": len(writer.by_key), "megabytes": round(writer.bytes / 1048576, 2),
              "log": str(root / "log.txt")}
    if any(skipped.values()):
        result["skipped"] = {k: v for k, v in skipped.items() if v}
    if done < last:
        result["stopped"] = ("the time limit (max_seconds) was reached" if not writer.over_limit else
                             f"call {done + 1} alone does not fit max_mb={max_mb}: raise it or select fewer kinds" if done < first
                             else "the size limit (max_mb) was reached")
        result["continue_with"] = {"first": done + 1, "last": last}
    if problems:
        result["problems"] = problems
    if not cap.has_contents:
        result["note"] = ("this capture was taken without contents: only what provably has not changed since is written. "
                          "Capture with contents='inputs' or 'outputs' to get what each call saw.")
    return result
