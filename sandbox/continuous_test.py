"""Continuous mode over time and the trigger (probe 0.1.10), against the sandbox's --flash mesh.

The sandbox's --flash P,D,S draws a mesh of its own (index data in an immutable buffer, 150 indices,
a pixel shader nothing else uses) for D frames out of every P from frame S on: something that is on
screen only for a moment, like an effect in the game.

Run A, without XXMI (every draw is the sandbox's, so counts are exact), --flash 45,4:
  - live_summary over the last seconds: the window spans those seconds; the flash mesh is one line,
    drawn in a few of the frames, with the region hash of its index bytes, while the scene's meshes
    are drawn in every frame; the timeline has a row per frame;
  - the probe adds up the same as the server does from live_get (summarize_frames, used with probes
    before 0.1.10), with and without a baseline;
  - a window of one flash with a baseline between two flashes: the flash line has no baseline
    frames, the scene's lines all of them; a baseline by seconds ends right before the window;
  - a trigger on the flash's pixel shader records the next frame, which draws it, with contents: its
    index data reads back as the sandbox wrote it; delay_frames adds frames; a trigger on the flash's
    index buffer hash (remembered from earlier frames) fires at once;
  - while a fired trigger waits for its frame, an explicit capture and another arming are refused,
    and cancelling lets them through again; a trigger on a hash never drawn expires (said once, then
    off), is cancelled, and is cancelled when the client goes away;
  - after a reconnect, where remembered hashes are dropped, an index buffer trigger fires once the
    frame's index data has been read back ('late') and a later frame is recorded; the next arming
    fires at once again.
Run B, with XXMI, --flash 1,1,S (on for good from frame S): a new-pair trigger armed before S, with a
baseline, waits through the baseline and the frames before S without firing although XXMI draws its
own text, then fires on the flash's vertex/pixel shader pair in the frame the sandbox drew it first;
the history then shows the flash as new against a baseline before it.

Run C, without XXMI, through the MCP tools (efmi_live_start, efmi_live_history, efmi_trigger_arm with
wait_s, efmi_read_buffer, efmi_trigger_wait, efmi_live_stop): the history lists the flash as transient;
the triggered frame becomes the latest capture, and its index data reads back from the kept contents.

Starts the sandbox with the probe given by EFMI_PROBE_DLL (default dist\\efmi_probe.dll); the window
opens on the second screen. No key is pressed and nothing is written outside the sandbox's run folder.
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

import json  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import patch  # noqa: E402

from efmi_mcp import contents, tools  # noqa: E402
from efmi_mcp.capture_model import Capture  # noqa: E402
from efmi_mcp.hashes import crc32c, fnv64, hex32, hex64  # noqa: E402
from efmi_mcp.live_tools import ROW, summarize_frames  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError  # noqa: E402
from contents_test import connect  # noqa: E402
from selftest import Sandbox  # noqa: E402

FLASH_IC = 150
SCENE_ICS = (864, 2400, 294)
NEVER = "0123456789abcdef"

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail[:700]}")
    return bool(cond)


class FlashSandbox(Sandbox):
    """The sandbox, with the frames in which it switched the flash on."""

    def _pump(self) -> None:
        self.lines: list[str] = []
        for line in self.proc.stdout:
            line = line.strip()
            self.lines.append(line)
            if line == "READY":
                self.ready.set()

    def flashes(self) -> list[int]:
        return [int(line.split("frame=")[1]) for line in list(getattr(self, "lines", []))
                if line.startswith("flash on frame=")]


def flash_index_bytes() -> bytes:
    """The flash mesh's index data: the sandbox's make_grid for a 5x5 grid, 16-bit indices."""
    n, out = 5, []
    for y in range(n):
        for x in range(n):
            a = y * (n + 1) + x
            b, c = a + 1, a + n + 1
            out += [a, c, b, b, c, c + 1]
    return struct.pack(f"<{len(out)}H", *out)


def rows(summary: dict) -> list[dict]:
    return [dict(zip(ROW, it)) for it in summary["items"]]


def lines_of(summary: dict, ic: int) -> list[dict]:
    return [r for r in rows(summary) if r["ic"] == ic]


def item_set(summary: dict) -> set[tuple]:
    return {tuple(it) for it in summary["items"]}


def trigger_state(client: ProbeClient) -> str:
    return client.status()["capture"].get("trigger", "?")


def flash_drawn_now(client: ProbeClient) -> bool:
    _now, frames = client.live_get(max_frames=1)
    return bool(frames) and any(it[5] == FLASH_IC for it in frames[-1]["items"])


def when_flash_off(client: ProbeClient, timeout: float = 5.0) -> None:
    """Returns right after a flash has ended, so that an arming made now lands in the frames between two
    flashes (the stream is a frame or two behind the game; a flash is four frames, the gap 41)."""
    deadline = time.time() + timeout
    while not flash_drawn_now(client) and time.time() < deadline:
        time.sleep(0.005)
    while flash_drawn_now(client) and time.time() < deadline:
        time.sleep(0.005)


def wait_state(client: ProbeClient, wanted: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    st = trigger_state(client)
    while st != wanted and time.time() < deadline:
        time.sleep(0.02)
        st = trigger_state(client)
    return st


def draws_of(cap: Capture, ic: int) -> list[dict]:
    return [d for d in cap.draws() if d["args"].get("index_count") == ic]


# ---------------------------------------------------------------- run A
def run_a() -> None:
    print("\n== run A: history and triggers, without XXMI (--flash 45,4)")
    sb = FlashSandbox(["--seconds", "120", "--flash", "45,4"], xxmi=False)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        st = client.status()
        feats = set(st.get("features") or ())
        if not check("the probe has live_summary and trigger", {"live_summary", "trigger"} <= feats,
                     f"{st.get('probe')} {sorted(feats)}"):
            return
        client.request("slot_cache", enable=False)      # nothing else hashes index data meanwhile
        client.live(enable=True, keep_frames=900)
        time.sleep(3.0)

        # ---- history by seconds
        _now, summ = client.live_summary(seconds=2.0, timeline=True)
        win = summ["window"]
        n = win["frames"]
        span = win["t_last"] - win["t_first"]
        check("the window covers about the last two seconds", n > 30 and 1500 <= span <= 2000,
              f"{n} frames over {span} ms")
        flash = lines_of(summ, FLASH_IC)
        expected_ib = hex32(crc32c(flash_index_bytes()))
        check("the flash mesh is one line, with the region hash of its index bytes",
              len(flash) == 1 and flash[0]["ib"] == expected_ib and flash[0]["ib_state"] == "ok",
              f"{flash} vs {expected_ib}")
        if not flash:
            return
        fl = flash[0]
        check("it is drawn in a few frames of the window (4 of every 45)",
              1 <= fl["frames"] <= n * 4 / 45 + 5 and fl["max_draws"] == 1, str(fl))
        scene = [r for r in rows(summ) if r["ic"] in SCENE_ICS]
        check("the scene's meshes are drawn in every frame of the window",
              len(scene) >= 3 and all(r["frames"] == n for r in scene), str([(r["ic"], r["frames"]) for r in scene]))
        others_ps = {r["ps"] for r in rows(summ) if r["ic"] != FLASH_IC}
        check("the flash's pixel shader is drawn by nothing else", fl["ps"] and fl["ps"] not in others_ps, str(fl))
        tl = summ.get("timeline") or []
        check("the timeline has one row per frame of the window",
              len(tl) == n and all(b[0] > a[0] and b[1] >= a[1] for a, b in zip(tl, tl[1:])), str(tl[:3]))
        _meta, code = client.shader(fl["ps"])
        check("the flash shader's bytecode has its hash", hex64(fnv64(code)) == fl["ps"], fl["ps"])

        # ---- the probe adds up what the server would
        _now, frames = client.live_get(since=0, max_frames=0)
        check("live_get still lists frames with their lines", frames and all(len(it) == 12 for it in frames[-1]["items"]))
        first, last = win["first"], win["last"]
        _now, by_frames = client.live_summary(first=first, last=last, timeline=False)
        server = summarize_frames(frames, first, last)
        check("the probe's sum over frames equals the server's", item_set(by_frames) == item_set(server),
              f"{len(by_frames['items'])} vs {len(server['items'])}")
        check("without a timeline none is sent", "timeline" not in by_frames)
        _now, with_base = client.live_summary(first=first, last=last, base_first=first - 60, base_last=first - 1)
        server = summarize_frames(frames, first, last, first - 60, first - 1)
        check("... with a baseline too", item_set(with_base) == item_set(server) and with_base["baseline"]["frames"] == 60,
              str(with_base["baseline"]))

        # ---- a window of one flash against a baseline between two flashes
        on = [f["frame"] for f in frames if any(it[5] == FLASH_IC for it in f["items"])]
        bursts: list[list[int]] = []
        for fr in on:
            if bursts and fr == bursts[-1][-1] + 1:
                bursts[-1].append(fr)
            else:
                bursts.append([fr])
        full = [b for b in bursts[1:-1] if len(b) == 4]
        if check("the stream shows flashes of four frames", len(full) >= 2, str(bursts[:6])):
            b0, b1 = full[0], full[1]
            g0, g1 = b0[-1] + 1, b1[0] - 1
            _now, s1 = client.live_summary(first=b1[0], last=b1[-1], base_first=g0, base_last=g1)
            fl1 = lines_of(s1, FLASH_IC)
            check("in a window of one flash it is in every frame and not in the baseline before",
                  len(fl1) == 1 and fl1[0]["frames"] == 4 and fl1[0]["base"] == 0
                  and fl1[0]["first"] == b1[0] and fl1[0]["last"] == b1[-1], str(fl1))
            sc1 = [r for r in rows(s1) if r["ic"] in SCENE_ICS]
            check("the scene's lines are in every baseline frame",
                  sc1 and all(r["base"] == g1 - g0 + 1 for r in sc1), str([(r["ic"], r["base"]) for r in sc1]))
        _now, s2 = client.live_summary(seconds=1.0, base_seconds=1.0, timeline=False)
        b, w = s2["baseline"], s2["window"]
        check("a baseline by seconds ends right before the window",
              b["frames"] > 20 and b["last"] < w["first"] and b["last"] + 1 == w["first"], f"{b} {w}")

        # ---- trigger on the flash's pixel shader, with contents
        when_flash_off(client)
        wanted = contents.request("inputs", None, -1, -1, False, 256)
        state = client.trigger_arm(shader=[fl["ps"]], capture={"detail": "full", "contents": wanted})
        check("arming answers with the trigger's state", state.get("state") in ("arming", "armed"), str(state))
        state, doc, blob = client.trigger_wait(wait_ms=5000)
        fired = state.get("fired") or {}
        check("the shader trigger fired at draw time", state.get("state") == "done" and fired.get("what") == "shader"
              and fired.get("shader") == fl["ps"] and fired.get("late") is False and fired.get("count") == FLASH_IC,
              str(state))
        if doc is not None:
            cap = Capture(doc, blob)
            check("the frame after the one it was seen in was recorded",
                  cap.frame == fired.get("frame", -9) + 1 == state.get("capture_frame"), f"{cap.frame} {state}")
            fd = draws_of(cap, FLASH_IC)
            check("that frame draws the flash, in full", len(fd) == 1 and fd[0]["shaders"].get("ps") == fl["ps"]
                  and fd[0]["ib"]["hash"] == expected_ib and cap.detail == "full", str(fd[:1]))
            if fd and "content" in fd[0]["ib"]:
                meta, data = client.read_content(cap.frame, fd[0]["ib"]["content"])
                check("its index data, kept with the capture, is what the sandbox wrote",
                      data == flash_index_bytes() and meta.get("exact", True), f"{len(data)} bytes {meta}")
            else:
                check("the capture kept the flash's index data", False, str(fd[:1]))
        else:
            check("the trigger handed over a capture", False, str(state))
        check("the trigger is off once collected", trigger_state(client) == "off")
        state, doc, _ = client.trigger_wait(wait_ms=0)
        check("the capture is handed over once", doc is None and state.get("state") == "off", str(state))

        # ---- delay
        when_flash_off(client)
        client.trigger_arm(shader=[fl["ps"]], delay_frames=2, capture={"detail": "light"})
        state, doc, _ = client.trigger_wait(wait_ms=5000)
        fired = state.get("fired") or {}
        check("delay_frames=2 records the third frame after", doc is not None and doc["frame"] == fired.get("frame", -9) + 3,
              f"{doc and doc['frame']} {state}")

        # ---- index buffer hash remembered from earlier frames
        when_flash_off(client)
        client.trigger_arm(ib=[expected_ib], capture={"detail": "light"})
        state, doc, blob = client.trigger_wait(wait_ms=5000)
        fired = state.get("fired") or {}
        check("an index buffer hash already known fires at draw time",
              doc is not None and fired.get("what") == "ib" and fired.get("ib") == expected_ib
              and fired.get("late") is False and doc["frame"] == fired.get("frame", -9) + 1, str(state))
        if doc is not None:
            check("... and the recorded frame draws it", len(draws_of(Capture(doc, blob), FLASH_IC)) == 1)

        # ---- refusals while a fired trigger waits for its frame
        when_flash_off(client)
        client.trigger_arm(shader=[fl["ps"]], delay_frames=600, capture={"detail": "light"})
        check("a trigger that fired waits for its frame", wait_state(client, "fired") == "fired")
        try:
            client.capture(detail="light", timeout_ms=3000)
            check("an explicit capture is refused meanwhile", False)
        except ProbeError as e:
            check("an explicit capture is refused meanwhile", "trigger" in str(e), str(e))
        try:
            client.trigger_arm(shader=[fl["ps"]])
            check("arming again is refused meanwhile", False)
        except ProbeError as e:
            check("arming again is refused meanwhile", "trigger_wait" in str(e), str(e))
        state = client.trigger_cancel()
        check("cancelling switches it off", state.get("state") == "off", str(state))
        try:
            doc, _blob = client.capture(detail="light", timeout_ms=3000)
            check("an explicit capture works again", doc["frame"] > 0)
        except ProbeError as e:
            check("an explicit capture works again", False, str(e))

        # ---- expiry and cancel on a hash never drawn
        client.trigger_arm(shader=[NEVER], expire_ms=400)
        state, doc, _ = client.trigger_wait(wait_ms=3000)
        check("a trigger that sees nothing expires", doc is None and state.get("state") == "expired"
              and state.get("watched_frames", 0) > 5, str(state))
        state, _doc, _ = client.trigger_wait(wait_ms=0)
        check("expiry is said once, then the trigger is off", state.get("state") == "off", str(state))
        client.trigger_arm(shader=[NEVER])
        check("a trigger watches", wait_state(client, "armed") == "armed")
        t0 = time.time()
        state, doc, _ = client.trigger_wait(wait_ms=300)
        check("waiting returns after wait_ms while it watches", doc is None and state.get("state") == "armed"
              and 0.25 <= time.time() - t0 < 2.0, str(state))
        client.trigger_cancel()
        state, _doc, _ = client.trigger_wait(wait_ms=0)
        check("a cancelled trigger is off", state.get("state") == "off" and trigger_state(client) == "off", str(state))
        try:
            client.trigger_arm()
            check("a trigger that watches for nothing is refused", False)
        except ProbeError as e:
            check("a trigger that watches for nothing is refused", "nothing" in str(e), str(e))
        try:
            client.trigger_arm(shader=["xyz"])
            check("a malformed hash is refused", False)
        except ProbeError as e:
            check("a malformed hash is refused", "hex" in str(e), str(e))

        # ---- the client goes away
        client.trigger_arm(shader=[NEVER])
        wait_state(client, "armed")
        client.close()
        client = connect(sb.pid)
        if not check("reconnected", client is not None):
            return
        check("a trigger ends with its client", trigger_state(client) == "off")

        # ---- remembered hashes were dropped with the reconnect: the index buffer trigger fires late
        client.request("slot_cache", enable=False)
        time.sleep(0.2)
        client.trigger_arm(ib=[expected_ib], capture={"detail": "light"})
        state, doc, blob = client.trigger_wait(wait_ms=6000)
        fired = state.get("fired") or {}
        check("a hash not known yet fires once the index data has been read back",
              doc is not None and fired.get("what") == "ib" and fired.get("late") is True
              and doc["frame"] >= fired.get("frame", 10**9) + 1, str(state))
        if doc is not None:
            print(f"       (seen in frame {fired.get('frame')}, recorded frame {doc['frame']})")
        client.trigger_arm(ib=[expected_ib], capture={"detail": "light"})
        state, doc, _ = client.trigger_wait(wait_ms=6000)
        fired = state.get("fired") or {}
        check("... and the next arming fires at draw time again", doc is not None and fired.get("late") is False
              and doc["frame"] == fired.get("frame", -9) + 1, str(state))
    finally:
        if client is not None:
            client.close()
        sb.quit()


# ---------------------------------------------------------------- run B
def run_b() -> None:
    start = 900
    print(f"\n== run B: a new shader pair, with XXMI (--flash 1,1,{start})")
    sb = FlashSandbox(["--seconds", "120", "--flash", f"1,1,{start}"], xxmi=True)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        client.live(enable=True, keep_frames=1200)
        time.sleep(1.0)
        frame = client.status()["frame"]
        if not check("armed well before the flash", frame < start - 300, f"frame {frame}"):
            return
        state = client.trigger_arm(new_pairs=True, baseline_frames=60, origin="game", capture={"detail": "full"})
        check("a new-pair trigger starts with a baseline", state.get("state") in ("arming", "baseline"), str(state))
        check("the baseline ends", wait_state(client, "armed", 5.0) == "armed")
        time.sleep(1.0)
        check("nothing new fires before the flash, XXMI's text notwithstanding",
              trigger_state(client) == "armed" and not sb.flashes(), f"{trigger_state(client)} {sb.flashes()}")
        state, doc, blob = client.trigger_wait(wait_ms=60000)
        fired = state.get("fired") or {}
        if not check("the flash fires it as a new pair", doc is not None and fired.get("what") == "new pair"
                     and fired.get("origin") == "game" and fired.get("count") == FLASH_IC, str(state)):
            return
        cap = Capture(doc, blob)
        fd = draws_of(cap, FLASH_IC)
        check("the recorded frame is the next one and draws the flash with that pair",
              cap.frame == fired["frame"] + 1 and len(fd) == 1 and fd[0]["shaders"].get("vs") == fired.get("vs")
              and fd[0]["shaders"].get("ps") == fired.get("ps"), str(fd[:1]))
        _now, frames = client.live_get(since=0, max_frames=0)
        first_seen = next((f["frame"] for f in frames if any(it[5] == FLASH_IC for it in f["items"])), None)
        check("it fired in the first frame that drew the flash", first_seen == fired["frame"],
              f"first in the stream {first_seen}, fired {fired['frame']}, sandbox {sb.flashes()}")
        time.sleep(1.5)
        _now, summ = client.live_summary(seconds=1.0, base_seconds=0, first=0, last=0, timeline=False)
        _now, back = client.live_summary(first=first_seen, last=summ["window"]["last"],
                                         base_first=first_seen - 120, base_last=first_seen - 1, timeline=False)
        fl = lines_of(back, FLASH_IC)
        check("the history shows the flash as new against the frames before it",
              len(fl) == 1 and fl[0]["base"] == 0 and fl[0]["first"] == first_seen and back["baseline"]["frames"] == 120,
              f"{fl} {back['baseline']}")
    finally:
        if client is not None:
            client.close()
        sb.quit()


# ---------------------------------------------------------------- run C
def run_c() -> None:
    print("\n== run C: the MCP tools, without XXMI (--flash 45,4)")
    sb = FlashSandbox(["--seconds", "90", "--flash", "45,4"], xxmi=False)
    client = None
    try:
        if not check("the sandbox started", sb.ready.wait(30)):
            return
        client = connect(sb.pid)
        if not check("connected to the probe", client is not None):
            return
        with tempfile.TemporaryDirectory(prefix="continuous-mcp-") as tmp:
            root = Path(tmp)
            (root / "Mods").mkdir()
            session = tools.Session(root, root / "out", root / "probe.dll")
            session.client = client            # this sandbox's probe, never another process's
            with patch.object(tools, "Session", return_value=session):
                server = tools.build_server(root, root / "out", root / "probe.dll")

            def call(name, **arguments):
                reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})
                return reply["result"]["content"][0]["text"], reply["result"]["isError"]

            text, err = call("efmi_live_start")
            check("efmi_live_start keeps 1800 frames", not err and "keeping 1800" in text, text)
            time.sleep(2.5)
            expected_ib = hex32(crc32c(flash_index_bytes()))
            text, err = call("efmi_live_history", seconds=2)
            check("efmi_live_history lists the flash as transient", not err and "transient:" in text
                  and f"ib={expected_ib} ic={FLASH_IC}" in text and "lines appear" in text
                  and "added up by the probe" in text, text)
            ps = next((part[3:] for line in text.splitlines() if f"ib={expected_ib}" in line
                       for part in line.split() if part.startswith("ps=")), "")
            when_flash_off(client)
            text, err = call("efmi_trigger_arm", shader=ps, contents="inputs", wait_s=10)
            check("efmi_trigger_arm with wait_s returns the triggered frame",
                  not err and "fired on shader" in text and "the recorded frame draws it 1 times" in text
                  and "contents: the probe keeps" in text, text)
            draw = next((int(line.split("draws [")[1].split("]")[0]) for line in text.splitlines()
                         if "the recorded frame draws it" in line), -1)
            text, err = call("efmi_read_buffer", draw=draw, slot="ib", count=50)
            try:
                got = json.loads(text)
            except ValueError:
                got = {}
            want = list(struct.unpack(f"<{FLASH_IC}H", flash_index_bytes()))
            check("efmi_read_buffer reads its index data from the kept contents",
                  not err and got.get("exact") is True and got.get("indices") == want, text)
            text, err = call("efmi_trigger_wait", wait_s=0)
            check("efmi_trigger_wait afterwards: no trigger", not err and "no trigger is armed" in text, text)
            text, err = call("efmi_live_stop")
            check("efmi_live_stop", not err, text)
    finally:
        if client is not None:
            client.close()
        sb.quit()


def main() -> int:
    run_a()
    run_b()
    run_c()
    print(f"\n{passed} passed, {len(failures)} failed")
    for name in failures:
        print(f"  FAIL {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
