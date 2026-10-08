"""Functional self-test of the probe against the sandbox (no XXMI dump involved).

Checks that what the probe hands out is internally consistent and matches what
the sandbox is known to have created:
  * region hashes equal the CRC-32C of the bytes read back for that region
  * constant buffer snapshots contain the values the sandbox wrote
  * texture readback returns the pixels the sandbox uploaded
  * kept shader bytecode hashes to the reported shader hash
  * the continuous stream stays cached while unrelated data is streamed into
    the shared buffer, and flags a region that is rewritten every frame
  * a second client can connect after the first one leaves
  * a buffer that is destroyed and created again, at the same address or not,
    is recorded with the hash of its own bytes
  * blend, depth-stencil and rasterizer state, scissor rectangles, samplers, the
    part of a resource a view shows and the arguments of an indirect draw are
    recorded as the sandbox set them
  * the render target copied before and after a chosen draw differs exactly
    where that draw's mesh is
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import pipeline  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.draw_outputs import changed_region, matcher_of  # noqa: E402
from efmi_mcp.hashes import crc32c, fnv64, hex32, hex64  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402
from parity_test import signal  # noqa: E402

RUN = HERE / "run"
# EFMI_PROBE_DLL selects another build, e.g. one in dist/candidate while the game holds the installed DLL.
PROBE = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll")

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


class Sandbox:
    def __init__(self, extra: list[str], xxmi: bool = True):
        args = [str(RUN / "efmi_sandbox.exe"), "--probe", str(PROBE), "--seconds", "60"] + extra
        if xxmi:
            args += ["--xxmi", str(RUN / "EFMI" / "d3d11.dll")]
        self.proc = subprocess.Popen(args, cwd=str(RUN), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        self.pid = self.proc.pid
        self.ready = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            if line.strip() == "READY":
                self.ready.set()

    def quit(self) -> None:
        signal(f"efmi_sandbox_quit_{self.pid}")
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def connect(pid: int) -> ProbeClient:
    for _ in range(80):
        if any(p.pid == pid for p in find_probes()):
            try:
                return ProbeClient.connect(pid)
            except ProbeError:
                pass
        time.sleep(0.1)
    raise SystemExit("could not connect to the probe")


def game_draws(cap: Capture) -> list[dict]:
    return [d for d in cap.draws() if d["origin"] != "xxmi"]


def test_capture_and_reads(xxmi: bool) -> None:
    print(f"\n== full scene, XXMI {'present' if xxmi else 'absent'}")
    sb = Sandbox([], xxmi=xxmi)
    try:
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return
        client = connect(sb.pid)
        st = client.status()
        check("attached to the device", st["attached"] and st["swap_chain"], str(st))
        check("xxmi detection", st["xxmi"]["present"] == xxmi)
        time.sleep(0.4)

        doc, blob = client.capture(detail="full", cb_data=True)
        cap = Capture(doc, blob)
        gd = game_draws(cap)
        calls = [d["call"] for d in gd]
        check("draw sequence",
              calls == ["Dispatch", "DrawIndexedInstanced", "DrawIndexedInstanced", "DrawIndexed",
                        "DrawInstanced", "DrawIndexed", "DrawInstanced"], str(calls))
        if not xxmi:
            check("without XXMI every draw is the game's", len(gd) == len(cap))
        else:
            check("XXMI's own draws are told apart", len(cap) > len(gd), f"{len(cap)} vs {len(gd)}")

        # region hashes against the bytes themselves
        # (XXMI redraws its own text every frame from buffers it keeps rewriting:
        # those are expected to have changed by the time they are read, and the
        # probe has to say so.)
        n_regions = bad = changed = 0
        for i, (res, off, size, h, state) in enumerate(cap.regions):
            if state != "ok":
                continue
            meta, data = client.read_buffer(cap.frame, res, off, size)
            n_regions += 1
            same = hex32(crc32c(data)) == h
            if meta.get("unchanged_since_capture") is True and not same:
                bad += 1
            if not same:
                changed += 1
        check(f"{n_regions} region hashes equal crc32c of the bytes read back "
              f"({changed} rewritten in between, all reported as such)", n_regions > 8 and bad == 0, f"bad={bad}")
        if not xxmi:
            check("nothing changes between capture and read in a static scene", changed == 0, str(changed))

        # index data of mesh 1: 12x12 grid, first triangle is (0, 13, 1)
        d1 = gd[1]
        reg = d1["ib"]["region"]
        meta, data = client.read_buffer(cap.frame, reg["res"], reg["offset"], reg["size"])
        idx = struct.unpack_from("<6H", data, 0)
        check("index buffer contents", idx == (0, 13, 1, 1, 13, 14), str(idx))
        check("read reports the range as unchanged since the capture", meta.get("unchanged_since_capture") is True, str(meta))

        # vertex data through bind offset: first vertex of mesh 1
        vb0 = d1["vb"][0]
        _m, vdata = client.read_buffer(cap.frame, vb0["res"], vb0["offset"], 32)
        pos = struct.unpack_from("<3f", vdata, 0)
        check("vertex buffer contents", abs(pos[0] + 0.95) < 1e-5 and abs(pos[1] + 0.05) < 1e-5, str(pos))

        # constants as bound for each object
        ok_cb = True
        seen = []
        for obj, d in enumerate(gd[1:6]):
            cb = d["cb"]["vs-cb0"]
            raw = cap.snap_bytes(cb["snap"])
            if raw is None or len(raw) != 256:
                ok_cb = False
                break
            tint = struct.unpack_from("<4f", raw, 64)
            misc = struct.unpack_from("<4f", raw, 80)
            seen.append((round(tint[1], 3), misc[1]))
            if abs(tint[1] - (1.0 - 0.1 * obj)) > 1e-5 or misc[1] != float(obj):
                ok_cb = False
            if cb["num_constants"] != 16 or cb["first_constant"] != 16 * (4 + obj * 3):
                ok_cb = False
        check("constant buffer windows hold each object's values", ok_cb, str(seen))

        # input layout
        lay = cap.layout(d1["layout"])
        check("input layout", lay is not None and [e["semantic"] for e in lay] == ["POSITION", "NORMAL", "TEXCOORD", "COLOR"]
              and lay[3]["slot"] == 1, str(lay))

        # textures
        t0 = d1["tex"]["ps-t0"]
        meta, px = client.read_texture(cap.frame, t0["res"], mip=0)
        check("texture readback size", meta["width"] == 256 and meta["height"] == 256 and len(px) == 256 * 256 * 4, str(meta))
        check("texture pixels", tuple(px[0:4]) == (60, 0, 0, 255) and tuple(px[4 * 255:4 * 256]) == (230, 254, 0, 255),
              f"{tuple(px[0:4])} {tuple(px[4 * 255:4 * 256])}")
        meta, px = client.read_texture(cap.frame, t0["res"], mip=3)
        check("mip readback", meta["width"] == 32 and len(px) == 32 * 32 * 4, str(meta))
        cube = d1["tex"]["ps-t6"]
        meta, px = client.read_texture(cap.frame, cube["res"], mip=0, slice=2)
        check("cube face readback", meta["cube"] and px[0] == 80 and len(px) == 32 * 32 * 4, f"{meta} {px[0]}")
        bc7 = d1["tex"]["ps-t2"]
        meta, px = client.read_texture(cap.frame, bc7["res"])
        check("block-compressed readback", meta["format_name"] == "BC7_UNORM" and len(px) == 16 * 16 * 16, str(meta))
        own = {k: v for k, v in d1["tex"].items() if k in [f"ps-t{i}" for i in range(7)]}
        tex_kinds = [cap.resource(own[f"ps-t{i}"]["res"])["kind"] for i in range(7)]
        check("1D, 2D and 3D textures identified",
              tex_kinds == ["tex2d", "tex2d", "tex2d", "tex1d", "tex3d", "tex2d", "tex2d"], str(tex_kinds))
        check("every texture has a hash", all(v["hash"] for v in d1["tex"].values()))
        ramp = own["ps-t3"]
        meta, px = client.read_texture(cap.frame, ramp["res"])
        check("1D texture readback", meta["kind"] == "tex1d" and len(px) == 256 * 4 and tuple(px[4:8]) == (1, 254, 128, 255),
              f"{meta} {tuple(px[4:8])}")
        if xxmi:
            # XXMI binds its IniParams texture to t120 of every stage it uses.
            ini = d1["tex"].get("ps-t120")
            ok = ini is not None and cap.resource(ini["res"])["kind"] == "tex1d" and cap.resource(ini["res"])["origin"] == "xxmi"
            check("XXMI's IniParams texture is visible and attributed to XXMI", ok, str(ini and cap.resource(ini["res"])))
            if ok:
                meta, px = client.read_texture(cap.frame, ini["res"])
                check("IniParams can be read", meta["format_name"] == "R32G32B32A32_FLOAT" and len(px) == meta["width"] * 16, str(meta))

        # render targets: scene texture is as large as the swap chain
        rt = cap.resource(d1["out"]["o0"]["res"])
        check("render target identified", rt["kind"] == "tex2d" and [rt["w"], rt["h"]] == cap.resolution, str(rt))
        back = cap.resource(gd[6]["out"]["o0"]["res"])
        check("back buffer is reported as not created by the game", back["known"] is False, str(back))

        # who created what
        game_res = [cap.resource(v["res"]) for k, v in d1["tex"].items() if k != "ps-t120" and k != "vs-t120"]
        game_res += [cap.resource(d1["ib"]["res"])] + [cap.resource(v["res"]) for v in d1["vb"].values()]
        check("resources the game created are attributed to the game",
              all(r["origin"] == "game" for r in game_res), str([(r["kind"], r["origin"]) for r in game_res]))
        used = {sid for d in cap.raw_draws for sid in d["sh"] if sid >= 0}
        game_sh = {cap.shaders[sid]["origin"] for d in cap.raw_draws if d.get("o") != "xxmi" for sid in d["sh"] if sid >= 0}
        check("shaders the game created are attributed to the game", game_sh == {"game"}, str(game_sh))
        if xxmi:
            xxmi_sh = {cap.shaders[sid]["origin"] for d in cap.raw_draws if d.get("o") == "xxmi" for sid in d["sh"] if sid >= 0}
            check("shaders of XXMI's own draws are attributed to XXMI", xxmi_sh == {"xxmi"}, str(xxmi_sh))

        # shaders
        vs_hash = d1["shaders"]["vs"]
        info, code = client.shader(vs_hash)
        check("shader bytecode hashes to the shader hash", hex64(fnv64(code)) == vs_hash and info["type"] == "vs",
              f"{hex64(fnv64(code))} vs {vs_hash}")
        check("bytecode is DXBC", code[:4] == b"DXBC")

        # light capture
        doc2, _ = client.capture(detail="light")
        cap2 = Capture(doc2)
        g2 = game_draws(cap2)
        check("light capture keeps identities", [d.get("ib", {}).get("hash") for d in g2] == [d.get("ib", {}).get("hash") for d in gd])
        check("light capture has no bindings", all("tex" not in d and "cb" not in d for d in g2))

        client.close()
        time.sleep(0.3)
        client = connect(sb.pid)
        doc3, _ = client.capture(detail="minimal")
        check("a second client can connect and capture", len(doc3["draws"]) >= 7)
        client.close()
    finally:
        sb.quit()


def test_recreated_buffers() -> None:
    """Mod buffers are destroyed and created again at every reload, often at the same addresses."""
    print("\n== buffers destroyed and created again with other bytes")
    sb = Sandbox(["--recreate-mesh3", "12"], xxmi=False)
    try:
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return
        client = connect(sb.pid)       # a connected client is what switches the region hash cache on
        objects: dict[tuple[str, str], set[int]] = {}
        wrong, looked = [], 0
        for _ in range(60):
            doc, blob = client.capture(detail="light")
            cap = Capture(doc, blob)
            draw = next((d for d in game_draws(cap) if d["call"] == "DrawIndexed" and d["ib"]["format"] == 42), None)
            if draw is None:
                continue
            for name, binding in (("ib", draw["ib"]), ("vb0", draw["vb"][0]), ("vb1", draw["vb"][1])):
                resource, region = cap.resource(binding["res"]), binding["region"]
                _, data = client.read_buffer(cap.frame, binding["res"], offset=region["offset"], size=region["size"])
                looked += 1
                objects.setdefault((name, resource["ptr"]), set()).add(resource["sn"])
                if region["hash"] != hex32(crc32c(data)):
                    wrong.append((cap.frame, name, resource["ptr"], resource["sn"], region["hash"], hex32(crc32c(data))))
            client.release()           # as a reload does: the old buffers can then really be destroyed
            time.sleep(0.03)
        reused = sorted(key for key, serials in objects.items() if len(serials) > 1)
        check("the buffers were replaced while the test watched", len({sn for s in objects.values() for sn in s}) > 6,
              str(objects))
        check("a new buffer was given the address of a destroyed one", bool(reused), str(objects))
        check("every recorded hash is the hash of the bytes in that buffer", looked > 100 and not wrong,
              f"{len(wrong)} of {looked}: {wrong[:4]}")
    finally:
        sb.quit()


def near(a, b, tolerance: float = 1e-6) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tolerance for x, y in zip(a, b))


def test_pipeline_state() -> None:
    """What the sandbox sets for its --state-pass draw, and the defaults of its ordinary draws."""
    print("\n== fixed-function state, view parameters and indirect arguments")
    sb = Sandbox(["--state-pass"])
    try:
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return
        client = connect(sb.pid)
        features = client.status().get("features", [])
        check("the probe announces both abilities", {"pipeline_state", "rt_snapshots"} <= set(features), str(features))
        time.sleep(0.4)
        doc, blob = client.capture(detail="full")
        cap = Capture(doc, blob)
        gd = game_draws(cap)
        calls = [d["call"] for d in gd]
        check("the indirect draw is in the frame",
              calls == ["Dispatch", "DrawIndexedInstanced", "DrawIndexedInstanced", "DrawIndexed", "DrawInstanced",
                        "DrawIndexed", "DrawIndexedInstancedIndirect", "DrawIndexed", "DrawInstanced"], str(calls))
        if "DrawIndexedInstancedIndirect" not in calls:
            return
        mesh3 = next(d for d in gd if d["call"] == "DrawIndexed")
        x = next(d for d in gd if d["call"] == "DrawIndexedInstancedIndirect")

        check("indirect arguments are read from the buffer at the call's offset",
              x["indirect"]["offset"] == 20 and x["indirect"].get("args") == {
                  "index_count": mesh3["args"]["index_count"], "instance_count": 2, "first_index": 0,
                  "first_vertex": 0, "first_instance": 0}, str(x["indirect"]))

        blend = pipeline.describe_blend(x["blend"])
        check("blend state", blend["alpha_to_coverage"] is False and blend.get("all_targets") == {
            "enabled": True, "write_mask": "rgb", "src": "src_alpha", "dst": "inv_src_alpha", "op": "add",
            "src_alpha": "one", "dst_alpha": "blend_factor", "op_alpha": "max"}, str(blend))
        check("blend factor and sample mask", near(blend["blend_factor"], [0.1, 0.2, 0.3, 0.4])
              and blend["sample_mask"] == "0x0000ffff", str(blend))
        depth = pipeline.describe_depth(x["depth_stencil"])
        check("depth-stencil state and reference", depth == {
            "depth_test": True, "depth_write": False, "depth_func": "less_equal",
            "stencil": {"ref": 5, "read_mask": "0x3f", "write_mask": "0x0f",
                        "front": {"func": "not_equal", "pass": "replace", "fail": "keep", "depth_fail": "incr_sat"},
                        "back": {"func": "greater", "pass": "invert", "fail": "zero", "depth_fail": "decr"}}}, str(depth))
        raster = pipeline.describe_raster(x["rasterizer"])
        check("rasterizer state", raster == {
            "fill": "solid", "cull": "front", "front_ccw": True, "depth_bias": 3, "depth_bias_clamp": 0.5,
            "slope_scaled_depth_bias": 1.5, "depth_clip": True, "scissor": True, "multisample": False,
            "antialiased_line": False}, str(raster))
        check("scissor rectangles", x["scissors"] == [[10, 20, 120, 60], [5, 6, 7, 8]], str(x["scissors"]))
        check("viewports: the first one, their number and its depth range",
              x["viewport"] == [0, 0, 128, 64] and x["viewport_count"] == 2 and near(x["depth_range"], [0.25, 0.75]),
              f"{x.get('viewport')} {x.get('viewport_count')} {x.get('depth_range')}")

        samplers = {slot: pipeline.describe_sampler(state) for slot, state in x.get("samplers", {}).items()}
        check("samplers are listed per stage and slot", sorted(samplers) == ["ps-s0", "ps-s3", "vs-s1"], str(sorted(samplers)))
        # The anisotropy asked for with this one (4) is not part of it: the filter does not use it.
        check("comparison sampler", samplers.get("ps-s3") == {
            "filter": "comparison_min_mag_linear_mip_point", "address": "clamp/border/mirror", "lod": [1.0, 7.0],
            "lod_bias": 0.5, "comparison": "less_equal", "border_color": [1.0, 0.5, 0.25, 0.0]}, str(samplers.get("ps-s3")))
        check("anisotropic sampler of the vertex stage", samplers.get("vs-s1") == {
            "filter": "anisotropic", "address": "clamp/clamp/clamp", "lod": [0.0, 3.0], "lod_bias": 0.0,
            "max_anisotropy": 4}, str(samplers.get("vs-s1")))
        linear = samplers.get("ps-s0") or {}
        check("the scene's linear sampler", linear.get("filter") == "min_mag_mip_linear" and linear.get("address") == "wrap/wrap/wrap",
              str(linear))

        check("a view of some mips and slices of a texture array", x["tex"]["ps-t8"].get("view") == {
            "kind": "srv", "format": "R8G8B8A8_UNORM", "dimension": "tex2d_array", "most_detailed_mip": 1,
            "mip_levels": 2, "first_slice": 1, "array_size": 2}, str(x["tex"]["ps-t8"]))
        check("a view of a range of buffer elements", x["tex"]["vs-t9"].get("view") == {
            "kind": "srv", "format": "UNKNOWN", "dimension": "buffer", "first_element": 8, "num_elements": 24},
            str(x["tex"]["vs-t9"]))
        check("a render target that is one mip of its texture", x["out"]["o0"].get("view") == {
            "kind": "rtv", "format": "R8G8B8A8_UNORM", "dimension": "tex2d", "mip_slice": 1} and "oD" not in x["out"],
            str(x["out"]))
        check("the view texts name what is special",
              pipeline.view_text(x["tex"]["ps-t8"]["view"]) == "R8G8B8A8_UNORM tex2d_array mips 1..+2 slices 1..+2"
              and pipeline.view_text(x["tex"]["vs-t9"]["view"]) == "UNKNOWN buffer elements 8..+24"
              and pipeline.view_text(x["out"]["o0"]["view"]) == "R8G8B8A8_UNORM tex2d mip 1")

        # the ordinary draws: no blend state object, the scene's own depth and rasterizer state
        m1 = gd[1]
        check("without a blend state object the defaults are reported",
              m1["blend"]["state"] is None and pipeline.describe_blend(m1["blend"])["all_targets"] == {
                  "enabled": False, "write_mask": "rgba"} and pipeline.describe_blend(m1["blend"])["sample_mask"] == "0xffffffff",
              str(m1["blend"]))
        # The scene asks for "always" with the test off; a function that is not used is not reported.
        depth = pipeline.describe_depth(m1["depth_stencil"])
        check("the scene's depth state: test off", depth == {"depth_test": False, "stencil": False}, str(depth))
        raster = pipeline.describe_raster(m1["rasterizer"])
        check("the scene's rasterizer state", raster["cull"] == "none" and raster["depth_clip"] is False
              and raster["scissor"] is False and raster["front_ccw"] is False, str(raster))
        check("no scissor rectangle, one viewport over the whole target",
              m1["scissors"] == [] and m1["viewport_count"] == 1 and near(m1["depth_range"], [0.0, 1.0])
              and m1["viewport"][2:] == cap.resolution, f"{m1['scissors']} {m1['viewport_count']} {m1['viewport']}")
        check("whole-resource views of the colour and the depth target",
              m1["out"]["o0"]["view"] == {"kind": "rtv", "format": "R8G8B8A8_UNORM", "dimension": "tex2d", "mip_slice": 0}
              and m1["out"]["oD"]["view"] == {"kind": "dsv", "format": "D32_FLOAT", "dimension": "tex2d", "mip_slice": 0},
              str(m1["out"]))
        view = m1["tex"]["ps-t0"].get("view") or {}
        check("a view made without a description reports the whole texture",
              view.get("kind") == "srv" and view.get("dimension") == "tex2d" and view.get("most_detailed_mip") == 0, str(view))
        uav = gd[0]["out"]["u0"].get("view") or {}
        check("the compute shader's raw buffer view", uav.get("kind") == "uav" and uav.get("dimension") == "buffer"
              and uav.get("format") == "R32_TYPELESS" and uav.get("flags") == ["raw"] and uav.get("first_element") == 0, str(uav))

        # ---- copies around these draws: a target that is one mip of its texture, a pass without a colour target
        everything = Capture(*client.capture(detail="full", rt={"match": [{"o": "game"}], "max": 64}))
        stats = everything.stats
        check("a draw without a colour target is counted apart and does not use up the limit",
              (stats.get("rt_matched"), stats.get("rt_draws"), stats.get("rt_depth_only")) == (8, 7, 1), str(stats))
        indirect = next(d["index"] for d in game_draws(everything) if d["call"] == "DrawIndexedInstancedIndirect")
        mip = [(n, r) for n, r in enumerate(everything.rts) if r["draw"] == indirect]
        check("a target that is one mip of its texture is copied at the size of that mip",
              [(r["when"], r["state"], r.get("mip"), r.get("w"), r.get("h")) for _, r in mip]
              == [(0, "ok", 1, 128, 64), (1, "ok", 1, 128, 64)], str(mip))
        if len(mip) == 2 and all(r["state"] == "ok" for _, r in mip):
            meta, after = client.read_rt(everything.frame, mip[1][0])
            before = client.read_rt(everything.frame, mip[0][0])[1]
            count, box, _ = changed_region(before, after, meta["width"], meta["height"], meta["row_bytes"])
            # Mesh 3 covers x 3.2..38.4, y 41.6..59.2 of the 128x64 viewport and its second instance
            # is 0.18 higher in clip space (up to y 35.8); the scissor rectangle starts at x 10.
            check("the draw changed what its scissor rectangle and its viewport leave of the mesh",
                  bool(box) and all(abs(got - want) <= 1 for got, want in zip(box, [10, 36, 38, 59])) and count > 600,
                  f"{count} {box}")
        with_depth = Capture(*client.capture(detail="full", rt={"match": [{"o": "game"}], "max": 64, "depth": True}))
        depth_pass = [d["index"] for d in game_draws(with_depth) if d["call"] == "DrawIndexed"][-1]
        copies = [(n, r) for n, r in enumerate(with_depth.rts) if r["draw"] == depth_pass]
        check("with depth asked for, that draw counts and its depth target is copied",
              (with_depth.stats.get("rt_draws"), with_depth.stats.get("rt_depth_only")) == (8, 0)
              and [(r["when"], r["kind"], r["state"]) for _, r in copies] == [(0, 11, "ok"), (1, 11, "ok")],
              f"{with_depth.stats} {copies}")
        if len(copies) == 2 and all(r["state"] == "ok" for _, r in copies):
            meta, after = client.read_rt(with_depth.frame, copies[1][0])
            before = client.read_rt(with_depth.frame, copies[0][0])[1]
            count, box, _ = changed_region(before, after, meta["width"], meta["height"], meta["row_bytes"])
            check("the depth it wrote is where the mesh is in that viewport",
                  bool(box) and all(abs(got - want) <= 1 for got, want in zip(box, [3, 42, 38, 59])) and count > 500,
                  f"{count} {box}")

        light = Capture(*client.capture(detail="light"))
        check("a light capture carries none of this", all("blend" not in d and "samplers" not in d for d in light.draws())
              and not light.states and not light.views)
        client.close()
    finally:
        sb.quit()


def test_render_target_copies() -> None:
    """The render target around one draw: what was there before, what is there after."""
    print("\n== render target copies around chosen draws")
    sb = Sandbox([])
    try:
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return
        client = connect(sb.pid)
        time.sleep(0.4)
        cap = Capture(*client.capture(detail="full"))
        width, height = cap.resolution
        mesh3 = next(d for d in game_draws(cap) if d["call"] == "DrawIndexed")
        match = matcher_of(cap, mesh3)
        check("a draw is recognised by its index buffer object, range and shaders",
              set(match) == {"ib", "sn", "ic", "fi", "vs", "ps", "o"} and match["o"] == "game", str(match))

        watched = Capture(*client.capture(detail="full", rt={"match": [match]}))
        rts = watched.rts
        check("one draw matched and was copied before and after",
              watched.stats.get("rt_draws") == 1 and watched.stats.get("rt_matched") == 1
              and [(r["when"], r["state"], r["kind"], r["slot"]) for r in rts]
              == [(0, "ok", 10, 0), (1, "ok", 10, 0)], f"{watched.stats} {rts}")
        if len(rts) != 2 or any(r["state"] != "ok" for r in rts):
            return
        found = watched.draw(rts[0]["draw"])
        check("it is the draw that was asked for", found["call"] == "DrawIndexed"
              and found["ib"]["hash"] == mesh3["ib"]["hash"] and rts[1]["draw"] == rts[0]["draw"], str(found["ib"]))
        meta_before, before = client.read_rt(watched.frame, 0)
        meta_after, after = client.read_rt(watched.frame, 1)
        check("the copies are the whole target", (meta_after["width"], meta_after["height"]) == (width, height)
              and rts[1]["box"] == [0, 0, width, height] and rts[1]["boxed"] is False and len(after) == width * height * 4,
              f"{meta_after} {rts[1]}")
        count, box, _ = changed_region(before, after, width, height, meta_after["row_bytes"])
        # mesh 3 is a grid from (-0.95, -0.85) to (-0.40, -0.30) in clip space
        expected = [0.025 * width, 0.65 * height, 0.30 * width, 0.925 * height]
        check("the pixels that changed are where that mesh is",
              bool(box) and all(abs(got - want) <= 2 for got, want in zip(box, expected)), f"{box} vs {expected}")
        check("and they are most of that rectangle", bool(count) and count > 0.9 * (expected[2] - expected[0]) * (expected[3] - expected[1]),
              str(count))

        def pixel(data, x, y):
            at = (y * width + x) * 4
            return tuple(data[at:at + 4])

        centre = (int(0.16 * width), int(0.79 * height))
        clear = (13, 15, 23, 255)
        check("before the draw the place still holds the clear colour", pixel(before, *centre) == clear, str(pixel(before, *centre)))
        check("after it the mesh is there", pixel(after, *centre) != clear and pixel(after, *centre)[3] == 255, str(pixel(after, *centre)))
        other = (int(0.16 * width), int(0.2 * height))      # mesh 1, drawn earlier in the frame
        check("what was drawn earlier is in both copies", pixel(before, *other) == pixel(after, *other) != clear,
              f"{pixel(before, *other)} {pixel(after, *other)}")

        half = [0, height // 2, width // 2, height]
        boxed = Capture(*client.capture(detail="full", rt={"match": [match], "box": half}))
        meta, data = client.read_rt(boxed.frame, 1)
        check("a box limits the copy to that part of the target",
              boxed.rts[1]["box"] == half and boxed.rts[1]["boxed"] is True and (meta["width"], meta["height"]) == (width // 2, height // 2)
              and len(data) == (width // 2) * (height // 2) * 4, f"{boxed.rts[1]} {meta}")
        count_boxed, where, _ = changed_region(client.read_rt(boxed.frame, 0)[1], data, meta["width"], meta["height"], meta["row_bytes"])
        check("the change is found inside the box, at its place relative to the box",
              bool(where) and abs(where[1] + half[1] - expected[1]) <= 2 and abs(where[0] - expected[0]) <= 2, f"{where}")

        both = Capture(*client.capture(detail="full", rt={"match": [match], "all": True, "depth": True}))
        kinds = [(r["when"], r["kind"], r["state"], r.get("fmt_name")) for r in both.rts]
        check("the depth target can be copied along", kinds == [(0, 10, "ok", "R8G8B8A8_UNORM"), (0, 11, "ok", "D32_FLOAT"),
                                                              (1, 10, "ok", "R8G8B8A8_UNORM"), (1, 11, "ok", "D32_FLOAT")], str(kinds))
        meta, data = client.read_rt(both.frame, 3)
        check("and read as 32-bit depth", meta.get("format_name") == "D32_FLOAT" and len(data) == width * height * 4, str(meta))

        first = Capture(*client.capture(detail="full", rt={"match": [{"o": "game"}], "max": 1}))
        check("the limit counts draws, in frame order; how many matched is reported as well",
              first.stats["rt_draws"] == 1 and first.stats["rt_matched"] == 6 and len(first.rts) == 2
              and first.draw(first.rts[0]["draw"])["call"] == "DrawIndexedInstanced",
              f"{first.stats} {[r['draw'] for r in first.rts]}")
        after_only = Capture(*client.capture(detail="full", rt={"match": [match], "before": False}))
        check("without 'before' only the copy after the draw is made", [r["when"] for r in after_only.rts] == [1])

        blit = next(d for d in reversed(game_draws(cap)) if d["call"] == "DrawInstanced")
        by_shader = Capture(*client.capture(detail="full", rt={"match": [{"sh": blit["shaders"]["ps"]}]}))
        check("a draw without an index buffer is chosen by its shader, into the back buffer",
              [r["state"] for r in by_shader.rts] == ["ok", "ok"] and by_shader.draw(by_shader.rts[0]["draw"])["call"] == "DrawInstanced"
              and by_shader.draw(by_shader.rts[0]["draw"])["shaders"]["ps"] == blit["shaders"]["ps"], str(by_shader.rts))

        kept = Capture(*client.capture(detail="full", rt={"match": [match]}))
        client.read_rt(kept.frame, 1)
        client.release_rt(kept.frame + 1)       # not this capture: nothing happens
        check("a copy can be read more than once", len(client.read_rt(kept.frame, 1)[1]) == width * height * 4)
        client.release_rt(kept.frame)
        try:
            client.read_rt(kept.frame, 1)
            check("released copies cannot be read any more", False)
        except ProbeError as exc:
            check("released copies cannot be read any more", "released" in str(exc), str(exc))
        ib = kept.draw(kept.rts[0]["draw"])["ib"]
        _, index_data = client.read_buffer(kept.frame, ib["res"], 0, 64)
        check("the rest of that capture stays readable", len(index_data) == 64, str(len(index_data)))

        tight = Capture(*client.capture(detail="full", rt={"match": [{"o": "game"}], "max": 64, "budget_mb": 16}))
        states = [(r["when"], r["state"]) for r in tight.rts]
        left_out = [n for n, (_, state) in enumerate(states) if state == "over_budget"]
        check("copies beyond the pixel budget are listed as left out",
              bool(left_out) and tight.stats["rt_over_budget"] == len(left_out)
              and tight.stats["rt_bytes"] <= 16 * 2 ** 20, f"{states} {tight.stats}")
        # 16 MiB hold four copies of this target: two draws with both of theirs
        check("a copy from before a draw is only made when the one after it fits too",
              states[:4] == [(0, "ok"), (1, "ok"), (0, "ok"), (1, "ok")] and all(state == "over_budget" for _, state in states[4:]),
              str(states))
        states = [state for _, state in states]
        try:
            client.read_rt(tight.frame, states.index("over_budget"))
            check("a copy that was left out cannot be read", False)
        except ProbeError as exc:
            check("a copy that was left out cannot be read", "no pixels" in str(exc), str(exc))

        plain = Capture(*client.capture(detail="full"))
        check("an ordinary capture makes no copies", plain.rts == [] and "rt_draws" not in plain.stats)
        try:
            client.read_rt(tight.frame, 0)
            check("copies go with their capture", False)
        except ProbeError as exc:
            check("copies go with their capture", "no longer held" in str(exc), str(exc))
        for bad in ({"match": []}, {"match": [{}]}):
            try:
                client.capture(detail="full", rt=bad)
                check("a request that selects nothing is refused", False, str(bad))
            except ProbeError as exc:
                check("a request that selects nothing is refused", "selects no draw" in str(exc), str(exc))
        gone = Capture(*client.capture(detail="full", rt={"match": [{"draw": 9999}]}))
        check("a selection that matches no draw gives an empty list", gone.rts == [] and gone.stats["rt_draws"] == 0)
        client.close()
    finally:
        sb.quit()


def live_items(frame: dict) -> list[list]:
    return [it for it in frame["items"] if it[1] != "xxmi"]


def live_after_frames(client: ProbeClient, count: int = 5) -> tuple[int, list[dict]]:
    """Wait for rendering to progress, including any in-flight live readbacks."""
    first, frames = client.live_get()
    now = first
    deadline = time.monotonic() + 3
    while now - first < count and time.monotonic() < deadline:
        time.sleep(0.01)
        now, frames = client.live_get()
    if now - first < count:
        raise RuntimeError(f"sandbox stopped presenting: frame {first} -> {now}")
    return now, frames


def test_live(extra: list[str], label: str) -> dict | None:
    print(f"\n== continuous mode, {label}")
    sb = Sandbox(extra + ["--skip", "80"])
    try:
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return None
        client = connect(sb.pid)
        time.sleep(0.4)
        doc, _ = client.capture(detail="full")
        ref = Capture(doc)
        ref_ib = [d["ib"]["hash"] for d in game_draws(ref) if "ib" in d]

        client.live(enable=True, keep_frames=600)
        time.sleep(1.5)
        now, frames = client.live_get()
        check("frames are being recorded", len(frames) > 30, str(len(frames)))
        nums = [f["frame"] for f in frames]
        check("no frame is skipped", all(b - a == 1 for a, b in zip(nums, nums[1:])), str(nums[:8]))
        tail = frames[-20:]

        # an explicit capture in the middle of the stream
        doc, _ = client.capture(detail="full")
        check("explicit capture works while the stream is on", len(doc["draws"]) > 0)
        time.sleep(0.4)
        _, more = client.live_get(since=nums[-1])
        check("the stream resumes after it", len(more) > 5, str(len(more)))

        result = {"ref_ib": ref_ib, "tail": tail}
        client.live(enable=False)
        _, after = client.live_get()
        check("switching off clears the ring", after == [])
        _, after = live_after_frames(client)
        check("stopped ring stays empty after in-flight frames finish", after == [])

        # Start the new generation with a cadence that cannot record within
        # this test. Any frame arriving here belongs to the stopped stream.
        # Repetition exercises stop/start at different points in a render frame.
        leaked = []
        for _ in range(8):
            client.live(enable=True)
            live_after_frames(client)
            client.live(enable=False)
            client.live(enable=True, every=0xffffffff)
            _, restarted = live_after_frames(client)
            leaked.extend(f["frame"] for f in restarted)
            client.live(enable=False)
        check("rapid restart cannot publish a previous live generation", not leaked, str(leaked))

        client.live(enable=True)
        live_after_frames(client)
        client.close()
        client = connect(sb.pid)
        _, reconnected = client.live_get()
        check("reconnect starts with live off and no previous client's frames",
              not client.status()["capture"]["live"] and reconnected == [], str(reconnected))
        client.live(enable=True, every=0xffffffff)
        _, restarted = live_after_frames(client)
        check("reconnected live session rejects old in-flight frames", restarted == [], str(restarted))
        client.live(enable=True)
        _, fresh = live_after_frames(client)
        deadline = time.monotonic() + 3
        while not fresh and time.monotonic() < deadline:
            time.sleep(0.01)
            _, fresh = client.live_get()
        check("new live session still records normally after reconnect", bool(fresh))
        client.live(enable=False)
        client.close()
        return result
    finally:
        sb.quit()


def main() -> int:
    if not (RUN / "efmi_sandbox.exe").exists() or not PROBE.exists():
        print("build first: probe\\build.bat, sandbox\\build.bat, python sandbox\\prepare_run.py")
        return 2

    test_capture_and_reads(xxmi=True)
    test_capture_and_reads(xxmi=False)
    test_recreated_buffers()
    test_pipeline_state()
    test_render_target_copies()

    r = test_live(["--stream"], "unrelated data streamed into the shared buffer every frame")
    if r:
        tail = r["tail"]
        hashes = [[it[2] for it in live_items(f) if it[5]] for f in tail]
        check("index hashes present and equal to the full capture's",
              all(h == r["ref_ib"] for h in hashes), f"{hashes[-1]} vs {r['ref_ib']}")
        check("nothing is read back once cached, despite the writes next door",
              all(f["readback_bytes"] == 0 for f in tail), str([f["readback_bytes"] for f in tail[-4:]]))
        vol = [(it[1], it[5]) for it in tail[-1]["items"] if it[3] == "volatile"]
        check("no mesh of the game is treated as volatile", all(origin == "xxmi" for origin, _ in vol), str(vol))

    r = test_live(["--rewrite-mesh1"], "one mesh re-uploaded every frame")
    if r:
        last = live_items(r["tail"][-1])
        indexed = [it for it in last if it[5]]
        states = [(it[5], it[3], it[2]) for it in indexed]
        m1 = [it for it in indexed if it[5] == 864]
        others = [it for it in indexed if it[5] != 864]
        check("the rewritten mesh is flagged volatile, not hashed", bool(m1) and all(it[3] == "volatile" and it[2] is None for it in m1), str(states))
        check("the other meshes in the same buffer keep their hashes", bool(others) and all(it[2] for it in others), str(states))

    print(f"\n{passed} passed, {len(failures)} failed")
    for f in failures:
        print("  failed:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
