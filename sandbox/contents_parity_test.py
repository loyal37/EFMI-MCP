"""Byte for byte: what the probe kept of each call against what XXMI's own frame analysis wrote for it.

The sandbox draws its content pass with the same bytes in every frame
(--content-static): about thirty calls, between each two of which a buffer or
a texture is rewritten. The probe records one frame with contents; then the
private copy of XXMI is asked for a frame analysis dump of a later frame. Every
.buf and .dds file XXMI wrote for call N is compared with the bytes the probe
hands back for the N-th call of the game.

XXMI copies each resource the moment a call has been issued and stalls the
frame for it. The probe copied nothing at that moment: it is asked afterwards,
when the program has drawn many more frames. The comparison says whether it
still answers with what was there at the call.

Nothing outside sandbox/run is written.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import contents  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.fa_dump import find_frame_dumps, parse_frame_dump  # noqa: E402
from efmi_mcp.parity import compare_contents  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes  # noqa: E402
from parity_test import PROBE, RUN, Sandbox, wait_for_dump  # noqa: E402


def settle(folder: Path, timeout: float = 120.0) -> int:
    """Waits until XXMI has stopped adding files to its dump; returns how many there are."""
    end = time.time() + timeout
    last, stable = -1, 0
    while time.time() < end:
        count = sum(1 for _ in folder.iterdir())
        stable = stable + 1 if count == last else 0
        last = count
        if stable >= 6:
            break
        time.sleep(0.5)
    return last


class Reader:
    """What contents.dump_frame needs of a session: the probe, and a folder."""

    def __init__(self, client, folder: Path):
        self.client, self.folder = client, folder

    def call(self, fn):
        return fn(self.client)

    def frame_dir(self, cap):
        return self.folder


def compare_texts(cap: Capture, dump_dir: Path, ours: Path) -> tuple[int, int, list[str]]:
    """Every .txt XXMI wrote against the one efmi_dump_frame writes for the same call and slot: (same, different, notes)."""
    game = [d for d in cap.draws() if d["origin"] != "xxmi"]
    numbers = [n for n in parse_frame_dump(dump_dir).draw_numbers()]
    same = different = 0
    notes: list[str] = []
    for mine, n in zip(game, numbers):
        for theirs in sorted(dump_dir.glob(f"{n:06d}-*.txt")):
            slot = theirs.name.split("-", 1)[1].split("=")[0]
            written = sorted(ours.glob(f"{mine['index']:06d}-{slot}=*.txt"))
            if written and written[0].read_bytes().replace(b"\r\n", b"\n") == theirs.read_bytes().replace(b"\r\n", b"\n"):
                same += 1
            else:
                different += 1
                if len(notes) < 8:
                    notes.append(f"  MISMATCH call {n:06d} {slot}.txt" + ("" if written else ": not written"))
    return same, different, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep XXMI's dump folder for inspection")
    args = ap.parse_args()
    if not (RUN / "efmi_sandbox.exe").exists() or not PROBE.exists():
        print("build first: probe\\build.bat, sandbox\\build.bat, python sandbox\\prepare_run.py")
        return 2

    efmi = RUN / "EFMI"
    before = set(find_frame_dumps(efmi))
    # --skip 80: the two draws without an index buffer, which XXMI's frame analysis does not come back from here.
    sb = Sandbox(["--skip", "80", "--content-static"], seconds=180)
    rc = 1
    dump_dir = None
    try:
        if not sb.wait_ready():
            print("sandbox did not start:\n" + "\n".join(sb.lines))
            return 2
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
        if "contents" not in st.get("features", ()):
            print(f"probe {st['probe']} does not keep contents (0.1.6 is needed)")
            return 2
        time.sleep(0.5)

        # strict: a no-overwrite map between two draws starts a new version, as XXMI's copy at each call shows it.
        doc, blob = client.capture(detail="full", timeout_ms=8000, contents={
            "inputs": ["ib", "vb", "cb", "buf", "tex"], "outputs": ["rt", "uav"], "all": True, "strict": True})
        cap = Capture(doc, blob)
        game = [d for d in cap.draws() if d["origin"] != "xxmi"]
        print(f"probe {st['probe']}: capture {cap.frame}, {len(cap)} calls ({len(game)} from the game), "
              f"{cap.stats.get('ct_snaps')} contents noted, {cap.stats.get('ct_bytes')} bytes copied so far")

        if not sb.dump():
            print("could not signal the sandbox")
            return 2
        dump_dir = wait_for_dump(efmi, before, timeout=150)
        if not dump_dir:
            print("XXMI did not produce a frame analysis dump")
            return 1
        count = settle(dump_dir)
        dump = parse_frame_dump(dump_dir)
        calls = [n for n in dump.draw_numbers() if dump.draws[n].call]
        print(f"xxmi dump: {dump_dir.name}, {len(calls)} calls, {count} entries, options {dump.analyse_options}")
        # The probe is asked only now: the dump alone took seconds, hundreds of frames in this program.
        res = compare_contents(cap, dump, lambda content: client.read_content(cap.frame, content, timeout_ms=10000),
                               args.verbose)
        print("\n".join(res.messages))
        after = client.status()["frame"]
        print(f"\n{res.checks} comparisons, {res.mismatches} mismatches; asked {after - cap.frame} frames after the capture")
        rc = 0 if res.mismatches == 0 and res.checks > 300 and len(calls) == len(game) == 34 else 1

        # The same frame written out by efmi_dump_frame: its index, vertex and constant text against XXMI's.
        out = RUN / "mcp-out" / "contents-parity-dump"
        shutil.rmtree(out, ignore_errors=True)
        written = contents.dump_frame(Reader(client, out), cap, folder=str(out), max_seconds=300)
        same, different, notes = compare_texts(cap, dump_dir, out)
        print(f"written out: {written['files']} files for calls {written['calls_written']}, {written['megabytes']} MiB, "
              f"skipped {written.get('skipped', {})}")
        print(f"text files of XXMI: {same} identical to the ones written here, {different} different")
        print("\n".join(notes))
        if different or same < 150 or written.get("problems"):
            rc = 1
        if not args.keep:
            shutil.rmtree(out, ignore_errors=True)
        client.close()
    finally:
        sb.quit()
        if dump_dir and not args.keep:
            shutil.rmtree(dump_dir, ignore_errors=True)
    print("PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
