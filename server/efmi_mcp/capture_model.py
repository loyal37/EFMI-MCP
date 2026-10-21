"""Readable view over the compact capture document produced by the probe."""
from __future__ import annotations

from typing import Any, Iterator

from . import pipeline
from .fa_dump import DRAW_NAMES

STAGES = ["vs", "hs", "ds", "gs", "ps", "cs"]
OUT_RTV, OUT_DSV, OUT_CS_UAV, OUT_OM_UAV = 10, 11, 12, 13

TOPOLOGY = {
    0: "undefined", 1: "pointlist", 2: "linelist", 3: "linestrip", 4: "trianglelist", 5: "trianglestrip",
    10: "linelist_adj", 11: "linestrip_adj", 12: "trianglelist_adj", 13: "trianglestrip_adj",
}


# The probe's EV_* numbers (capture.h).
EVENT_NAMES = {1: "Map", 2: "Unmap", 3: "UpdateSubresource", 4: "CopyResource", 5: "CopySubresourceRegion",
               6: "CopyStructureCount", 7: "DiscardResource", 8: "ClearRenderTargetView", 9: "ClearDepthStencilView",
               10: "ClearUnorderedAccessViewUint", 11: "ClearUnorderedAccessViewFloat", 12: "GenerateMips",
               13: "ResolveSubresource", 14: "ExecuteCommandList", 15: "ClearState", 16: "ClearView", 17: "DiscardView"}


def topology_name(v: int) -> str:
    if v in TOPOLOGY:
        return TOPOLOGY[v]
    if 33 <= v <= 64:
        return f"{v - 32}_control_point_patchlist"
    return str(v)


class Capture:
    """One recorded frame."""

    def __init__(self, doc: dict, blob: bytes = b""):
        self.doc = doc
        self.blob = blob
        self.frame: int = doc["frame"]
        self.detail: str = doc.get("detail", "full")
        self.resources: list[dict] = doc.get("resources", [])
        self.shaders: list[dict] = doc.get("shaders", [])
        self.layouts: list[dict] = doc.get("layouts", [])
        self.regions: list[list] = doc.get("regions", [])
        self.snaps: list[list] = doc.get("snaps", [])
        self.raw_draws: list[dict] = doc.get("draws", [])
        # Probe 0.1.5: fixed-function state objects, view parameters, render target copies around draws.
        self.states: list[dict] = doc.get("states", [])
        self.views: list[list] = doc.get("views", [])
        self.rts: list[dict] = doc.get("rts", [])
        # Probe 0.1.6: what calls saw, kept by the probe ("cont"), and the writes between the calls ("ev").
        self.conts: list[list] = doc.get("cont", [])
        self.raw_events: list[list] = doc.get("ev", [])
        self.has_contents: bool = "cont" in doc
        self.stats: dict = doc.get("stats", {})
        self.resolution = doc.get("resolution", [-1, -1])
        self.held: bool = bool(doc.get("held"))
        # With track_region_hashes=1 (EFMI) a mod's ib/vb hash is the hash of the part a draw uses; with 0
        # (ZZMI and the other XXMI games) it is the creation hash of the whole buffer. Unknown: EFMI's way.
        settings = doc.get("xxmi_settings") or {}
        self.region_hashes: bool = bool(settings.get("track_region_hashes", True))

    def __len__(self) -> int:
        return len(self.raw_draws)

    # -- lookups --------------------------------------------------------
    def resource(self, rid: int) -> dict | None:
        return self.resources[rid] if 0 <= rid < len(self.resources) else None

    def res_hash(self, rid: int) -> str | None:
        r = self.resource(rid)
        return r.get("hash") if r and r.get("known") else None

    def mesh_hash(self, rid: int, region_id: int) -> str | None:
        """The hash a mod uses for an index or vertex buffer binding (see region_hashes)."""
        if not self.region_hashes:
            return self.res_hash(rid)
        r = self.region(region_id)
        return r["hash"] if r else None

    def ib_hash(self, raw_draw: dict) -> str | None:
        """The index buffer hash of a raw draw record as mods use it, or None."""
        ib = raw_draw.get("ib")
        return self.mesh_hash(ib[0], ib[3]) if ib else None

    def shader_hash(self, sid: int) -> str | None:
        if 0 <= sid < len(self.shaders):
            s = self.shaders[sid]
            return s.get("hash") if s.get("known") else None
        return None

    def region(self, idx: int) -> dict | None:
        if not (0 <= idx < len(self.regions)):
            return None
        res, off, size, h, state = self.regions[idx]
        return {"res": res, "offset": off, "size": size, "hash": h, "state": state}

    def snap_bytes(self, idx: int) -> bytes | None:
        if not (0 <= idx < len(self.snaps)):
            return None
        _res, _off, size, blob_off = self.snaps[idx]
        if blob_off is None:
            return None
        return self.blob[blob_off:blob_off + size]

    def snap(self, idx: int) -> dict | None:
        """A window copied into the capture: where it lies in its buffer, and its bytes (None: not delivered)."""
        if not (0 <= idx < len(self.snaps)):
            return None
        res, off, size, _blob_off = self.snaps[idx][:4]
        return {"res": res, "offset": off, "size": size, "data": self.snap_bytes(idx)}

    def content(self, idx: int | None) -> dict | None:
        """One entry of what the probe keeps: a version of a buffer, or one subresource of a texture."""
        if idx is None or not (0 <= idx < len(self.conts)):
            return None
        entry = self.conts[idx]
        if entry[1]:
            return {"id": idx, "res": entry[0], "kind": "texture", "mip": entry[2], "slice": entry[3]}
        return {"id": idx, "res": entry[0], "kind": "buffer", "version": entry[2]}

    def state(self, idx: int) -> dict | None:
        """A blend, depth-stencil, rasterizer or sampler state as the probe describes it; None: none bound."""
        return self.states[idx] if 0 <= idx < len(self.states) else None

    def view(self, idx: int) -> dict | None:
        """Which part of its resource a bound view shows, and in which format."""
        return pipeline.view_info(self.views[idx]) if 0 <= idx < len(self.views) else None

    def layout(self, lid: int) -> list[dict] | None:
        if not (0 <= lid < len(self.layouts)):
            return None
        out = []
        for sem, index, fmt, slot, offset, slot_class, step in self.layouts[lid].get("elems", []):
            out.append({"semantic": sem, "index": index, "format": fmt, "slot": slot, "offset": offset,
                        "per_instance": bool(slot_class), "step": step})
        return out

    # -- draws ----------------------------------------------------------
    def draw(self, i: int) -> dict:
        d = self.raw_draws[i]
        t = d["t"]
        out: dict[str, Any] = {
            "index": d["i"],
            "type": t,
            "call": DRAW_NAMES.get(t, str(t)),
            "origin": d.get("o", "unknown"),
            "args": {},
        }
        for key, name in (("ic", "index_count"), ("vc", "vertex_count"), ("inst", "instance_count"),
                          ("fi", "first_index"), ("fv", "first_vertex"), ("finst", "first_instance")):
            if key in d:
                out["args"][name] = d[key]
        if "tg" in d:
            out["args"]["thread_groups"] = d["tg"]
        if "ind" in d:
            out["indirect"] = {"res": d["ind"][0], "offset": d["ind"][1], "hash": self.res_hash(d["ind"][0])}
            if "inds" in d:
                # The arguments as the buffer held them when the call was issued.
                args = pipeline.indirect_args(t, self.snap_bytes(d["inds"]))
                if args:
                    out["indirect"]["args"] = args
        if "xd" in d:
            out["xxmi_depth"] = d["xd"]
            out["caller_rva"] = d.get("ra")

        def kept(entry: dict, key: str, n: int) -> dict:
            """Adds where the probe keeps what binding n of a list held (its position in "cont"), if it does."""
            ids = d.get(key)
            if ids and n < len(ids) and ids[n] >= 0:
                entry["content"] = ids[n]
            return entry

        if t < 8:
            out["topology"] = topology_name(d.get("topo", 0))
            out["layout"] = d.get("il", -1)
            if "ib" in d:
                res, fmt, off, reg = d["ib"]
                r = self.region(reg)
                out["ib"] = {"res": res, "format": fmt, "offset": off,
                             "hash": self.mesh_hash(res, reg),
                             "resource_hash": self.res_hash(res), "region": r}
                if "cib" in d:
                    out["ib"]["content"] = d["cib"]
            vbs = {}
            for n, (slot, res, stride, off, reg) in enumerate(d.get("vb", [])):
                r = self.region(reg)
                vbs[slot] = kept({"res": res, "stride": stride, "offset": off,
                                  "hash": self.mesh_hash(res, reg),
                                  "resource_hash": self.res_hash(res), "region": r}, "cvb", n)
            if vbs:
                out["vb"] = vbs

        if "so" in d:
            # Stream-output targets (probe 0.1.12, full captures): where the call writes its vertices.
            out["so"] = {f"so{slot}": {"res": res, "hash": self.res_hash(res)} for slot, res in d["so"]}

        out["shaders"] = {}
        for stage, sid in zip(STAGES, d.get("sh", [])):
            if sid >= 0:
                out["shaders"][stage] = self.shader_hash(sid)

        cbs = {}
        for n, (stage, slot, res, first, num, snap) in enumerate(d.get("cb", [])):
            cbs[f"{STAGES[stage]}-cb{slot}"] = kept({"res": res, "hash": self.res_hash(res), "first_constant": first,
                                                    "num_constants": num, "snap": snap}, "ccb", n)
        if cbs:
            out["cb"] = cbs

        tex = {}
        views = d.get("srvv") or []
        for n, (stage, slot, res) in enumerate(d.get("srv", [])):
            tex[f"{STAGES[stage]}-t{slot}"] = kept({"res": res, "hash": self.res_hash(res)}, "csrv", n)
            if n < len(views):
                tex[f"{STAGES[stage]}-t{slot}"]["view"] = self.view(views[n])
        if tex:
            out["tex"] = tex

        outs = {}
        views = d.get("outv") or []
        for n, (kind, slot, res) in enumerate(d.get("out", [])):
            # "u": what a compute shader writes (kind 12) or a pixel shader besides its targets (13).
            name = f"o{slot}" if kind == OUT_RTV else "oD" if kind == OUT_DSV else f"u{slot}"
            outs[name] = kept({"res": res, "hash": self.res_hash(res)}, "cout", n)
            if n < len(views):
                outs[name]["view"] = self.view(views[n])
        if outs:
            out["out"] = outs
        if "vp" in d:
            out["viewport"] = d["vp"]
        if "smp" in d:
            out["samplers"] = {f"{STAGES[stage]}-s{slot}": self.state(idx) for stage, slot, idx in d["smp"]}
        if "bs" in d:
            # Recorded by probe 0.1.5 for full captures; a state of None is D3D11's default.
            idx, *factor, mask = d["bs"]
            out["blend"] = {"state": self.state(idx), "factor": factor, "sample_mask": mask}
            out["depth_stencil"] = {"state": self.state(d["dss"][0]), "stencil_ref": d["dss"][1]}
            out["rasterizer"] = self.state(d.get("rs", -1))
            out["scissors"] = d.get("sc", [])
            out["viewport_count"] = d.get("vpn", 0)
            out["depth_range"] = d.get("vpz")
            if "vps" in d:
                out["viewports"] = d["vps"]     # every viewport: [left, top, width, height, min depth, max depth]
        return out

    def events(self) -> list[dict]:
        """The writes, copies and clears the frame issued between its calls (probe 0.1.6, when asked for)."""
        out = []
        for raw in self.raw_events:
            kind, before, dst, src, a0, a1, a2, a3 = raw[:8]
            event = {"type": EVENT_NAMES.get(kind, str(kind)), "before_call": before, "dst": dst, "src": src,
                     "a": [a0, a1, a2, a3]}
            if len(raw) >= 12:
                event["values"] = raw[8:12]
            out.append(event)
        return out

    def draws(self) -> Iterator[dict]:
        for i in range(len(self.raw_draws)):
            yield self.draw(i)
