"""Regression for helper D3D11 devices replacing the presenting device.

Run after building the probe and sandbox and copying the sandbox executable to
sandbox/run. No real-game pipe is opened: every connection names the PID of the
test process started here. The default needs no XXMI runtime. --xxmi uses only
the existing private runtime in sandbox/run/EFMI.

    python sandbox/multidevice_test.py
    python sandbox/multidevice_test.py --order after --xxmi
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path
import struct
import threading
import time

import selftest as st

EXPECTED_CALLS = [
    "Dispatch", "DrawIndexedInstanced", "DrawIndexedInstanced", "DrawIndexed",
    "DrawInstanced", "DrawIndexed", "DrawInstanced",
]
EXPECTED_INDEX_COUNTS = [864, 2400, 294, 2400]


class MultiDeviceSandbox(st.Sandbox):
    def __init__(self, extra: list[str], xxmi: bool):
        self.auxiliary_ready = threading.Event()
        self.output: deque[str] = deque(maxlen=80)
        super().__init__(extra, xxmi=xxmi)

    def _pump(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            self.output.append(line)
            if line == "READY":
                self.ready.set()
            elif line.startswith("AUXILIARY READY "):
                self.auxiliary_ready.set()


def grid_indices(n: int, fmt: str = "H") -> bytes:
    values = []
    for y in range(n):
        for x in range(n):
            a = y * (n + 1) + x
            values.extend((a, a + n + 1, a + 1, a + 1, a + n + 1, a + n + 2))
    return struct.pack(f"<{len(values)}{fmt}", *values)


def capture_checks(client: st.ProbeClient) -> tuple[list[str], list[tuple[str, ...]]]:
    doc, blob = client.capture(detail="full", cb_data=True)
    cap = st.Capture(doc, blob)
    draws = st.game_draws(cap)
    calls = [d["call"] for d in draws]
    st.check("full capture contains exactly the main-device draw sequence", calls == EXPECTED_CALLS, str(calls))
    st.check("main-device resources and shaders remain registered", bool(cap.resources) and bool(cap.shaders))
    indexed = [d for d in draws if d["args"].get("index_count", 0)]
    counts = [d["args"]["index_count"] for d in indexed]
    st.check("auxiliary DrawIndexed(3) never enters the main capture", counts == EXPECTED_INDEX_COUNTS, str(counts))
    if calls != EXPECTED_CALLS or counts != EXPECTED_INDEX_COUNTS:
        return [], []

    hashes = []
    vertex_hashes = []
    for label, draw, expected_ib in zip(
        ("shared mesh 1", "shared mesh 2", "immutable mesh", "GPU-posed mesh"),
        indexed,
        (grid_indices(12), grid_indices(20), grid_indices(7, "I"), grid_indices(20)),
    ):
        reg = draw["ib"]["region"]
        st.check(f"{label}: IB has a captured region hash", bool(reg and reg["hash"]), str(reg))
        if not reg or not reg["hash"]:
            continue
        meta, data = client.read_buffer(cap.frame, reg["res"], reg["offset"], reg["size"])
        expected_hash = st.hex32(st.crc32c(expected_ib))
        st.check(f"{label}: exact IB bytes and CRC32C match", data == expected_ib and reg["hash"] == expected_hash,
                 f"bytes={len(data)}, hash={reg['hash']}, expected={expected_hash}")
        st.check(f"{label}: IB remains unchanged", meta.get("unchanged_since_capture") is True, str(meta))
        hashes.append(reg["hash"])

        vhashes = []
        for slot, vb in draw["vb"].items():
            vreg = vb["region"]
            st.check(f"{label}: VB{slot} has a captured region hash", bool(vreg and vreg["hash"]), str(vreg))
            if not vreg or not vreg["hash"]:
                continue
            _meta, vertices = client.read_buffer(cap.frame, vreg["res"], vreg["offset"], vreg["size"])
            st.check(f"{label}: VB{slot} readback matches CRC32C", bool(vertices) and st.hex32(st.crc32c(vertices)) == vreg["hash"])
            vhashes.append(vreg["hash"])
        vertex_hashes.append(tuple(vhashes))

    mesh1 = indexed[0]
    for slot, expected in ((0, (-0.95, -0.05, 0.5)), (1, (0.5, 0.2, 0.3, 1.0))):
        vb = mesh1["vb"][slot]
        _meta, data = client.read_buffer(cap.frame, vb["res"], vb["offset"], len(expected) * 4)
        actual = struct.unpack(f"<{len(expected)}f", data)
        st.check(f"main VB{slot} holds the expected vertex, unaffected by auxiliary Map",
                 all(abs(a - b) < 1e-5 for a, b in zip(actual, expected)), str(actual))

    cb = mesh1.get("cb", {}).get("vs-cb0", {})
    raw = cap.snap_bytes(cb.get("snap", -1))
    tint = struct.unpack_from("<4f", raw, 64) if raw and len(raw) >= 80 else ()
    st.check("main CB window survives auxiliary UpdateSubresource", cb.get("first_constant") == 64
             and cb.get("num_constants") == 16 and len(tint) == 4
             and all(abs(a - b) < 1e-5 for a, b in zip(tint, (1.0, 1.0, 0.8, 1.0))), str(cb))
    t0 = mesh1.get("tex", {}).get("ps-t0")
    st.check("main texture binding survives helper work", t0 is not None)
    if t0 is not None:
        meta, pixels = client.read_texture(cap.frame, t0["res"])
        st.check("texture readback uses the main device", meta.get("width") == 256
                 and meta.get("height") == 256 and pixels[:4] == bytes((60, 0, 0, 255)), str(meta))

    # A subsequent capture checks that the presenting device stays selected.
    next_doc, next_blob = client.capture(detail="full")
    next_cap = st.Capture(next_doc, next_blob)
    later = [d for d in st.game_draws(next_cap) if d["args"].get("index_count", 0)]
    st.check("subsequent full capture advances the frame", next_cap.frame > cap.frame,
             f"{cap.frame} -> {next_cap.frame}")
    st.check("subsequent full capture preserves IB/VB hashes",
             [d["ib"]["hash"] for d in later] == hashes
             and [tuple(v["hash"] for v in d["vb"].values()) for d in later] == vertex_hashes)
    return hashes, vertex_hashes


def wait_live(client: st.ProbeClient, since: int = 0) -> tuple[int, list[dict]]:
    deadline = time.monotonic() + 5
    current, frames = since, []
    while time.monotonic() < deadline:
        current, frames = client.live_get(since=since)
        if len(frames) >= 8:
            break
        time.sleep(0.05)
    return current, frames


def live_checks(client: st.ProbeClient, expected_hashes: list[str]) -> None:
    client.live(enable=True, keep_frames=240)
    try:
        now, frames = wait_live(client)
        st.check("live stream records frames", len(frames) >= 8, str(len(frames)))
        if not frames:
            return
        tail = frames[-8:]
        nums = [f["frame"] for f in tail]
        st.check("live frame numbers advance without gaps", len(nums) >= 2
                 and all(b == a + 1 for a, b in zip(nums, nums[1:])), str(nums))
        st.check("every live frame contains all seven main-device calls",
                 all(f["game_draws"] == 7 and st.live_items(f) for f in tail),
                 str([f["game_draws"] for f in tail]))
        counts = [[it[5] for it in st.live_items(f) if it[5]] for f in tail]
        st.check("live stream excludes the auxiliary triangle", all(c == EXPECTED_INDEX_COUNTS for c in counts), str(counts))
        hashes = [[it[2] for it in st.live_items(f) if it[5]] for f in tail]
        st.check("live IB hashes match the full capture", bool(expected_hashes)
                 and all(h == expected_hashes for h in hashes), str(hashes[-1]))

        doc, _ = client.capture(detail="full")
        st.check("explicit capture during live still has main-device calls",
                 [d["call"] for d in st.game_draws(st.Capture(doc))] == EXPECTED_CALLS)
        later_now, more = wait_live(client, since=now)
        st.check("live continues advancing after explicit capture", later_now > now and len(more) >= 8,
                 f"{now} -> {later_now}, frames={len(more)}")
        st.check("live remains nonempty after explicit capture", bool(more) and all(f["game_draws"] == 7 for f in more[-8:]))
    finally:
        client.live(enable=False)


def test_order(order: str, xxmi: bool) -> None:
    print(f"\n== auxiliary device {order}, XXMI {'present' if xxmi else 'absent'}")
    extra = ["--aux-device-first"] if order == "first" else ["--aux-device-after", "5"]
    sb = MultiDeviceSandbox(extra, xxmi)
    try:
        if not sb.ready.wait(30) or not sb.auxiliary_ready.wait(10):
            st.check("main scene and auxiliary device start", False, "\n".join(sb.output))
            return
        with st.connect(sb.pid) as client:
            status = client.status()
            st.check("probe is attached to a presenting device", status["attached"] and status["swap_chain"], str(status))
            st.check("requested XXMI mode is active", status["xxmi"]["present"] == xxmi, str(status["xxmi"]))
            # The auxiliary-ready marker is printed before its first interleaved
            # frame, so allow that frame to finish before recording anything.
            time.sleep(0.2)
            hashes, _ = capture_checks(client)
            live_checks(client, hashes)
    except Exception as exc:
        st.check(f"{order}: regression completes without an exception", False, repr(exc))
        print("\n".join(sb.output))
    finally:
        sb.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order", choices=("both", "after", "first"), default="both")
    parser.add_argument("--xxmi", action="store_true", help="load the existing private sandbox/run/EFMI runtime")
    parser.add_argument("--probe", type=Path, default=st.PROBE, help="probe DLL to test (defaults to dist/efmi_probe.dll)")
    args = parser.parse_args()
    st.PROBE = args.probe.resolve()
    required = [st.RUN / "efmi_sandbox.exe", st.PROBE]
    if args.xxmi:
        required.append(st.RUN / "EFMI" / "d3d11.dll")
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("Missing test prerequisites: " + ", ".join(missing))
        return 2
    for order in ("after", "first") if args.order == "both" else (args.order,):
        test_order(order, args.xxmi)
    print(f"\n{st.passed} passed, {len(st.failures)} failed")
    for failure in st.failures:
        print("  failed:", failure)
    return 1 if st.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
