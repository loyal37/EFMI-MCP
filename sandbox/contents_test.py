"""Does the probe hand back what a call saw? (probe 0.1.6, capture option "contents")

The sandbox's content pass issues about thirty calls and rewrites something
between each two of them: a dynamic vertex buffer, a pool a vertex shader
reads, textures by map, update, copy, clear, GenerateMips and resolve, the
targets of draws, the buffers and textures a compute and a pixel shader write.
Every byte it writes says in which frame and at which place in the frame it was
written. This test records one frame, lets the program run on for many frames
so that everything has been overwritten again, and then asks the probe for the
contents each call saw and compares them with what the sandbox must have
written at that point, computed here independently.

Nothing outside sandbox/run is written; no key is pressed.
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

from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402
from parity_test import signal  # noqa: E402

RUN = HERE / "run"
PROBE = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll")
INPUTS = ["ib", "vb", "cb", "buf", "tex"]

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail[:700]}")


# ---------------------------------------------------------------- what the sandbox writes (sandbox.cpp: pat, fill, colour)
def fill(size: int, tag: int, fc: int) -> bytes:
    data = bytearray((tag * 131 + fc * 17 + i * 7 + (i >> 8) * 3) & 0xFF for i in range(size))
    if size >= 8:
        data[0:8] = struct.pack("<II", fc, tag)
    return bytes(data)


def colour(tag: int, fc: int) -> bytes:
    return bytes(((tag * 9 + fc) & 0xFF, (tag * 5 + 40) & 0xFF, (255 - tag * 3) & 0xFF, 255))


def patched(base: bytes, offset: int, piece: bytes) -> bytes:
    return base[:offset] + piece + base[offset + len(piece):]


def boxed(base: bytes, width: int, left: int, top: int, right: int, bottom: int, pixel: bytes) -> bytes:
    data = bytearray(base)
    for y in range(top, bottom):
        for x in range(left, right):
            data[(y * width + x) * 4:(y * width + x) * 4 + 4] = pixel
    return bytes(data)


def lcg_bytes(skip: int, count: int) -> bytes:
    """The sandbox's rnd8() sequence: `count` bytes after `skip` earlier ones."""
    state, out = 0x12345678, bytearray()
    for n in range(skip + count):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        if n >= skip:
            out.append(state >> 24)
    return bytes(out)


class Sandbox:
    def __init__(self, extra: list[str]):
        args = [str(RUN / "efmi_sandbox.exe"), "--probe", str(PROBE), "--seconds", "90",
                "--xxmi", str(RUN / "EFMI" / "d3d11.dll")] + extra
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


def connect(pid: int) -> ProbeClient | None:
    for _ in range(60):
        if any(p.pid == pid for p in find_probes()):
            try:
                return ProbeClient.connect(pid)
            except ProbeError:
                pass
        time.sleep(0.1)
    return None


class Frame:
    """One capture taken with contents, and the calls of the content pass in it by their tag.

    The calls are found by the constants they read (which holds their tag), so that takes a capture
    that keeps constant buffers. Otherwise `numbers` says which call has which tag: the scene issues
    the same calls in every frame.
    """

    def __init__(self, client: ProbeClient, numbers: dict[int, int] | None = None, **contents):
        self.client = client
        doc, blob = client.capture(detail="full", timeout_ms=8000, contents=contents)
        self.cap = Capture(doc, blob)
        self.frame = self.cap.frame
        self.calls: dict[int, dict] = {}
        self.fc = None
        self.constants: dict[int, bytes] = {}
        if numbers:
            self.calls = {tag: self.cap.draw(n) for tag, n in numbers.items()}
            vertices = self.calls[1].get("vb", {}).get(1, {}).get("content")
            if vertices is not None:      # the buffer says in which frame it was written
                self.fc = struct.unpack("<I", client.read_content(self.frame, vertices, 0, 4)[1])[0]
            return
        for d in self.cap.draws():
            cb = d.get("cb", {}).get("cs-cb2" if d["type"] >= 8 else "ps-cb2")
            if d["origin"] != "game" or not cb or cb.get("content") is None:
                continue
            _meta, data = client.read_content(self.frame, cb["content"])
            if len(data) != 32:
                continue
            tag, fc = struct.unpack_from("<II", data, 16)
            self.calls[tag] = d
            self.constants[tag] = data
            self.fc = fc if self.fc is None else self.fc
            if fc != self.fc:
                self.fc = -1          # two frames in one capture would be the probe's fault

    def read(self, content_id: int | None, offset: int = 0, size: int = 0) -> tuple[dict, bytes]:
        if content_id is None:
            raise ProbeError("the capture names no contents for that slot")
        return self.client.read_content(self.frame, content_id, offset, size)

    def slot(self, tag: int, group: str, name) -> int | None:
        return self.calls[tag].get(group, {}).get(name, {}).get("content")


def expect(frame: Frame, label: str, tag: int, group: str, name, wanted: bytes, **meta_wanted) -> None:
    try:
        meta, data = frame.read(frame.slot(tag, group, name))
    except (ProbeError, KeyError) as exc:
        check(label, False, f"{type(exc).__name__}: {exc}")
        return
    wrong = next((i for i in range(min(len(data), len(wanted))) if data[i] != wanted[i]), None)
    detail = (f"{len(data)} bytes, wanted {len(wanted)}; first difference at {wrong}: got {data[wrong:wrong + 8].hex() if wrong is not None else '-'} "
              f"wanted {wanted[wrong:wrong + 8].hex() if wrong is not None else '-'}; meta {meta}")
    ok = data == wanted and meta.get("exact") is True and all(meta.get(k) == v for k, v in meta_wanted.items())
    check(label, ok, detail)


def main() -> int:
    if not (RUN / "efmi_sandbox.exe").exists() or not PROBE.exists():
        print("build first: probe\\build.bat, sandbox\\build.bat, python sandbox\\prepare_run.py")
        return 2
    sb = Sandbox(["--content-pass", "--state-pass"])
    client = None
    try:
        if not sb.ready.wait(30):
            print("sandbox did not start")
            return 2
        client = connect(sb.pid)
        if client is None:
            print("could not connect to the probe")
            return 2
        st = client.status()
        print(f"probe {st['probe']}, features {st.get('features')}")
        if "contents" not in st.get("features", ()):
            print("this probe does not keep contents (0.1.6 is needed); set EFMI_PROBE_DLL to the build under test")
            return 2
        time.sleep(0.5)

        print("== one frame with the contents of every call")
        f = Frame(client, inputs=INPUTS, outputs=["rt", "uav"], all=True, events=True)
        cap, fc = f.cap, f.fc
        stats = cap.stats
        print(f"capture {f.frame}: {len(cap)} calls, frame counter {fc}, stats "
              f"{ {k: v for k, v in stats.items() if k.startswith('ct_') or k in ('events', 'record_ms')} }")
        check("every call of the content pass is found by the constants it read",
              sorted(f.calls) == list(range(1, 30)) and fc not in (None, -1, 0), str(sorted(f.calls)))
        check("each call saw its own version of the constant buffer",
              all(data[:16] == struct.pack("<4f", *(c / 255 for c in colour(tag, fc))) for tag, data in f.constants.items())
              and len({f.slot(tag, "cb", "cs-cb2" if tag in range(23, 28) else "ps-cb2") for tag in f.calls}) == 29)
        check("the probe says that contents are being kept", client.status()["capture"].get("contents_kept") is True)

        # Let the program run on: everything is overwritten many times before anything is asked for.
        time.sleep(0.7)
        vb = f.calls[1]["vb"][1]
        late_meta, late = client.read_buffer(f.frame, vb["res"], 0, 8)
        later = struct.unpack_from("<I", late)[0]
        check("the vertex buffer itself has been rewritten since (the frame counter in it moved on)",
              later > fc + 5 and late_meta.get("unchanged_since_capture") is False, f"{later} vs {fc}, {late_meta}")

        kept = client.status()["capture"]
        check("the status says how much has been set aside so far",
              kept.get("contents_frame") == f.frame and kept.get("contents_copies", 0) >= stats["ct_copies"]
              and kept.get("contents_bytes", 0) >= stats["ct_bytes"] > 0 and kept.get("contents_lost") == 0, str(kept))

        print("== buffers")
        v1, v2 = fill(8192, 1, fc), fill(8192, 2, fc)
        v3 = patched(v2, 4096, fill(256, 3, fc))
        expect(f, "dynamic vertex buffer as the first draw saw it (before it was mapped again)", 1, "vb", 1, v1)
        # Mapped with NO_OVERWRITE between draws 2 and 3: the writer's promise is taken at its word, so
        # both share one version, which holds the bytes added for draw 3 where draw 2 was not reading.
        expect(f, "dynamic vertex buffer at the third draw, with what a no-overwrite map added", 3, "vb", 1, v3)
        check("a no-overwrite map does not start a new version", f.slot(2, "vb", 1) == f.slot(3, "vb", 1)
              and f.slot(1, "vb", 1) != f.slot(2, "vb", 1))
        p4 = fill(4096, 4, fc)
        p5 = patched(p4, 64, fill(64, 5, fc))
        p6 = patched(p5, 256, fill(64, 6, fc))
        p7 = patched(p6, 96, fill(64, 7, fc))
        for tag, pool, what in ((4, p4, "rewritten whole"), (5, p5, "after an update of bytes 64-127"),
                                (6, p6, "after a copy into bytes 256-319"), (7, p7, "after an overlapping update of bytes 96-159")):
            expect(f, f"pool read by the vertex shader, {what}", tag, "tex", "vs-t9", pool)
        meta, part = f.read(f.slot(5, "tex", "vs-t9"), 100, 40)
        check("a part of a buffer version", part == p5[100:140] and meta.get("offset") == 100 and meta.get("size") == 40, str(meta))
        check("calls between which nothing was written share one version",
              f.slot(8, "tex", "vs-t9") == f.slot(7, "tex", "vs-t9") and f.slot(29, "tex", "vs-t9") == f.slot(7, "tex", "vs-t9")
              and len({f.slot(t, "tex", "vs-t9") for t in (4, 5, 6, 7)}) == 4)
        meta, tri = f.read(f.calls[1]["ib"]["content"])
        check("index buffer (never written: read where it is)", tri == struct.pack("<294H", *(i % 3 for i in range(294)))
              and meta.get("from") == "in place" and meta.get("exact") is True, str(meta))
        for tag in (20, 21, 22):
            words = struct.pack("<256I", *(tag * 65536 + i for i in range(256)))
            expect(f, f"buffer a pixel shader wrote, after draw {tag}", tag, "out", "u1", words)
        check("the unordered access view of a draw is listed next to its render target",
              set(f.calls[20].get("out", {})) == {"o0", "u1"}, str(f.calls[20].get("out")))
        for tag in (23, 24, 25):
            words = struct.pack("<1024I", *(tag * 0x01000000 + i for i in range(1024)))
            expect(f, f"buffer a compute shader wrote, after dispatch {tag}", tag, "out", "u0", words)

        print("== textures")
        expect(f, "mapped texture as draw 8 saw it", 8, "tex", "ps-t10", colour(8, fc) * 64, width=8, height=8)
        expect(f, "mapped texture as draw 9 saw it", 9, "tex", "ps-t10", colour(9, fc) * 64)
        expect(f, "render target sampled before it was cleared again", 10, "tex", "ps-t11", colour(10, fc) * 256)
        expect(f, "render target sampled after it was cleared again", 11, "tex", "ps-t11", colour(11, fc) * 256)
        u12 = colour(12, fc) * 64
        u13 = boxed(u12, 8, 2, 2, 6, 6, colour(13, fc))
        u14 = boxed(u13, 8, 4, 4, 8, 8, colour(9, fc))
        expect(f, "texture updated whole", 12, "tex", "ps-t12", u12)
        expect(f, "texture after an update of a box", 13, "tex", "ps-t12", u13)
        expect(f, "texture after a copy into a corner", 14, "tex", "ps-t12", u14)
        expect(f, "third mip after GenerateMips", 15, "tex", "ps-t13", colour(15, fc) * 16, mip=2, width=4, height=4)
        expect(f, "third mip while only the first was cleared", 16, "tex", "ps-t13", colour(15, fc) * 16, mip=2)
        expect(f, "third mip after GenerateMips ran again", 17, "tex", "ps-t13", colour(16, fc) * 16, mip=2)
        expect(f, "what a draw left in one slice of one mip of an array", 18, "out", "o0", colour(18, fc) * 64,
               mip=1, slice=2, width=8, height=8)
        expect(f, "what a draw left in a multisampled target, resolved", 19, "out", "o0", colour(19, fc) * 256, resolved=True)
        expect(f, "texture a multisampled target was resolved into", 28, "tex", "ps-t14", colour(19, fc) * 256)
        expect(f, "the same texture after the target was cleared and resolved again", 29, "tex", "ps-t14", colour(29, fc) * 256)
        for tag in (26, 27):
            words = struct.pack("<256I", *(tag * 1000 + i for i in range(256)))
            expect(f, f"texture a compute shader wrote, after dispatch {tag}", tag, "out", "u0", words,
                   format_name="R32_UINT", width=16, height=16)
        expect(f, "what draw 9 left in its render target", 9, "out", "o0", colour(9, fc) * 256)
        # The scene's own textures are still bound while the content pass draws.
        meta, data = f.read(f.slot(1, "tex", "ps-t0"))
        check("a texture nothing writes to is not copied: it is read where it is",
              meta.get("from") == "in place" and meta.get("width") == 256 and len(data) == 256 * 256 * 4, str(meta))

        print("== the writes between the calls")
        events = cap.events()
        number = {tag: d["index"] for tag, d in f.calls.items()}

        def before(tag: int, kind: str) -> list[dict]:
            return [e for e in events if e["type"] == kind and number[tag - 1] < e["before_call"] <= number[tag]]

        clear = before(11, "ClearRenderTargetView")
        check("a clear, with its colour", len(clear) == 1 and clear[0]["dst"] == f.calls[11]["tex"]["ps-t11"]["res"]
              and [round(v * 255) for v in clear[0]["values"]] == list(colour(11, fc)), str(clear))
        maps = [e for e in before(3, "Map") if e["dst"] == vb["res"]]
        check("a no-overwrite map of the vertex buffer", len(maps) == 1 and maps[0]["a"][1] == 5, str(maps))
        update = before(5, "UpdateSubresource")
        check("an update with its range", len(update) == 1 and update[0]["a"] == [0, 64, 64, 1]
              and update[0]["dst"] == f.calls[5]["tex"]["vs-t9"]["res"], str(update))
        copies = before(6, "CopySubresourceRegion")
        check("a copy with its destination range and its source", len(copies) == 1 and copies[0]["a"][:3] == [0, 256, 64]
              and copies[0]["dst"] == f.calls[6]["tex"]["vs-t9"]["res"] and copies[0]["src"] >= 0, str(copies))
        check("GenerateMips and resolves are listed",
              len([e for e in events if e["type"] == "GenerateMips"]) == 2
              and len([e for e in events if e["type"] == "ResolveSubresource"]) == 2, str({e["type"] for e in events}))
        check("the events are in call order", [e["before_call"] for e in events] == sorted(e["before_call"] for e in events))

        print("== state that came with 0.1.6")
        multi = next((d for d in cap.draws() if d.get("viewport_count", 0) == 2), None)
        check("every viewport of a draw", multi is not None and multi.get("viewports") == [
            [0, 0, 128, 64, 0.25, 0.75], [8, 16, 64, 32, 0, 1]], str(multi and multi.get("viewports")))

        print("== textures that could not be read before")
        volume = f.calls[1].get("tex", {}).get("ps-t4") or next(
            (d["tex"]["ps-t4"] for d in cap.draws() if "ps-t4" in d.get("tex", {})), None)
        try:
            meta, data = client.read_texture(f.frame, volume["res"])
            # rnd8() order in the sandbox: 8192 bytes of BC1, 4096 of BC7, then the volume.
            check("a 3D texture, every depth slice", data == lcg_bytes(8192 + 4096, 4096) and meta.get("kind") == "tex3d"
                  and meta.get("depth") == 16 and meta.get("rows") == 16, str(meta))
        except (ProbeError, TypeError) as exc:
            check("a 3D texture, every depth slice", False, str(exc))
        try:
            meta, data = client.read_texture(f.frame, f.calls[19]["out"]["o0"]["res"])
            check("a multisampled texture, resolved", meta.get("resolved") is True and meta.get("samples") == 4
                  and len(data) == 1024 and len(set(data[i:i + 4] for i in range(0, 1024, 4))) == 1
                  and data[1:4] == colour(29, 0)[1:4], f"{meta} {data[:8].hex()}")
        except ProbeError as exc:
            check("a multisampled texture, resolved", False, str(exc))

        print("== a part of the calls, a small budget, letting go")
        g = Frame(client, numbers=number, inputs=["vb"], outputs=["rt"], match=[{"first": number[9], "last": number[10]}])
        time.sleep(0.4)
        check("the scene issues the same calls in every frame",
              all(g.calls[tag]["call"] == f.calls[tag]["call"] and g.calls[tag]["shaders"] == f.calls[tag]["shaders"]
                  for tag in number))
        check("outputs only of the calls that were asked for",
              g.cap.stats.get("ct_out_calls") == 2 and g.slot(9, "out", "o0") is not None and g.slot(8, "out", "o0") is None
              and g.slot(11, "out", "o0") is None, str(g.cap.stats))
        check("inputs only of the kinds that were asked for",
              g.slot(4, "tex", "vs-t9") is None and g.slot(8, "tex", "ps-t10") is None and g.slot(1, "vb", 1) is not None
              and g.slot(1, "cb", "ps-cb2") is None)
        expect(g, "what the selected draw left in its target", 9, "out", "o0", colour(9, g.fc) * 256)
        try:
            f.read(f.slot(1, "vb", 1))
            check("the contents of the capture before are gone once a new one is taken", False)
        except ProbeError as exc:
            check("the contents of the capture before are gone once a new one is taken", "capture again" in str(exc), str(exc))

        small = Frame(client, inputs=INPUTS, budget_mb=0.004)      # 4 KiB: less than the vertex buffer
        time.sleep(0.5)
        try:
            small.read(small.slot(1, "vb", 1))
            check("what does not fit the budget is refused, not made up", False)
        except ProbeError as exc:
            check("what does not fit the budget is refused, not made up", "budget" in str(exc), str(exc))
        try:
            meta, tri = small.read(small.calls[1]["ib"]["content"])
            check("what needs no copy is still there", tri[:6] == struct.pack("<3H", 0, 1, 2), str(meta))
        except ProbeError as exc:
            check("what needs no copy is still there", False, str(exc))
        client.release_content(small.frame)
        try:
            small.read(small.calls[1]["ib"]["content"])
            check("released contents say so", False)
        except ProbeError as exc:
            check("released contents say so", "released" in str(exc), str(exc))
        after = client.status()["capture"]
        check("nothing is being watched after the release", after.get("contents_kept") is False and after.get("contents_bytes") == 0,
              str(after))
        meta, data = client.read_buffer(small.frame, small.calls[1]["ib"]["res"], 0, 6)
        check("the capture itself is still readable after its contents were released", data == struct.pack("<3H", 0, 1, 2))

        print("== strict: a no-overwrite map starts a new version too")
        s = Frame(client, numbers=number, inputs=["vb"], strict=True)
        time.sleep(0.5)
        expect(s, "the vertex buffer exactly as the second draw had it", 2, "vb", 1, fill(8192, 2, s.fc))
        expect(s, "and as the third draw had it", 3, "vb", 1, patched(fill(8192, 2, s.fc), 4096, fill(256, 3, s.fc)))

        print("== an ordinary capture afterwards")
        doc, blob = client.capture(detail="full", cb_data=True, timeout_ms=8000)
        plain = Capture(doc, blob)
        check("a capture without contents has none and switches the watching off",
              not plain.has_contents and all(d.get("ib", {}).get("content") is None for d in plain.draws())
              and client.status()["capture"].get("contents_kept") is False)
    finally:
        if client:
            client.close()
        sb.quit()

    print(f"\n{passed} passed, {len(failures)} failed")
    for name in failures:
        print("  failed:", name)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
