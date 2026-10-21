"""Recording (probe 0.1.11): what the probe writes between switching it on and off, against the
sandbox's --flash mesh.

The hotkey (Ctrl+F9 in the game) cannot be pressed here without sending keys to whatever has the
focus, so the tests switch recording through the pipe command, which takes the same path at the next
frame boundary; the key itself is checked in the game.

Run A, without XXMI, --flash 45,4 (four frames of every 45):
  - the status names the hotkey; starting makes a folder with recording.json, stopping finishes it
    with the counts the status gave;
  - frames.bin has a record per recorded frame, in order; every recorded frame is covered by as many
    runs as it had lines (the runs are the frames' lines regrouped);
  - the scene's meshes are one run each over the whole recording, the flash is a run per flash of at
    most four frames, 45 frames apart;
  - the first keyframe is taken at the start and reads like a capture: it draws what the stream says
    that frame drew;
  - a recording goes on while its client is away, and is stopped by the next one.
Run B, with XXMI, --flash 1,1,S (on for good from frame S): no keyframe for news before the flash,
although XXMI draws its own text; a keyframe for the flash's new shader pair a few frames after it
first appears, which draws it; a periodic keyframe after ten seconds; a capture with contents taken by
the client before the flash can still be read after the recording's keyframes.
Run C, with an efmi_probe.ini next to the DLL ([Record] key, dir, keyframe_every_s=1, max_keyframes=3):
another hotkey is named, the folder is where the setting says, keyframes stop at three.

Run D, without XXMI, through the MCP tools: efmi_record on / status / off, efmi_record_list,
efmi_record_history (the flash recurs as one group, as many times as it flashed), efmi_record_keyframe
(the start keyframe loads as a capture that efmi_list_draws reads).

Starts the sandbox with the probe given by EFMI_PROBE_DLL (default dist\\efmi_probe.dll); the window
opens on the second screen. Recordings are written next to that DLL and removed at the end.
"""
from __future__ import annotations

import json
import shutil
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp import contents, tools  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import patch  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError  # noqa: E402
from contents_test import connect  # noqa: E402
from continuous_test import FLASH_IC, SCENE_ICS, FlashSandbox, flash_index_bytes  # noqa: E402
from selftest import PROBE  # noqa: E402

failures: list[str] = []
passed = 0
made: list[Path] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail[:700]}")
    return bool(cond)


def record(client: ProbeClient, action: str, **fields) -> dict:
    reply, _ = client.request("record", action=action, **fields)
    return reply["record"]


def load(folder: Path) -> dict:
    out = {"meta": json.loads((folder / "recording.json").read_text(encoding="utf-8"))}
    out["lines"] = [json.loads(l) for l in (folder / "lines.jsonl").read_text(encoding="utf-8").splitlines() if l]
    out["runs"] = [json.loads(l) for l in (folder / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l]
    raw = (folder / "frames.bin").read_bytes()
    out["frames"] = [struct.unpack_from("<5I", raw, i) for i in range(0, len(raw) - len(raw) % 20, 20)]
    kf = folder / "keyframes.jsonl"
    out["keyframes"] = [json.loads(l) for l in kf.read_text(encoding="utf-8").splitlines() if l] if kf.exists() else []
    return out


def lines_with(rec: dict, ic: int) -> list[int]:
    return [l["id"] for l in rec["lines"] if l["line"][5] == ic]


def keyframe(folder: Path, entry: dict) -> Capture:
    doc = json.loads((folder / entry["json"]).read_text(encoding="utf-8"))
    return Capture(doc, (folder / entry["bin"]).read_bytes())


def started(client: ProbeClient) -> Path | None:
    st = record(client, "start")
    for _ in range(100):
        if st.get("folder") or st.get("error"):
            break
        time.sleep(0.05)
        st = record(client, "status")
    if st.get("folder"):
        made.append(Path(st["folder"]))
        return Path(st["folder"])
    return None


# ---------------------------------------------------------------- run A
def run_a() -> None:
    print("\n== run A: what a recording holds, without XXMI (--flash 45,4)")
    sb = FlashSandbox(["--seconds", "90", "--flash", "45,4"], xxmi=False)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        st = client.status()
        check("the probe can record", "record" in (st.get("features") or ()), str(st.get("features")))
        rs = st["capture"]["record"]
        check("the status names the hotkey and no recording", rs.get("on") is False and rs.get("hotkey") == "ctrl+F9",
              str(rs))
        folder = started(client)
        if not check("starting makes a folder of its own", folder is not None and folder.is_dir()
                     and folder.name.startswith("recording-"), str(folder)):
            return
        meta = json.loads((folder / "recording.json").read_text(encoding="utf-8"))
        check("recording.json says it is recording", meta.get("state") == "recording" and meta.get("started_by") == "client"
              and meta.get("probe") == st["probe"], str(meta))
        time.sleep(3.0)
        mid = record(client, "status")
        check("while it records the status counts frames", mid.get("on") and mid.get("frames", 0) > 60, str(mid))

        # the client goes away and comes back: the recording goes on
        before = mid.get("frames", 0)
        client.close()
        time.sleep(1.5)
        client = connect(sb.pid)
        if not check("reconnected", client is not None):
            return
        back = record(client, "status")
        check("a recording goes on while its client is away", back.get("on") and back.get("frames", 0) > before + 40,
              f"{before} -> {back.get('frames')}")
        time.sleep(1.0)
        end = record(client, "stop", flush=True)
        check("stopping finishes it", end.get("on") is False and end.get("stopped_by") == "client" and end.get("pending") == 0,
              str(end))

        rec = load(folder)
        meta = rec["meta"]
        check("recording.json is finished with the counts", meta.get("state") == "stopped"
              and meta.get("frames") == end.get("frames") == len(rec["frames"])
              and meta.get("lines") == len(rec["lines"]) == end.get("lines")
              and meta.get("runs") == len(rec["runs"]) and meta.get("keyframes") == len(rec["keyframes"]),
              f"{meta} / {len(rec['frames'])} frames, {len(rec['lines'])} lines, {len(rec['runs'])} runs")
        check("open-runs.json is gone", not (folder / "open-runs.json").exists())
        frames = rec["frames"]
        nums = [f[0] for f in frames]
        check("frames.bin is in order, with times", nums == sorted(set(nums)) and all(b[1] >= a[1] for a, b in zip(frames, frames[1:]))
              and frames[-1][1] > 4000, f"{nums[:5]} ... {frames[-1]}")
        check("the line numbers are 0..n-1", [l["id"] for l in rec["lines"]] == list(range(len(rec["lines"]))))
        # every recorded frame: as many runs cover it as it had lines
        by_frame: dict[int, int] = {f: 0 for f in nums}
        index = {f: i for i, f in enumerate(nums)}
        bad_runs = []
        for run in rec["runs"]:
            lid, first, last, n = run[:4]
            span = [f for f in nums[index[first]:index[last] + 1]] if first in index and last in index else []
            if len(span) != n:
                bad_runs.append(run)
            for f in span:
                by_frame[f] += 1
        check("each run covers the recorded frames from its first to its last", not bad_runs, str(bad_runs[:3]))
        check("every recorded frame is covered by as many runs as it had lines",
              all(by_frame[f[0]] == f[4] for f in frames), str([(f[0], f[4], by_frame[f[0]]) for f in frames
                                                               if by_frame[f[0]] != f[4]][:5]))
        scene = [lid for ic in SCENE_ICS for lid in lines_with(rec, ic)]
        scene_runs = [r for r in rec["runs"] if r[0] in scene]
        check("the scene's meshes are one run each over the whole recording",
              len(scene) >= 3 and len(scene_runs) == len(scene)
              and all(r[1] == nums[0] and r[2] == nums[-1] and r[3] == len(nums) for r in scene_runs),
              str(scene_runs[:4]))
        flash = lines_with(rec, FLASH_IC)
        fruns = sorted((r for r in rec["runs"] if r[0] in flash), key=lambda r: r[1])
        starts = [r[1] for r in fruns]
        check("the flash is a run per flash, of at most four frames, 45 frames apart",
              len(flash) == 1 and len(fruns) >= 8 and all(r[2] - r[1] <= 3 for r in fruns)
              and all(42 <= b - a <= 48 for a, b in zip(starts[1:-1], starts[2:-1])), str(fruns[:6]))
        kfs = rec["keyframes"]
        if check("the first keyframe is the start", kfs and kfs[0]["reason"] == "start", str(kfs[:2])):
            cap = keyframe(folder, kfs[0])
            game = [d for d in cap.draws() if d["origin"] != "xxmi"]
            check("it reads like a full capture", cap.detail == "full" and cap.frame == kfs[0]["frame"]
                  and len(cap) == kfs[0]["draws"] and any(d["args"].get("index_count") == 864 for d in game)
                  and any(d.get("cb") for d in game), f"{cap.frame} {len(cap)} {kfs[0]}")
            ics = {d["args"].get("index_count") for d in game} - {None}
            near = min(frames, key=lambda f: abs(f[0] - cap.frame))
            check("it draws the scene the stream shows", set(SCENE_ICS) <= ics, str(sorted(ics)))
            check("its frame is not one of the stream's", cap.frame not in index, f"{cap.frame} near {near[0]}")
    finally:
        if client is not None:
            try:
                record(client, "stop", flush=True)
            except ProbeError:
                pass
            client.close()
        sb.quit()


# ---------------------------------------------------------------- run B
def run_b() -> None:
    start_frame = 420
    print(f"\n== run B: keyframes for news, with XXMI (--flash 1,1,{start_frame})")
    sb = FlashSandbox(["--seconds", "90", "--flash", f"1,1,{start_frame}"], xxmi=True)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        frame = client.status()["frame"]
        if not check("well before the flash", frame < start_frame - 150, f"frame {frame}"):
            return
        folder = started(client)
        if not check("recording", folder is not None):
            return
        # a capture of the client's with contents, taken before the flash
        doc, blob = client.capture(detail="full", timeout_ms=8000,
                                   contents=contents.request("buffers", None, -1, -1, False, 256))
        mine = Capture(doc, blob)
        ib = next((d["ib"] for d in mine.draws() if d["args"].get("index_count") == 864 and d.get("ib", {}).get("content") is not None), None)
        deadline = time.time() + 30
        while not sb.flashes() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(11.0)                 # the periodic keyframe comes ten seconds after the last
        st = record(client, "status")
        if ib is not None:
            meta, data = client.read_content(mine.frame, ib["content"])
            check("the client's contents survive the recording's keyframes",
                  st.get("keyframes", 0) >= 2 and len(data) > 0, f"{st.get('keyframes')} {meta}")
        else:
            check("the client's capture kept index data", False)
        end = record(client, "stop", flush=True)
        rec = load(folder)
        kfs = rec["keyframes"]
        reasons = [k["reason"] for k in kfs]
        flash = lines_with(rec, FLASH_IC)
        fruns = [r for r in rec["runs"] if r[0] in flash]
        first_flash = min((r[1] for r in fruns), default=None)
        news = [k for k in kfs if k["reason"] == "new shader pair"]
        check("no keyframe for news before the flash, XXMI's text notwithstanding",
              first_flash is not None and all(k["frame"] > first_flash for k in news), f"{first_flash} {kfs}")
        if check("a keyframe for the flash's new shader pair, a few frames after it appeared",
                 news and first_flash is not None and 0 < news[0]["frame"] - first_flash <= 8, f"{first_flash} {news[:2]}"):
            cap = keyframe(folder, news[0])
            fd = [d for d in cap.draws() if d["args"].get("index_count") == FLASH_IC]
            check("it draws the flash, with the pair it names", len(fd) == 1
                  and fd[0]["shaders"].get("ps") in news[0].get("what", ""), f"{news[0]} {fd[:1]}")
        check("a periodic keyframe after ten seconds", "periodic" in reasons, str(reasons))
        check("the status counted the news", end.get("news", 0) >= 1, str(end))
    finally:
        if client is not None:
            try:
                record(client, "stop", flush=True)
            except ProbeError:
                pass
            client.close()
        sb.quit()


# ---------------------------------------------------------------- run C
def run_c() -> None:
    print("\n== run C: settings in efmi_probe.ini")
    ini = PROBE.parent / "efmi_probe.ini"
    if ini.exists():
        check("no efmi_probe.ini of someone else's next to the DLL", False, str(ini))
        return
    ini.write_text("[Record]\nkey = shift F7\ndir = record-test\nkeyframe_every_s = 1\nmax_keyframes = 3\n", encoding="utf-8")
    sb = None
    client = None
    try:
        sb = FlashSandbox(["--seconds", "60", "--flash", "45,4"], xxmi=False)
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        rs = client.status()["capture"]["record"]
        check("another hotkey is named", rs.get("hotkey") == "shift+F7", str(rs))
        folder = started(client)
        check("the folder is where the setting says", folder is not None and folder.parent == PROBE.parent / "record-test",
              str(folder))
        if folder is not None:
            made.append(folder.parent)
        time.sleep(5.0)
        end = record(client, "stop", flush=True)
        check("keyframes stop at three", end.get("keyframes") == 3 and end.get("keyframes_stopped") is True, str(end))
    finally:
        if client is not None:
            client.close()
        if sb is not None:
            sb.quit()
        ini.unlink(missing_ok=True)


# ---------------------------------------------------------------- run D
def run_d() -> None:
    print("\n== run D: the MCP tools, without XXMI (--flash 45,4)")
    sb = FlashSandbox(["--seconds", "60", "--flash", "45,4"], xxmi=False)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        with tempfile.TemporaryDirectory(prefix="record-mcp-") as tmp:
            root = Path(tmp)
            (root / "Mods").mkdir()
            session = tools.Session(root, root / "out", PROBE)
            session.client = client
            with patch.object(tools, "Session", return_value=session):
                server = tools.build_server(root, root / "out", PROBE)

            def call(name, **arguments):
                reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})
                return reply["result"]["content"][0]["text"], reply["result"]["isError"]

            text, err = call("efmi_record", state="on")
            check("efmi_record on", not err and " is on " in text and "folder:" in text, text)
            folder = next((Path(l.split("folder: ")[1].split(" (")[0]) for l in text.splitlines() if l.startswith("folder: ")), None)
            if folder:
                made.append(folder)
            time.sleep(4.0)
            text, err = call("efmi_record", state="status")
            check("efmi_record status while recording", not err and " is on " in text, text)
            text, err = call("efmi_record", state="off")
            check("efmi_record off", not err and " is off (stopped by client" in text, text)
            text, err = call("efmi_record_list")
            check("efmi_record_list names it", not err and folder is not None and folder.name in text, text)
            text, err = call("efmi_record_history", show="recurring")
            group = next((l for l in text.splitlines() if " times, " in l and "lines: at" in l), "")
            times = int(group.split(" times")[0]) if group else 0
            check("efmi_record_history: the flash recurs as one group, once per flash", not err
                  and f"ic={FLASH_IC}" in text and times >= 5, text[:1500])
            text, err = call("efmi_record_keyframe", at_s=0)
            check("efmi_record_keyframe loads the start keyframe as a capture", not err and "(start" in text
                  and "loaded as capture" in text, text[:800])
            text, err = call("efmi_list_draws", min_index_count=800)
            check("the other tools read it", not err and "ic=864" in text, text[:600])
    finally:
        if client is not None:
            client.close()
        sb.quit()


def main() -> int:
    try:
        run_a()
        run_b()
        run_c()
        run_d()
    finally:
        for path in made:
            if path.exists() and path.parent in (PROBE.parent / "recordings", PROBE.parent / "record-test", PROBE.parent):
                shutil.rmtree(path, ignore_errors=True)
        root = PROBE.parent / "recordings"
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    print(f"\n{passed} passed, {len(failures)} failed")
    for name in failures:
        print(f"  FAIL {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
