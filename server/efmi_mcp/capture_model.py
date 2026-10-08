"""Readable view over the compact capture document produced by the probe."""
from __future__ import annotations

from typing import Any, Iterator

from . import pipeline
from .fa_dump import DRAW_NAMES

STAGES = ["vs", "hs", "ds", "gs", "ps", "cs"]
OUT_RTV, OUT_DSV, OUT_CS_UAV = 10, 11, 12

TOPOLOGY = {
    0: "undefined", 1: "pointlist", 2: "linelist", 3: "linestrip", 4: "trianglelist", 5: "trianglestrip",
    10: "linelist_adj", 11: "linestrip_adj", 12: "trianglelist_adj", 13: "trianglestrip_adj",
}


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
        self.stats: dict = doc.get("stats", {})
        self.resolution = doc.get("resolution", [-1, -1])
        self.held: bool = bool(doc.get("held"))

    def __len__(self) -> int:
        return len(self.raw_draws)

    # -- lookups --------------------------------------------------------
    def resource(self, rid: int) -> dict | None:
        return self.resources[rid] if 0 <= rid < len(self.resources) else None

    def res_hash(self, rid: int) -> str | None:
        r = self.resource(rid)
        return r.get("hash") if r and r.get("known") else None

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

        if t < 8:
            out["topology"] = topology_name(d.get("topo", 0))
            out["layout"] = d.get("il", -1)
            if "ib" in d:
                res, fmt, off, reg = d["ib"]
                r = self.region(reg)
                out["ib"] = {"res": res, "format": fmt, "offset": off,
                             "hash": r["hash"] if r else None,
                             "resource_hash": self.res_hash(res), "region": r}
            vbs = {}
            for slot, res, stride, off, reg in d.get("vb", []):
                r = self.region(reg)
                vbs[slot] = {"res": res, "stride": stride, "offset": off,
                             "hash": r["hash"] if r else None,
                             "resource_hash": self.res_hash(res), "region": r}
            if vbs:
                out["vb"] = vbs

        out["shaders"] = {}
        for stage, sid in zip(STAGES, d.get("sh", [])):
            if sid >= 0:
                out["shaders"][stage] = self.shader_hash(sid)

        cbs = {}
        for stage, slot, res, first, num, snap in d.get("cb", []):
            cbs[f"{STAGES[stage]}-cb{slot}"] = {"res": res, "hash": self.res_hash(res),
                                               "first_constant": first, "num_constants": num, "snap": snap}
        if cbs:
            out["cb"] = cbs

        tex = {}
        views = d.get("srvv") or []
        for n, (stage, slot, res) in enumerate(d.get("srv", [])):
            tex[f"{STAGES[stage]}-t{slot}"] = {"res": res, "hash": self.res_hash(res)}
            if n < len(views):
                tex[f"{STAGES[stage]}-t{slot}"]["view"] = self.view(views[n])
        if tex:
            out["tex"] = tex

        outs = {}
        views = d.get("outv") or []
        for n, (kind, slot, res) in enumerate(d.get("out", [])):
            name = f"o{slot}" if kind == OUT_RTV else "oD" if kind == OUT_DSV else f"u{slot}"
            outs[name] = {"res": res, "hash": self.res_hash(res)}
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
        return out

    def draws(self) -> Iterator[dict]:
        for i in range(len(self.raw_draws)):
            yield self.draw(i)
