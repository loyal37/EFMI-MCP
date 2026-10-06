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
"""
from __future__ import annotations

import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import crc32c, fnv64, hex32, hex64  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402
from parity_test import signal  # noqa: E402

RUN = HERE / "run"
PROBE = ROOT / "dist" / "efmi_probe.dll"

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
