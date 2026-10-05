"""Measures what the probe costs, on a frame with a game-like number of draws.

The sandbox renders without vsync, so its frame rate is limited by CPU work per
frame; any time the probe adds shows up directly as a lower frame rate.

Two runs of the same scene are compared window by window, at the same number of
seconds after start-up (the scene's own cost drifts over the first seconds):

  reference run   XXMI only, the probe DLL is never loaded
  probe run       window 1: probe loaded, nobody connected
                  window 2: a client is connected, not capturing
                  window 3: continuous mode (every frame recorded)
                  window 4: full-state captures requested back to back
                  window 5: client gone again
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402
from parity_test import signal  # noqa: E402

RUN = HERE / "run"
PROBE = ROOT / "dist" / "efmi_probe.dll"

WINDOWS = [(2.0, 5.0), (7.0, 10.0), (12.0, 15.0), (17.0, 20.0), (22.0, 25.0)]
NAMES = ["loaded, no client", "client connected", "continuous mode", "full captures", "client gone"]


class Runner:
    def __init__(self, with_probe: bool, with_xxmi: bool, repeat: int):
        args = [str(RUN / "efmi_sandbox.exe"), "--novsync", "--repeat", str(repeat), "--seconds", "40"]
        if with_xxmi:
            args += ["--xxmi", str(RUN / "EFMI" / "d3d11.dll")]
        if with_probe:
            args += ["--probe", str(PROBE)]
        self.proc = subprocess.Popen(args, cwd=str(RUN), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        self.pid = self.proc.pid
        self.beats: list[tuple[float, int]] = []
        self.ready = threading.Event()
        self.t_ready = 0.0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line == "READY":
                self.t_ready = time.perf_counter()
                self.ready.set()
            elif line.startswith("beat frame="):
                self.beats.append((time.perf_counter(), int(line.split("=")[1])))

    def wait_until(self, seconds_after_ready: float) -> None:
        delay = self.t_ready + seconds_after_ready - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    def window_fps(self, start: float, end: float) -> float:
        pts = [(t - self.t_ready, f) for t, f in self.beats if start - 0.6 <= t - self.t_ready <= end + 0.6]
        if len(pts) < 2:
            return 0.0
        (t0, f0), (t1, f1) = pts[0], pts[-1]
        return (f1 - f0) / (t1 - t0) if t1 > t0 else 0.0

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1000, help="the three mesh draws are issued this many times per frame")
    ap.add_argument("--no-xxmi", action="store_true", help="measure without XXMI in the process")
    args = ap.parse_args()
    with_xxmi = not args.no_xxmi

    ref = Runner(with_probe=False, with_xxmi=with_xxmi, repeat=args.repeat)
    if not ref.ready.wait(30):
        print("sandbox did not start")
        return 2
    ref.wait_until(WINDOWS[-1][1] + 1.0)
    ref_fps = [ref.window_fps(a, b) for a, b in WINDOWS]
    ref.quit()

    r = Runner(with_probe=True, with_xxmi=with_xxmi, repeat=args.repeat)
    if not r.ready.wait(30):
        print("sandbox did not start")
        return 2
    live_frames: list[dict] = []
    cap_times: list[float] = []
    cap_stats = None
    try:
        r.wait_until(6.0)
        client = connect(r.pid)
        client.status()

        r.wait_until(11.0)
        client.live(enable=True, hash_ib=True)
        r.wait_until(15.5)
        _, live_frames = client.live_get(max_frames=200)
        client.live(enable=False)

        r.wait_until(16.5)
        while time.perf_counter() - r.t_ready < 20.5:
            t0 = time.perf_counter()
            doc, _blob = client.capture(detail="full", hold=False)
            cap_times.append((time.perf_counter() - t0) * 1000)
            cap_stats = doc["stats"]

        r.wait_until(21.0)
        client.close()
        r.wait_until(WINDOWS[-1][1] + 1.0)
        fps = [r.window_fps(a, b) for a, b in WINDOWS]
    finally:
        r.quit()

    draws = live_frames[-1]["draws"] if live_frames else 0
    print(f"\nscene: about {draws or args.repeat * 3} draw calls per frame, XXMI {'present' if with_xxmi else 'absent'}")
    print(f"{'window':<20}{'reference':>12}{'with probe':>12}{'frame time':>22}{'difference':>14}")
    for name, a, b in zip(NAMES, ref_fps, fps):
        if a > 0 and b > 0:
            diff = 1000.0 / b - 1000.0 / a
            print(f"{name:<20}{a:9.1f} fps{b:9.1f} fps{1000 / a:9.2f} -> {1000 / b:6.2f} ms{diff:+11.2f} ms")
        else:
            print(f"{name:<20}{a:9.1f} fps{b:9.1f} fps")

    if live_frames:
        steady = live_frames[len(live_frames) // 4:]
        costs = [f["cost_ms"] for f in steady]
        last = live_frames[-1]
        hashed = sum(1 for it in last["items"] if it[2])
        print(f"\ncontinuous mode, probe's own timer over {len(steady)} frames: median {statistics.median(costs):.3f} ms, "
              f"max {max(costs):.3f} ms per frame")
        print(f"  first frame after switching on: {live_frames[0]['cost_ms']:.3f} ms, "
              f"{live_frames[0]['readback_bytes']} bytes queued for readback")
        print(f"  last frame: {last['draws']} draws -> {len(last['items'])} distinct items, {hashed} with an index hash, "
              f"{last['volatile']} volatile regions skipped, {last['readback_bytes']} bytes read back")
    if cap_times and cap_stats:
        print(f"full capture: {len(cap_times)} requests, round trip median {statistics.median(cap_times):.1f} ms "
              f"(max {max(cap_times):.1f}); in the game process: record {cap_stats['record_ms']:.2f} ms, "
              f"readback handling {cap_stats['finalize_ms']:.2f} ms; "
              f"{cap_stats['regions']} regions, {cap_stats['cache_hits']} cache hits, {cap_stats['arena_bytes']} bytes read back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
