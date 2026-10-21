"""Continuous mode over time: what was drawn in the last seconds, and a capture that starts by itself.

efmi_live_history adds the stream up over a stretch of time, so that something that is drawn only for a
moment (a boss's skill, a hit effect) shows up as a line that came and went. efmi_trigger_arm has the probe
watch every frame for a mesh or shader, or for a vertex/pixel shader pair it has not seen during a baseline,
and record the next frame in full as soon as it appears; efmi_trigger_wait collects that capture.
"""
from __future__ import annotations

import statistics
import time
from collections import OrderedDict

from . import contents, draw_outputs
from .capture_model import Capture
from .fa_dump import DRAW_NAMES
from .mcp_stdio import ToolError

SUMMARY_FEATURE = "live_summary"
TRIGGER_FEATURE = "trigger"
KEEP_FRAMES = 1800

# The columns of a summary row (probe 0.1.10, or summarize_frames for older probes).
ROW = ("type", "origin", "ib", "ib_state", "ib_res", "ic", "fi", "vc", "vs", "ps",
       "frames", "first", "last", "max_draws", "max_inst", "draws", "base")


def parse_hashes(text, digits: int, what: str) -> list[str]:
    """Hashes given as one string (separated by commas or spaces) or as a list."""
    items = text if isinstance(text, (list, tuple)) else str(text or "").replace(",", " ").split()
    out = []
    for item in items:
        h = str(item).strip().lower().removeprefix("0x")
        if not h:
            continue
        if len(h) != digits or any(ch not in "0123456789abcdef" for ch in h):
            raise ToolError(f"{what} hashes have {digits} hex digits, not {item!r}")
        if h not in out:
            out.append(h)
    return out


def summarize_frames(frames: list[dict], first: int = 0, last: int = 0, base_first: int = 0, base_last: int = 0,
                     seconds: float = 0, base_seconds: float = 0) -> dict:
    """What the probe's live_summary returns, worked out from live_get frames (probes before 0.1.10)."""
    frames = sorted(frames, key=lambda f: f["frame"])
    ring = {"first": frames[0]["frame"] if frames else 0, "last": frames[-1]["frame"] if frames else 0,
            "frames": len(frames)}
    if frames and seconds > 0:
        end = frames[-1]["t_ms"]
        i = len(frames) - 1
        while i > 0 and end - frames[i - 1]["t_ms"] <= seconds * 1000:
            i -= 1
        first, last, base_first, base_last = frames[i]["frame"], frames[-1]["frame"], 0, 0
        if base_seconds > 0 and i > 0:
            start = frames[i]["t_ms"]
            j = i
            while j > 0 and start - frames[j - 1]["t_ms"] <= base_seconds * 1000:
                j -= 1
            if j < i:
                base_first, base_last = frames[j]["frame"], frames[i - 1]["frame"]
    first = first or ring["first"]
    last = last or ring["last"]
    agg: dict[tuple, list] = {}
    window = [f for f in frames if first <= f["frame"] <= last]
    baseline = [f for f in frames if base_first and base_first <= f["frame"] <= base_last]
    for f in baseline:
        for key in {tuple(it[:10]) for it in f["items"]}:
            agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0])[6] += 1
    for f in window:
        lines: dict[tuple, list] = {}
        for it in f["items"]:
            line = lines.setdefault(tuple(it[:10]), [0, 0])
            line[0] += it[10]
            line[1] += it[11]
        for key, (draws, inst) in lines.items():
            a = agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0])
            if not a[0]:
                a[1] = f["frame"]
            a[0] += 1
            a[2] = f["frame"]
            a[3] = max(a[3], draws)
            a[4] = max(a[4], inst)
            a[5] += draws
    return {"ring": ring,
            "window": {"first": first, "last": last, "frames": len(window),
                       "t_first": window[0]["t_ms"] if window else 0, "t_last": window[-1]["t_ms"] if window else 0},
            "baseline": {"first": base_first, "last": base_last, "frames": len(baseline)},
            "items": [list(key) + a for key, a in agg.items() if a[0]],
            "stale": False,
            "timeline": [[f["frame"], f["t_ms"], f["draws"], f["game_draws"], len(f["items"])] for f in window]}


def _row(it: list) -> dict:
    return dict(zip(ROW, it))


def _line_text(r: dict, note: str = "") -> str:
    ib = r["ib"] or r["ib_state"] or "-"
    parts = [f"{DRAW_NAMES.get(r['type'], str(r['type'])):<22}", f"{r['origin']:<5}", f"ib={ib:<8}",
             f"ic={r['ic']}" if r["ic"] else f"vc={r['vc']}"]
    if r["fi"]:
        parts.append(f"fi={r['fi']}")
    parts.append(f"vs={r['vs'] or '-'} ps={r['ps'] or '-'}")
    if r["max_draws"] > 1:
        parts.append(f"draws<={r['max_draws']}")
    if r["max_inst"] > 1:
        parts.append(f"inst<={r['max_inst']}")
    return " ".join(parts) + note


def register(server, session, capture_report) -> None:
    s = session
    s.trigger = None                 # what this server last armed, for the reports

    def features() -> tuple[dict, set]:
        st = s.call(lambda c: c.status())
        return st, set(st.get("features") or ())

    # ================================================================ history
    @server.tool("efmi_live_history",
                 "What the continuous mode saw over a stretch of time, added up per mesh/shader line: in how many frames "
                 "each line was drawn, when it first and last appeared, and whether it was drawn during an earlier "
                 "baseline. show='transient' (the default) lists the lines drawn in at most max_share of the frames, "
                 "grouped by the moment they appeared: something that is on screen only briefly, like a boss's skill "
                 "or a hit effect, shows up there. show='new' lists the lines not drawn at all during the baseline "
                 "(baseline_seconds before the window), show='all' every line. The window is the last `seconds` of "
                 "the stream, or frames first..last. Needs efmi_live_start some time before the moment of interest: "
                 "the probe keeps keep_frames frames (30 s at 60 fps by default). Index buffers rewritten every frame "
                 "have no hash ('volatile'); their lines are told apart by shaders and counts. Reads only.",
                 {"seconds": {"type": "number", "minimum": 0, "default": 5.0,
                              "description": "the last this many seconds of the stream (0: use first/last)"},
                  "baseline_seconds": {"type": "number", "minimum": 0, "default": 0,
                                       "description": "this many seconds before the window count as the baseline"},
                  "first": {"type": "integer", "default": 0, "description": "first frame (seconds=0)"},
                  "last": {"type": "integer", "default": 0, "description": "last frame (seconds=0)"},
                  "show": {"type": "string", "enum": ["transient", "new", "all"], "default": "transient",
                           "description": "with hash given, every line that has it is shown unless set"},
                  "max_share": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5,
                                "description": "show='transient': lines drawn in at most this share of the frames"},
                  "origin": {"type": "string", "enum": ["game", "xxmi", "all"], "default": "game"},
                  "min_index_count": {"type": "integer", "minimum": 0, "default": 0},
                  "hash": {"type": "string", "default": "",
                           "description": "only lines with this index buffer (8 hex digits) or shader hash (16)"},
                  "timeline": {"type": "boolean", "default": False,
                               "description": "also list draws per quarter second"},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60}})
    def efmi_live_history(seconds: float = 5.0, baseline_seconds: float = 0, first: int = 0, last: int = 0,
                          show: str = "", max_share: float = 0.5, origin: str = "game", min_index_count: int = 0,
                          hash: str = "", timeline: bool = False, offset: int = 0, limit: int = 60):
        h = str(hash or "").strip().lower().removeprefix("0x")
        if h and (len(h) not in (8, 16) or any(ch not in "0123456789abcdef" for ch in h)):
            raise ToolError("hash: 8 hex digits for an index buffer, 16 for a shader")
        show = show or ("all" if h else "transient")
        seconds, baseline_seconds = max(0.0, float(seconds)), max(0.0, float(baseline_seconds))
        if not seconds and not first and not last:
            seconds = 5.0
        _st, feats = features()
        if SUMMARY_FEATURE in feats:
            _now, summ = s.call(lambda c: c.live_summary(first=int(first), last=int(last), seconds=seconds,
                                                         base_seconds=baseline_seconds, timeline=True))
            source = "probe"
        else:
            _now, frames = s.call(lambda c: c.live_get(since=0, max_frames=0))
            summ = summarize_frames(frames, int(first), int(last), 0, 0, seconds, baseline_seconds)
            source = "server (probe before 0.1.10)"
        if summ.get("stale"):
            raise ToolError("the stream was restarted or cleared while it was being added up: ask again")
        ring, win, base = summ["ring"], summ["window"], summ["baseline"]
        if not ring["frames"]:
            raise ToolError("the continuous mode has no frames: call efmi_live_start first, some time before the "
                            "moment of interest (and the game has to be rendering)")
        if not win["frames"]:
            raise ToolError(f"no kept frame in that window; the stream keeps frames {ring['first']}-{ring['last']}")
        if show == "new" and not base["frames"]:
            raise ToolError("show='new' compares with a baseline: give baseline_seconds (the stream has to reach "
                            "back that far before the window)")
        ticks = summ.get("timeline") or []
        t_of = {t[0]: t[1] for t in ticks}
        t0 = win["t_first"] or (ticks[0][1] if ticks else 0)
        span = max(0.0, (win["t_last"] - win["t_first"]) / 1000.0)
        fps = (win["frames"] - 1) / span if span > 0 else 0.0

        def at(frame: int) -> str:
            t = t_of.get(frame)
            return f"+{(t - t0) / 1000.0:.2f}s" if t is not None else f"frame {frame}"

        rows = [_row(it) for it in summ["items"]]
        if origin != "all":
            rows = [r for r in rows if (r["origin"] == "xxmi") == (origin == "xxmi")]
        rows = [r for r in rows if (r["ic"] or r["vc"]) >= min_index_count]
        if h:
            rows = [r for r in rows if h in (r["ib"], r["ib_res"], r["vs"], r["ps"])]
        n = win["frames"]
        lines = [f"stream kept by the probe: frames {ring['first']}-{ring['last']} ({ring['frames']} frames); "
                 f"window: frames {win['first']}-{win['last']}, {n} frames over {span:.2f} s"
                 + (f" ({fps:.0f} fps)" if fps else "")
                 + (f"; baseline: frames {base['first']}-{base['last']} ({base['frames']} frames)" if base["frames"] else "")
                 + f"; added up by the {source}"]
        if ticks:
            draws = [t[2] for t in ticks]
            lines.append(f"draws per frame: {min(draws)}-{max(draws)} (median {statistics.median(draws):.0f}); "
                         f"{len(rows)} distinct lines of origin '{origin}'" + (f" with {h}" if h else ""))
        if ring["frames"] and win["first"] <= ring["first"] and seconds and span + 0.05 < seconds:
            lines.append(f"note: the stream reaches back only {span:.2f} s; start it earlier or keep more frames")

        if show == "transient":
            picked = [r for r in rows if r["frames"] <= max_share * n]
            picked.sort(key=lambda r: (r["first"], r["last"]))
            lines.append(f"transient: {len(picked)} lines drawn in at most {max_share:.0%} of the {n} frames, "
                         "grouped by when they appeared")
        elif show == "new":
            picked = [r for r in rows if not r["base"]]
            picked.sort(key=lambda r: (r["first"], r["last"]))
            lines.append(f"new: {len(picked)} lines not drawn in any of the {base['frames']} baseline frames, "
                         "grouped by when they appeared")
        else:
            picked = sorted(rows, key=lambda r: (-r["frames"], r["first"]))
            lines.append(f"all: {len(picked)} lines, most frames first")
        page = picked[offset:offset + max(1, min(int(limit), 300))]
        group_start = None
        for r in page:
            if show != "all" and (group_start is None or r["first"] > group_start + 6):
                group_start = r["first"]
                together = [x for x in picked if group_start <= x["first"] <= group_start + 6]
                gone = max(x["last"] for x in together)
                lines.append(f"at {at(group_start)} (frame {group_start}): {len(together)} lines appear"
                             + (", still drawn at the end" if gone >= win["last"] else f", last drawn at {at(gone)}"))
            note = ""
            if base["frames"]:
                note = " [not in baseline]" if not r["base"] else f" [baseline {r['base']}/{base['frames']}]"
            lines.append(f"  {at(r['first'])}..{at(r['last'])} {r['frames']}/{n} frames  "
                         + _line_text(r, note + s.mod_note(r["ib"])))
        if offset + len(page) < len(picked):
            lines.append(f"... {len(picked) - offset - len(page)} more (offset={offset + len(page)})")
        if any(r["ib_state"] == "volatile" for r in page):
            lines.append("('volatile' index buffers are rewritten every frame: they have no stable hash)")
        if timeline and ticks:
            lines.append("timeline (per quarter second: frames, draws min-max, lines max):")
            buckets: "OrderedDict[int, list]" = OrderedDict()
            for t in ticks:
                buckets.setdefault(int((t[1] - t0) // 250), []).append(t)
            for b, ts in list(buckets.items())[:240]:
                lines.append(f"  +{b * 0.25:.2f}s {len(ts):>3} frames, draws {min(t[2] for t in ts)}-"
                             f"{max(t[2] for t in ts)}, lines <= {max(t[4] for t in ts)}")
        lines.append("next: efmi_trigger_arm with one of these hashes (or new_pairs=true) records such a moment in full")
        return "\n".join(lines)

    # ================================================================ trigger
    def describe(spec: dict) -> str:
        what = []
        if spec.get("ib"):
            what.append("index buffer " + ", ".join(spec["ib"]))
        if spec.get("shader"):
            what.append("shader " + ", ".join(spec["shader"]))
        if spec.get("new_pairs"):
            what.append(f"a vertex/pixel shader pair not drawn during a baseline of {spec['baseline_frames']} frames")
        text = " or ".join(what)
        if spec.get("origin"):
            text += f" (draws of origin '{spec['origin']}')"
        if spec.get("min_count"):
            text += f" (draws of at least {spec['min_count']} indices/vertices)"
        return text

    def state_text(st: dict) -> str:
        state = st.get("state", "?")
        if state == "off":
            return "no trigger is armed"
        text = f"trigger {st.get('id')} is {state}"
        if "watched_frames" in st:
            text += (f": {st['watched_frames']} frames watched, armed {st.get('armed_ms_ago', 0) / 1000.0:.1f} s ago "
                     f"at frame {st.get('armed_frame')}")
        return text

    def fired_text(f: dict) -> str:
        what = {"ib": f"index buffer {f.get('ib')}", "shader": f"shader {f.get('shader')}",
                "new pair": f"new shader pair vs={f.get('vs')} ps={f.get('ps')}"}.get(f.get("what"), f.get("what"))
        text = (f"fired on {what}: frame {f.get('frame')}, draw #{f.get('draw')} "
                f"({DRAW_NAMES.get(f.get('type'), f.get('type'))}, origin {f.get('origin')}, count {f.get('count')}, "
                f"vs={f.get('vs') or '-'} ps={f.get('ps') or '-'}), {f.get('ms_ago', 0) / 1000.0:.1f} s ago")
        if f.get("late"):
            text += ("; the hash was known only once that frame's index data had been read back, so a later frame "
                     "was recorded")
        return text

    def present_in(cap: Capture, spec: dict, fired: dict) -> str:
        ibs, shaders = set(spec.get("ib") or ()), set(spec.get("shader") or ())
        pair = (fired.get("vs"), fired.get("ps")) if fired.get("what") == "new pair" else None
        hits = []
        for d in cap.draws():
            ib = (d.get("ib") or {}).get("hash")
            sh = set(d["shaders"].values())
            if (ib and ib in ibs) or (sh & shaders) or (pair and (d["shaders"].get("vs"), d["shaders"].get("ps")) == pair):
                hits.append(d["index"])
        if hits:
            return (f"the recorded frame draws it {len(hits)} times: draws {hits[:12]}"
                    + (" ..." if len(hits) > 12 else ""))
        return ("WARNING: the recorded frame does not draw it: it lasted only one frame (or delay_frames was too "
                "long); the stream (efmi_live_history) still shows when it was drawn")

    @server.tool("efmi_trigger_arm",
                 "Has the probe watch every frame and record the next frame in full as soon as something appears: a mesh "
                 "(index buffer hash, as in efmi_live_history / mod INIs), a shader hash, or with new_pairs=true any "
                 "vertex/pixel shader pair that was not drawn during a baseline of baseline_frames frames (switch it on "
                 "while nothing special happens: the next new effect, e.g. a boss's skill, fires it). The frame after "
                 "the one it was seen in (plus delay_frames) is recorded with the capture options given here, contents "
                 "included, and becomes the latest capture; collect it with efmi_trigger_wait. A hash first seen in a "
                 "frame is known only once that frame's index data has been read back, so such a trigger records a "
                 "frame or two later ('late'); shader hashes and index buffers seen before fire without delay. Index "
                 "buffers rewritten every frame have no hash: watch their shaders instead. While it watches, every "
                 "frame is recorded at minimal detail, like the continuous mode. One trigger at a time; arming again "
                 "replaces it. It stops when it fires, expires, is cancelled or this server disconnects. Needs probe "
                 "0.1.10.",
                 {"ib": {"type": "string", "default": "", "description": "index buffer hashes (8 hex digits), comma separated"},
                  "shader": {"type": "string", "default": "", "description": "shader hashes (16 hex digits) of any stage"},
                  "new_pairs": {"type": "boolean", "default": False},
                  "baseline_frames": {"type": "integer", "minimum": 1, "maximum": 36000, "default": 120},
                  "origin": {"type": "string", "enum": ["", "game", "xxmi", "any"], "default": "",
                             "description": "which draws count; '' is 'game' with new_pairs (XXMI's own text changes) "
                                            "and 'any' otherwise"},
                  "min_index_count": {"type": "integer", "minimum": 0, "default": 0,
                                      "description": "draws with fewer indices (vertices when not indexed) do not count"},
                  "delay_frames": {"type": "integer", "minimum": 0, "maximum": 600, "default": 0},
                  "expire_s": {"type": "number", "minimum": 0, "maximum": 86400, "default": 600,
                               "description": "give up after this long (0: never)"},
                  "detail": {"type": "string", "enum": ["full", "light"], "default": "full"},
                  "cb_data": {"type": "boolean", "default": False},
                  "contents": {"type": "string", "enum": list(contents.LEVELS), "default": "none",
                               "description": "as in efmi_capture_frame; 'outputs' keeps what the triggering shaders' "
                                              "draws wrote (or, for an index buffer, that mesh's draws in the latest "
                                              "capture)"},
                  "budget_mb": {"type": "integer", "minimum": 16, "maximum": 8192, "default": contents.BUDGET_MB},
                  "wait_s": {"type": "number", "minimum": 0, "maximum": 300, "default": 0,
                             "description": "wait this long for it right away, as efmi_trigger_wait does"}},
                 read_only=False)
    def efmi_trigger_arm(ib: str = "", shader: str = "", new_pairs: bool = False, baseline_frames: int = 120,
                         origin: str = "", min_index_count: int = 0, delay_frames: int = 0, expire_s: float = 600,
                         detail: str = "full", cb_data: bool = False, contents: str = "none",
                         budget_mb: int = contents.BUDGET_MB, wait_s: float = 0):
        ibs = parse_hashes(ib, 8, "index buffer")
        shaders = parse_hashes(shader, 16, "shader")
        if not ibs and not shaders and not new_pairs:
            raise ToolError("give ib, shader or new_pairs=true: the trigger has to watch for something")
        st, feats = features()
        if TRIGGER_FEATURE not in feats:
            raise ToolError(f"Probe {st.get('probe')} has no trigger; it needs 0.1.10. Restart the game to load the "
                            f"updated {s.probe_dll}. Meanwhile efmi_live_start + efmi_live_history show what was drawn.")
        origin = origin if origin in ("game", "xxmi") else "" if origin == "any" else ("game" if new_pairs else "")
        wanted = None
        if contents not in (None, "", "none"):
            if contents == "outputs":
                matchers = [{"sh": h} for h in shaders]
                for h in ibs:
                    if s.latest is None:
                        raise ToolError("contents='outputs' for an index buffer finds its draws in the latest capture: "
                                        "there is none (use shader hashes, or contents='inputs')")
                    matchers += draw_outputs.select(s.capture("latest"), h, None)[0]
                if not matchers:
                    raise ToolError("contents='outputs' needs shader or index buffer hashes to choose the draws; "
                                    "with new_pairs use contents='inputs'")
                wanted = _contents_request("outputs", matchers, budget_mb)
            else:
                wanted = _contents_request(contents, None, budget_mb)
        capture = {"detail": detail, "hash_ib": True, "hash_vb": True, "cb_data": bool(cb_data), "hold": True,
                   "timeout_ms": 8000}
        if wanted:
            capture["contents"] = wanted
        spec = {"ib": ibs, "shader": shaders, "new_pairs": bool(new_pairs), "baseline_frames": int(baseline_frames),
                "origin": origin, "min_count": int(min_index_count), "delay_frames": int(delay_frames),
                "expire_ms": int(float(expire_s) * 1000), "capture": capture}
        state = s.call(lambda c: c.trigger_arm(**spec))
        s.trigger = {**spec, "contents": contents, "armed_at": time.time()}
        lines = [f"trigger {state.get('id')} armed at frame {state.get('frame')}: watching for {describe(spec)}",
                 f"then the next frame{f' + {delay_frames}' if delay_frames else ''} is recorded ({detail}"
                 + (f", contents {contents}" if wanted else "") + (", constant buffers" if cb_data else "") + ")"
                 + (f"; gives up after {float(expire_s):.0f} s" if expire_s else "")]
        if wait_s:
            lines += wait(float(wait_s))
        else:
            lines.append("collect it with efmi_trigger_wait (efmi_trigger_cancel disarms it)")
        return "\n".join(lines)

    def _contents_request(level: str, matchers, budget_mb: int) -> dict | None:
        return contents.request(level, matchers, -1, -1, False, budget_mb)

    def wait(wait_s: float) -> list[str]:
        t_start = time.perf_counter()
        deadline = t_start + max(0.0, min(wait_s, 300.0))
        while True:
            left = int(max(0.0, deadline - time.perf_counter()) * 1000)
            state, doc, blob = s.call(lambda c: c.trigger_wait(wait_ms=min(left, 60000)))
            if doc is not None or state.get("state") not in ("arming", "baseline", "armed", "fired", "recording") \
                    or time.perf_counter() >= deadline:
                break
        if doc is None:
            st = state.get("state")
            if st == "expired":
                return [f"the trigger expired after {state.get('armed_ms_ago', 0) / 1000.0:.0f} s without firing "
                        f"({state.get('watched_frames', 0)} frames watched)"]
            if st == "off":
                return ["no trigger is armed (it fired and was collected, was cancelled, or this server reconnected)"]
            lines = [state_text(state) + "; nothing recorded yet"]
            if state.get("fired"):
                lines.append(fired_text(state["fired"]) + "; its frame is being recorded: call efmi_trigger_wait again")
            return lines
        cap = Capture(doc, blob)
        s.add_capture(cap)
        fired = state.get("fired") or {}
        spec = s.trigger or {}
        lines = [fired_text(fired) if fired else f"trigger {state.get('id')} recorded a frame",
                 f"recorded frame {cap.frame} (capture {cap.frame}, now the latest), "
                 f"{cap.frame - fired.get('frame', cap.frame)} frame(s) after the one it was seen in; "
                 f"{state.get('watched_frames', 0)} frames were watched before",
                 present_in(cap, spec, fired)]
        lines += capture_report(s, cap, (time.perf_counter() - t_start) * 1000)
        return lines

    @server.tool("efmi_trigger_wait",
                 "Waits for the armed trigger (efmi_trigger_arm) to fire and its frame to be recorded, for up to wait_s "
                 "seconds, then returns the capture's summary like efmi_capture_frame, with what fired it: which draw of "
                 "which frame, and whether the recorded frame still draws it. The capture is the latest one for the "
                 "other tools. Without a capture yet it says what the trigger is doing; wait_s=0 only looks.",
                 {"wait_s": {"type": "number", "minimum": 0, "maximum": 300, "default": 30}})
    def efmi_trigger_wait(wait_s: float = 30):
        return "\n".join(wait(float(wait_s)))

    @server.tool("efmi_trigger_cancel", "Disarms the trigger. A frame it recorded and nobody collected is let go.",
                 read_only=False)
    def efmi_trigger_cancel():
        _st, feats = features()
        if TRIGGER_FEATURE not in feats:
            return "this probe has no trigger (needs 0.1.10)"
        before = s.call(lambda c: c.trigger_wait(wait_ms=0))
        if before[1] is not None:
            s.add_capture(Capture(before[1], before[2]))
            s.call(lambda c: c.trigger_cancel())
            return (f"the trigger had already recorded frame {before[1]['frame']}: kept as the latest capture; "
                    "the trigger is off")
        s.call(lambda c: c.trigger_cancel())
        was = before[0].get("state", "off")
        return "no trigger was armed" if was == "off" else f"trigger {before[0].get('id')} cancelled (it was {was})"
