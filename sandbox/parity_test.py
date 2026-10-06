"""Parity test: does the probe report the same identities as XXMI itself?

Runs the sandbox with a private copy of the real XXMI DLL and the probe, takes
a capture through the probe, then asks XXMI for one of its own frame analysis
dumps of the same (static) scene and compares the two, hash by hash.

Nothing outside sandbox/run is written.
"""
from __future__ import annotations

import os
import argparse
import ctypes
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.fa_dump import find_frame_dumps, parse_frame_dump  # noqa: E402
from efmi_mcp.parity import compare_ordered, compare_sets  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402

RUN = HERE / "run"
# EFMI_PROBE_DLL selects another build, e.g. one in dist/candidate while the game holds the installed DLL.
PROBE = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
kernel32.SetEvent.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def signal(name: str) -> bool:
    h = kernel32.OpenEventW(0x0002, 0, name)   # EVENT_MODIFY_STATE
    if not h:
        return False
    kernel32.SetEvent(h)
    kernel32.CloseHandle(h)
    return True


class Sandbox:
    def __init__(self, extra_args: list[str], seconds: int = 60):
        args = [str(RUN / "efmi_sandbox.exe"),
                "--xxmi", str(RUN / "EFMI" / "d3d11.dll"),
                "--probe", str(PROBE),
                "--seconds", str(seconds)] + extra_args
        self.proc = subprocess.Popen(args, cwd=str(RUN), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        self.pid = self.proc.pid
        self.lines: list[str] = []

    def wait_ready(self, timeout: float = 30.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    return False
                continue
            self.lines.append(line.rstrip())
            if line.strip() == "READY":
                return True
        return False

    def dump(self) -> bool:
        return signal(f"efmi_sandbox_dump_{self.pid}")

    def quit(self) -> None:
        signal(f"efmi_sandbox_quit_{self.pid}")
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_for_dump(folder: Path, before: set[Path], timeout: float = 90.0) -> Path | None:
    end = time.time() + timeout
    found: Path | None = None
    while time.time() < end:
        new = [p for p in find_frame_dumps(folder) if p not in before]
        if new:
            found = new[-1]
            break
        time.sleep(0.2)
    if not found:
        return None
    # The dump is complete once its log stops growing.
    last, stable = -1, 0
    while time.time() < end:
        log = found / "log.txt"
        size = log.stat().st_size if log.exists() else -1
        count = sum(1 for _ in found.iterdir())
        key = size * 100003 + count
        stable = stable + 1 if key == last and size > 0 else 0
        last = key
        if stable >= 5:
            return found
        time.sleep(0.3)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep XXMI's dump folder for inspection")
    ap.add_argument("--probe-first", action="store_true", help="load the probe before XXMI")
    ap.add_argument("--preload-runtime", action="store_true", help="load the system d3d11.dll before XXMI")
    args = ap.parse_args()

    if not (RUN / "efmi_sandbox.exe").exists() or not PROBE.exists():
        print("build first: probe\\build.bat, sandbox\\build.bat, python sandbox\\prepare_run.py")
        return 2

    # XXMI 1.2.0's own frame analysis never returns from the non-indexed draws of
    # this scene (reproduced with XXMI alone, without the probe), so those two
    # draws are left out of the run that is compared against XXMI.
    extra = ["--skip", "80"]
    if args.probe_first:
        extra.append("--probe-first")
    if args.preload_runtime:
        extra.append("--preload-runtime")

    efmi = RUN / "EFMI"
    before = set(find_frame_dumps(efmi))
    sb = Sandbox(extra)
    rc = 1
    try:
        if not sb.wait_ready():
            print("sandbox did not start:\n" + "\n".join(sb.lines))
            return 2
        for line in sb.lines:
            print("  [sandbox]", line)

        client = None
        for _ in range(50):
            if any(p.pid == sb.pid for p in find_probes()):
                try:
                    client = ProbeClient.connect(sb.pid)
                    break
                except ProbeError:
                    pass
            time.sleep(0.1)
        if client is None:
            print("could not connect to the probe")
            return 2

        st = client.status()
        print(f"probe {st['probe']}: attached={st['attached']} via {st['attached_via']!r} "
              f"saw_wrapper={st['saw_wrapper']} swap_chain={st['swap_chain']} resolution={st['resolution']}")
        print(f"  runtime: {st['runtime']}")
        print(f"  xxmi: {st['xxmi']}")
        if not st["attached"]:
            return 1

        time.sleep(0.5)
        t0 = time.perf_counter()
        doc, blob = client.capture(detail="full", cb_data=True)
        dt = (time.perf_counter() - t0) * 1000
        cap = Capture(doc, blob)
        print(f"capture: frame {cap.frame}, {len(cap)} draws, {len(cap.resources)} resources, "
              f"round trip {dt:.1f} ms, stats {cap.stats}")

        if not sb.dump():
            print("could not signal the sandbox")
            return 2
        dump_dir = wait_for_dump(efmi, before)
        if not dump_dir:
            print("XXMI did not produce a frame analysis dump")
            return 1
        dump = parse_frame_dump(dump_dir)
        print(f"xxmi dump: {dump_dir.name}, {dump.file_count} files, analyse_options {dump.analyse_options}")

        res = compare_ordered(cap, dump, args.verbose)
        print("\n".join(res.messages))
        print(f"\n{res.checks} checks, {res.mismatches} mismatches"
              + (f", {res.stale} where XXMI's cached hash was stale" if res.stale else ""))
        rc = 0 if res.mismatches == 0 and res.checks > 20 else 1

        # The frame-independent comparison offered to agents must agree as well.
        loose = compare_sets(cap, dump)
        print("set comparison: " + "; ".join(m.strip() for m in loose.messages))
        if loose.mismatches:
            rc = 1

        # A second capture must hit the region cache and still agree with the first.
        doc2, _ = client.capture(detail="full")
        cap2 = Capture(doc2)

        def game_hashes(c: Capture) -> list:
            return [(d.get("ib", {}).get("hash"), sorted((k, v["hash"]) for k, v in d.get("vb", {}).items()))
                    for d in c.draws() if d["origin"] != "xxmi"]

        same = game_hashes(cap) == game_hashes(cap2)
        print(f"second capture: cache hits {cap2.stats.get('cache_hits')}, "
              f"arena bytes {cap2.stats.get('arena_bytes')}, game ib/vb hashes identical: {same}")
        if not same:
            print(f"first game geometry: {game_hashes(cap)}")
            print(f"second game geometry: {game_hashes(cap2)}")
            rc = 1
        client.close()
        if not args.keep:
            shutil.rmtree(dump_dir, ignore_errors=True)
    finally:
        sb.quit()
    print("PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
