"""End-to-end test of the MCP server: real JSON-RPC over stdio, real probe, sandbox scene.

Starts the real server entry point over stdio and calls every tool, checking
the answers against the sandbox scene. Existing game probes are hidden only
inside this test's server process so a running user game is never selected.
"""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from parity_test import signal  # noqa: E402
from efmi_mcp.probe_client import find_probes  # noqa: E402

RUN = HERE / "run"
# EFMI_PROBE_DLL selects another build, e.g. one in dist/candidate while the game holds the installed DLL.
PROBE = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll")
OUT = RUN / "mcp-out"

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}\n       {detail[:600]}")


class Mcp:
    def __init__(self, efmi_root: Path):
        # This test starts its own sandbox after the MCP server. Keep existing
        # game sessions invisible to the test, including its "no game" checks.
        # The filter is scoped to this child process, never the production server.
        existing = [endpoint.pid for endpoint in find_probes()]
        bootstrap = (
            "from efmi_mcp import probe_client\n"
            "discover = probe_client.find_probes\n"
            f"existing = {existing!r}\n"
            "probe_client.find_probes = lambda: [p for p in discover() if p.pid not in existing]\n"
            "from efmi_mcp.__main__ import main\n"
            "main()\n"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", bootstrap, "--efmi-root", str(efmi_root), "--out", str(OUT), "--probe-dll", str(PROBE)],
            cwd=str(ROOT / "server"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 1
        self.stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.decode("utf-8", "replace").rstrip())

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self.next_id
            self.next_id += 1
        self.proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed its output\n" + "\n".join(self.stderr[-20:]))
        return json.loads(line.decode("utf-8"))

    def call(self, name: str, **args) -> tuple[str, bool, list[dict]]:
        r = self.send("tools/call", {"name": name, "arguments": args})
        res = r["result"]
        text = "\n".join(c["text"] for c in res["content"] if c["type"] == "text")
        return text, bool(res.get("isError")), res["content"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class Sandbox:
    def __init__(self, extra: list[str]):
        args = [str(RUN / "efmi_sandbox.exe"), "--xxmi", str(RUN / "EFMI" / "d3d11.dll"), "--probe", str(PROBE),
                "--seconds", "90"] + extra
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


def main() -> int:
    verbose = "-v" in sys.argv
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)

    # a small "mod" in the sandbox copy, so hash-to-mod matching has something to find
    mod_dir = RUN / "EFMI" / "Mods" / "SandboxTestMod"
    mod_dir.mkdir(parents=True, exist_ok=True)

    mcp = Mcp(RUN / "EFMI")
    sb = None
    try:
        print("== protocol")
        r = mcp.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "mcp_test", "version": "0"}})
        check("initialize", r["result"]["protocolVersion"] == "2025-06-18" and "tools" in r["result"]["capabilities"], str(r))
        check("instructions are offered", len(r["result"].get("instructions", "")) > 200)
        mcp.send("notifications/initialized", notify=True)
        r = mcp.send("ping")
        check("ping", r.get("result") == {})
        r = mcp.send("tools/list")
        tools = {t["name"]: t for t in r["result"]["tools"]}
        check("tools/list", len(tools) >= 15 and all("inputSchema" in t for t in tools.values()), str(list(tools)))
        r = mcp.send("no/such/method")
        check("unknown method is a JSON-RPC error", r.get("error", {}).get("code") == -32601, str(r))
        r = mcp.send("tools/call", {"name": "efmi_nope", "arguments": {}})
        check("unknown tool is rejected", "error" in r, str(r))

        print("== without the game")
        text, err, _ = mcp.call("efmi_status")
        check("status explains that the probe is not loaded", "not loaded" in text and "Inject Libraries" in text, text)
        text, err, _ = mcp.call("efmi_capture_frame")
        check("capture without the game is a clear tool error", err and "Inject Libraries" in text, text)
        text, err, _ = mcp.call("efmi_list_mods")
        check("mods can be listed offline", not err and "SandboxTestMod" not in text or "mods" in text, text)

        print("== with the sandbox running")
        sb = Sandbox([])
        if not sb.ready.wait(30):
            check("sandbox starts", False)
            return 1
        time.sleep(1.0)
        text, err, _ = mcp.call("efmi_status")
        if verbose: print(text)
        check("status with the game", not err and "attached: True" in text and "1280x720" in text and "xxmi: present" in text, text)
        check("status says that the probe can press the reload key and the mods switch",
              "the probe presses F10" in text and "the probe presses F11" in text, text)
        check("status has no warning about hashes kept by buffer address", "destroyed buffer" not in text, text)

        text, err, _ = mcp.call("efmi_capture_frame", detail="full", cb_data=True)
        if verbose: print(text)
        check("capture_frame", not err and "7 from the game" in text and "distinct index-buffer hashes" in text, text)
        frame = int(text.split()[1])

        text, err, _ = mcp.call("efmi_list_draws", origin="game")
        if verbose: print(text)
        lines = [l for l in text.splitlines() if l.startswith("#")]
        check("list_draws shows the game's seven draws", not err and len(lines) == 7, text)
        ib_line = next(l for l in lines if "DrawIndexedInstanced" in l and "ic=864" in l)
        ib_hash = ib_line.split("ib=")[1].split()[0]
        draw_idx = int(ib_line.split()[0][1:])
        vs_hash = ib_line.split("vs=")[1].split()[0]
        check("draw line carries hashes", len(ib_hash) == 8 and len(vs_hash) == 16, ib_line)

        # write a mod ini that refers to that hash, then see it being matched
        (mod_dir / "mod.ini").write_text(
            "namespace = SandboxTestMod\n\n[TextureOverrideSandboxMesh]\nhash = %s\nmatch_index_count = 864\n" % ib_hash,
            encoding="utf-8")
        time.sleep(5.5)             # the mod index is refreshed every few seconds
        text, err, _ = mcp.call("efmi_find_hash", hash=ib_hash)
        if verbose: print(text)
        check("find_hash: mod reference and role", "SandboxTestMod" in text and "index buffer (region hash)" in text
              and "match_index_count=864" in text, text)
        text, err, _ = mcp.call("efmi_list_draws", ib_hash=ib_hash)
        check("list_draws filter by ib hash, mod noted", "1 matching" in text and "[mod: SandboxTestMod]" in text, text)
        text, err, _ = mcp.call("efmi_list_mods")
        check("list_mods", "SandboxTestMod: 1 ini, 1 hashes, namespace SandboxTestMod" in text, text)
        text, err, _ = mcp.call("efmi_list_draws", hash=vs_hash, limit=3)
        check("list_draws by any hash, with paging", "more (use offset=3)" in text, text)

        text, err, _ = mcp.call("efmi_get_draw", draw=draw_idx)
        if verbose: print(text)
        d = json.loads(text)
        check("get_draw", d["ib"]["hash"] == ib_hash and d["ib"]["format"] == "R16_UINT" and d["vb"]["vb0"]["stride"] == 32
              and d["constant_buffers"]["vs-cb0"]["num_constants"] == 16 and d["textures"]["ps-t2"].split()[2] == "64x64"
              and d["input_layout"][0].startswith("POSITION R32G32B32_FLOAT") and d["args"]["instance_count"] == 4, text)

        check("get_draw: fixed-function state of an ordinary draw",
              d.get("blend", {}).get("default") is True and d["blend"]["all_targets"] == {"enabled": False, "write_mask": "rgba"}
              and d["blend"]["sample_mask"] == "0xffffffff" and d["depth_stencil"] == {"depth_test": False, "stencil": False}
              and d["rasterizer"]["cull"] == "none" and d["rasterizer"]["scissor"] is False and "scissor_rects" not in d
              and d["viewport"] == [0, 0, 1280, 720] and d["viewport_depth_range"] == [0, 1] and "viewport_count" not in d
              and "pipeline_state" not in d, text)
        check("get_draw: samplers", list(d.get("samplers", {})) == ["ps-s0"] and d["samplers"]["ps-s0"]["filter"] == "min_mag_mip_linear"
              and d["samplers"]["ps-s0"]["address"] == "wrap/wrap/wrap", str(d.get("samplers")))
        check("get_draw: views of whole textures are not mentioned",
              all("| view:" not in line for line in list(d["textures"].values()) + list(d["outputs"].values()))
              and set(d["outputs"]) == {"o0", "oD"}, f"{d['textures']} {d['outputs']}")

        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="ib", count=2)
        b = json.loads(text)
        check("read_buffer ib", b["indices"] == [0, 13, 1, 1, 13, 14] and b["index_count"] == 864 and b["max"] == 168
              and b["unchanged_since_capture"] is True, text)
        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="vb0", count=2, save=True)
        b = json.loads(text)
        v0 = b["elements"][0]
        check("read_buffer vb0 decoded with the layout", b["element_count"] == 169 and abs(v0["POSITION"][0] + 0.95) < 1e-4
              and v0["NORMAL"] == [0.0, 0.0, 1.0] and Path(b["saved"]).stat().st_size == 169 * 32, text)
        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="vb1", count=1)
        b = json.loads(text)
        check("read_buffer vb1", len(b["elements"][0]["COLOR"]) == 4, text)
        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="vs-cb0", first=4, count=2)
        b = json.loads(text)
        check("read_buffer constants from the capture", b["values"]["c4"] == [1.0, 1.0, 0.8, 1.0] and "from the capture" in b["source"], text)
        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="gs-cb9")
        check("read_buffer bad slot is a tool error", err and "nothing bound" in text, text)

        text, err, content = mcp.call("efmi_read_texture", draw=draw_idx, slot="ps-t0")
        if verbose: print(text)
        imgs = [c for c in content if c["type"] == "image"]
        check("read_texture: info, files and a preview image", not err and "256x256 R8G8B8A8_UNORM_SRGB" in text and len(imgs) == 1
              and imgs[0]["mimeType"] == "image/png" and len(imgs[0]["data"]) > 500, text)
        text, err, content = mcp.call("efmi_read_texture", draw=draw_idx, slot="ps-t2")
        check("read_texture: BC7 converted", not err and "BC7_UNORM" in text and ".png" in text, text)
        text, err, content = mcp.call("efmi_read_texture", draw=draw_idx, slot="o0")
        check("read_texture: render target", not err and "1280x720" in text and any(c["type"] == "image" for c in content), text)

        text, err, _ = mcp.call("efmi_get_shader", hash=vs_hash, max_lines=200)
        if verbose: print(text)
        check("get_shader disassembly", not err and "vs_5_0" in text and "dcl_input" in text, text)

        text, err, _ = mcp.call("efmi_export_mesh", draw=draw_idx)
        m = json.loads(text)
        obj = Path(m["obj"]).read_text().splitlines() if m.get("obj") else []
        check("export_mesh", m["vertices"] == 169 and m["triangles"] == 288 and sum(l.startswith("v ") for l in obj) == 169
              and sum(l.startswith("f ") for l in obj) == 288 and (Path(m["folder"]) / "mesh.json").exists(), text)

        print("== continuous mode")
        text, err, _ = mcp.call("efmi_live_now")
        check("live_now before start is a clear error", err and "efmi_live_start" in text, text)
        text, err, _ = mcp.call("efmi_live_start")
        check("live_start", not err and "continuous mode on" in text, text)
        time.sleep(0.8)
        text, err, _ = mcp.call("efmi_live_now")
        if verbose: print(text)
        check("live_now", not err and f"ib={ib_hash}" in text and "inst=4" in text and "[mod: SandboxTestMod]" in text, text)
        text, err, _ = mcp.call("efmi_watch_hash", hash=ib_hash, seconds=0.8)
        if verbose: print(text)
        first = text.splitlines()[0]
        n_frames = int(first.split()[1])
        n_seen = int(first.split("drawn in ")[1].split()[0])
        check("watch_hash sees the mesh in every frame", not err and n_frames > 10 and n_seen == n_frames and "instances per frame: 4-4" in text, text)
        text, err, _ = mcp.call("efmi_watch_hash", hash="deadbeef", seconds=0.3)
        check("watch_hash for an absent hash", "drawn in 0 of them" in text, text)
        text, err, _ = mcp.call("efmi_live_stop")
        check("live_stop", "off" in text)

        print("== comparing")
        text, err, _ = mcp.call("efmi_capture_frame", detail="light")
        frame2 = int(text.split()[1])
        text, err, _ = mcp.call("efmi_diff_captures", a=frame, b=frame2)
        if verbose: print(text)
        check("diff of two captures of a static scene", not err and f"only in {frame}: 0" in text and f"only in {frame2}: 0" in text, text)
        text, err, _ = mcp.call("efmi_get_draw", draw=draw_idx, capture=frame)
        check("older capture still answers", not err and json.loads(text)["capture"] == frame, text)
        text, err, _ = mcp.call("efmi_read_buffer", draw=draw_idx, slot="ib", capture=frame)
        check("reading from a superseded capture is refused clearly", err and "capture again" in text, text)

        print("== what a draw paints")
        check("the tool is listed with its selection arguments",
              {"hash", "draws", "max_draws", "depth", "box", "targets"} <= set(tools["efmi_capture_draw_outputs"]["inputSchema"]["properties"]))

        def changed_box(report: str, number: int = 0) -> list[int]:
            return json.loads(report.split(" within ")[number + 1].split(" (left")[0])

        def saved(report: str) -> list[Path]:
            return [Path(line.split(": ", 1)[1]) for line in report.splitlines()
                    if line.strip().startswith(("before: ", "after: ", "what it touched: "))]

        # The latest capture is a light one: enough to find the mesh again.
        text, err, content = mcp.call("efmi_capture_draw_outputs", hash=ib_hash)
        if verbose: print(text)
        images = [c for c in content if c["type"] == "image"]
        check("capture_draw_outputs by index buffer hash", not err and "draws that matched: 1; targets copied for 1 of them" in text
              and f"ib={ib_hash} ic=864 [mod: SandboxTestMod]" in text and "o0: " in text and "1280x720 R8G8B8A8_UNORM" in text
              and "preview image: 1" in text and len(images) == 1 and images[0]["mimeType"] == "image/png", text)
        if not err and " within " in text:
            # Mesh 1 is a grid from (-0.95, -0.05) of size 0.55, drawn four times, each 0.18 higher: it reaches the top edge.
            box = changed_box(text)
            check("the changed area is where the four instances of that mesh are",
                  all(abs(got - want) <= 2 for got, want in zip(box, [32, 0, 384, 378])), str(box))
            files = saved(text)
            check("before, after and the marked picture are saved", len(files) == 3 and all(f.stat().st_size > 2000 for f in files)
                  and {f.name.split("-")[-1] for f in files} == {"before.png", "after.png", "changed.png"}, str(files))
        outputs_frame = int(text.split()[1].rstrip(":"))
        new_idx = int(next(l for l in text.splitlines() if l.startswith("#")).split()[0][1:])
        text, err, _ = mcp.call("efmi_read_buffer", draw=new_idx, slot="ib", count=2)
        check("the frame it recorded is the latest capture and stays readable after the copies are released",
              not err and json.loads(text)["indices"] == [0, 13, 1, 1, 13, 14] and json.loads(text)["capture"] == outputs_frame, text)
        text, err, _ = mcp.call("efmi_status")
        check("status has no warning about an old probe", not err and "need probe 0.1.5" not in text, text)

        text, err, content = mcp.call("efmi_capture_draw_outputs", draws=[draw_idx], capture=frame, box=[0, 360, 640, 720],
                                      preview=False)
        if verbose: print(text)
        check("capture_draw_outputs by draw number of an older capture, limited to a box",
              not err and f"draws [{draw_idx}] of capture {frame}" in text and "targets copied for 1 of them" in text
              and not [c for c in content if c["type"] == "image"], text)
        if not err and " within " in text:
            box = changed_box(text)
            check("the changed area is given in pixels of the whole target", all(abs(got - want) <= 2 for got, want in
                                                                               zip(box, [32, 360, 384, 378])), str(box))

        ps_hash = ib_line.split("ps=")[1].split()[0]
        text, err, content = mcp.call("efmi_capture_draw_outputs", hash=ps_hash, max_draws=2, depth=True, targets="all")
        if verbose: print(text)
        shown = [l for l in text.splitlines() if l.startswith("#")]
        check("capture_draw_outputs by shader hash: the limit, and the depth target along",
              not err and "draws that matched: 5; targets copied for 2 of them (the first 2 in frame order" in text
              and len(shown) == 2 and text.count("    oD: ") == 2 and text.count("D32_FLOAT") == 2
              and text.count("    o0: ") == 2 and "recording cost inside the game: " in text
              and text.count("changed nothing in this target (depth writes are off") == 2
              and "Depth pictures are scaled" in text, text)
        check("previews are returned for the colour targets that changed", len([c for c in content if c["type"] == "image"]) == 2,
              str(len(content)))

        text, err, _ = mcp.call("efmi_capture_draw_outputs", hash="deadbeef")
        check("an index buffer hash that is not in the capture is a clear error", err and "no draw of capture" in text, text)
        text, err, _ = mcp.call("efmi_capture_draw_outputs")
        check("a call without a selection is a clear error", err and "give draws" in text, text)
        text, err, _ = mcp.call("efmi_capture_draw_outputs", hash=ib_hash, box=[10, 10, 5, 20])
        check("a box that is not one is a clear error", err and "box must be" in text, text)

        print("== after the game exits")
        sb.quit()
        sb = None
        time.sleep(0.5)
        text, err, _ = mcp.call("efmi_capture_frame")
        check("capture after exit is a clear error", err, text)
        text, err, _ = mcp.call("efmi_list_draws", capture=frame, limit=2)
        check("captures in memory stay usable", not err and "#" in text, text)

        print("== a second game session, with every kind of fixed-function state in use")
        sb = Sandbox(["--state-pass"])
        if not sb.ready.wait(30):
            check("second sandbox starts", False)
            return 1
        time.sleep(1.0)
        text, err, _ = mcp.call("efmi_capture_frame", detail="full")
        check("the server finds the new game process", not err and "9 from the game" in text, text)
        text, err, _ = mcp.call("efmi_list_draws", origin="game")
        lines = [l for l in text.splitlines() if l.startswith("#")]
        x_line = next((l for l in lines if "DrawIndexedInstancedIndirect" in l), "")
        check("the indirect draw is listed", bool(x_line), text)
        if x_line:
            text, err, _ = mcp.call("efmi_get_draw", draw=int(x_line.split()[0][1:]))
            if verbose: print(text)
            d = json.loads(text)
            check("get_draw: the arguments an indirect draw read from its buffer",
                  d["indirect_args"]["offset"] == 20 and d["indirect_args"].get("args") == {
                      "index_count": 294, "instance_count": 2, "first_index": 0, "first_vertex": 0, "first_instance": 0}, text)
            check("get_draw: blend state", d["blend"]["all_targets"] == {
                "enabled": True, "write_mask": "rgb", "src": "src_alpha", "dst": "inv_src_alpha", "op": "add",
                "src_alpha": "one", "dst_alpha": "blend_factor", "op_alpha": "max"}
                and d["blend"]["sample_mask"] == "0x0000ffff" and "default" not in d["blend"]
                and [round(v, 3) for v in d["blend"]["blend_factor"]] == [0.1, 0.2, 0.3, 0.4], str(d["blend"]))
            check("get_draw: depth-stencil state", d["depth_stencil"]["depth_func"] == "less_equal"
                  and d["depth_stencil"]["depth_write"] is False and d["depth_stencil"]["stencil"]["ref"] == 5
                  and d["depth_stencil"]["stencil"]["front"] == {"func": "not_equal", "pass": "replace", "fail": "keep",
                                                                 "depth_fail": "incr_sat"}, str(d["depth_stencil"]))
            check("get_draw: rasterizer state with its scissor rectangles", d["rasterizer"]["cull"] == "front"
                  and d["rasterizer"]["front_ccw"] is True and d["rasterizer"]["depth_bias"] == 3
                  and d["scissor_rects"] == [[10, 20, 120, 60], [5, 6, 7, 8]], f"{d['rasterizer']} {d.get('scissor_rects')}")
            check("get_draw: viewports", d["viewport"] == [0, 0, 128, 64] and d["viewport_count"] == 2
                  and d["viewport_depth_range"] == [0.25, 0.75], f"{d['viewport']} {d.get('viewport_count')} {d['viewport_depth_range']}")
            check("get_draw: samplers per stage", d["samplers"]["ps-s3"]["comparison"] == "less_equal"
                  and d["samplers"]["ps-s3"]["address"] == "clamp/border/mirror" and d["samplers"]["vs-s1"]["max_anisotropy"] == 4,
                  str(d["samplers"]))
            check("get_draw: views that show a part of their resource say so",
                  d["textures"]["ps-t8"].endswith("| view: R8G8B8A8_UNORM tex2d_array mips 1..+2 slices 1..+2")
                  and d["textures"]["vs-t9"].endswith("| view: UNKNOWN buffer elements 8..+24")
                  and d["outputs"]["o0"].endswith("| view: R8G8B8A8_UNORM tex2d mip 1")
                  and "| view:" not in d["textures"]["ps-t0"], f"{d['textures']} {d['outputs']}")

            print("== buffers behind resource views")
            xd = int(x_line.split()[0][1:])
            text, err, _ = mcp.call("efmi_read_buffer", draw=xd, slot="vs-t9", count=2)
            b = json.loads(text) if not err else {}
            check("read_buffer: a buffer bound as a shader resource, by the elements of its view",
                  b.get("element_bytes") == 16 and b.get("elements") == 24 and b.get("first_element_of_buffer") == 8
                  and list(b.get("values", {})) == ["e0", "e1"] and b.get("exact") is True
                  and b.get("view") == "UNKNOWN buffer elements 8..+24", text)
            text, err, _ = mcp.call("efmi_read_buffer", draw=xd, slot="vs-t9", whole=True, save=True, count=1)
            b = json.loads(text) if not err else {}
            check("read_buffer: the whole buffer behind a view",
                  b.get("elements") == 64 and b.get("first_element_of_buffer") == 0
                  and Path(b.get("saved", "missing")).stat().st_size == 1024, text)
            text, err, _ = mcp.call("efmi_read_buffer", draw=xd, slot="ps-t8")
            check("read_buffer: a texture slot names the tool to use", err and "efmi_read_texture" in text, text)
            text, err, _ = mcp.call("efmi_read_buffer", draw=xd, slot="ib", count=1)
            b = json.loads(text) if not err else {}
            check("read_buffer: the indices of an indirect draw, by the count it read from its buffer",
                  b.get("index_count") == 294 and len(b.get("indices", [])) == 3, text)
            text, err, _ = mcp.call("efmi_read_buffer", draw=xd, slot="vs-cb0", whole=True, count=1)
            b = json.loads(text) if not err else {}
            check("read_buffer: a whole constant buffer, with the window that was bound",
                  b.get("registers") == 4096 and b.get("bound_window") == {"first_constant": 160, "num_constants": 16}, text)

        compute = next((l for l in lines if l.split()[1] == "Dispatch"), "")
        text, err, _ = mcp.call("efmi_read_buffer", draw=int(compute.split()[0][1:]) if compute else 0, slot="u0", count=3)
        b = json.loads(text) if not err else {}
        check("read_buffer: what a compute shader wrote through an unordered access view",
              bool(compute) and b.get("kind") == "unordered access view" and b.get("element_bytes") == 4
              and b.get("elements") == 441 * 8 and [round(b["values"][k][0], 4) for k in ("e0", "e1", "e2")] == [0.35, -0.85, 0.5]
              and "source" in b, text)

        mesh3 = next((l for l in lines if l.split()[1] == "DrawIndexed" and "ic=294" in l), "")
        if mesh3:
            ib3 = mesh3.split("ib=")[1].split()[0]
            text, err, _ = mcp.call("efmi_capture_draw_outputs", hash=ib3, preview=False)
            if verbose: print(text)
            # The indirect draw of the same buffer has no region hash (its index count is not known when it
            # is issued), so the hash selects the two DrawIndexed calls: the scene's and the depth pass.
            check("a pass without a colour target is named and not counted",
                  not err and "1 matching draw has no colour target" in text and "depth=true" in text and "    oD: " not in text
                  and "draws that matched: 2; targets copied for 1 of them;" in text, text)
            text, err, content = mcp.call("efmi_capture_draw_outputs", hash=ib3, depth=True)
            if verbose: print(text)
            depth_only = text.split("\n#")[-1]
            check("with depth=true its depth target is copied and the written area found",
                  not err and "no colour target" not in text and "draws that matched: 2; targets copied for 2 of them;" in text
                  and "    oD: " in depth_only and "    o0: " not in depth_only and " within [3, 42, 38, 59]" in depth_only
                  and len([c for c in content if c["type"] == "image"]) == 2, text)
        else:
            check("mesh 3 is listed", False, text)
        sb.quit()
        sb = None
    finally:
        if sb:
            sb.quit()
        mcp.close()
        shutil.rmtree(mod_dir, ignore_errors=True)

    print(f"\n{passed} passed, {len(failures)} failed")
    for f in failures:
        print("  failed:", f)
    if failures and mcp.stderr:
        print("server stderr:\n  " + "\n  ".join(mcp.stderr[-25:]))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
