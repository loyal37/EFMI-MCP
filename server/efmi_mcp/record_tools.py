"""Recordings: what the probe wrote between switching recording on and off (probe 0.1.11).

The user switches recording on and off in the game with its hotkey (Ctrl+F9 by default), or the agent with
efmi_record. The probe writes a folder of its own for every recording, also without a client:

  recording.json   when, how long, by what started and stopped, counts
  lines.jsonl      every mesh/shader line the first time it was drawn: {"id", "frame", "line": [...]}
  runs.jsonl       [line, first frame, last frame, frames, most draws, most instances, all draws]: a stretch
                   of recorded frames in which the line was drawn in every one
  open-runs.json   the runs not finished yet (only while recording, or when the game ended meanwhile)
  frames.bin       per recorded frame 5 x u32: frame, ms since the start, draws, game draws, lines
  keyframes.jsonl  frames recorded in full: at the start, when the game drew a vertex/pixel shader pair the
                   recording had not seen, and every ten seconds; keyframes\\<frame>.json/.bin

The tools here read those files, with or without the game.
"""
from __future__ import annotations

import json
import statistics
import struct
import time
from collections import Counter, defaultdict
from pathlib import Path

from .capture_model import Capture
from .fa_dump import DRAW_NAMES
from .mcp_stdio import ToolError

FEATURE = "record"
LINE = ("type", "origin", "ib", "ib_state", "ib_res", "ic", "fi", "vc", "vs", "ps")


def roots(probe_dll: Path) -> list[Path]:
    """Where the probe puts recordings: the project's captures folder next to the probe's folder, or a
    recordings folder next to the DLL ([Record] dir in efmi_probe.ini can name another)."""
    here = Path(probe_dll).parent
    return [here.parent / "captures", here / "recordings"]


def find_recordings(probe_dll: Path, extra: str = "") -> list[Path]:
    places = [Path(extra)] if extra else roots(probe_dll)
    found = []
    for place in places:
        if (place / "recording.json").exists():
            found.append(place)
        elif place.is_dir():
            found += [p for p in place.iterdir() if p.is_dir() and p.name.startswith("recording-")
                      and (p / "recording.json").exists()]
    return sorted(found, key=lambda p: p.name)


class Recording:
    def __init__(self, folder: Path):
        self.folder = folder
        try:
            self.meta = json.loads((folder / "recording.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(f"{folder} is not a readable recording: {exc}")
        self.lines = {raw["id"]: raw["line"] for raw in self._jsonl("lines.jsonl") if "id" in raw}
        self.runs = [r for r in self._jsonl("runs.jsonl") if isinstance(r, list) and len(r) >= 7]
        self.open_runs = False
        if self.meta.get("state") != "stopped":
            try:
                doc = json.loads((folder / "open-runs.json").read_text(encoding="utf-8"))
                self.runs += [r for r in doc.get("runs", []) if isinstance(r, list) and len(r) >= 7]
                self.open_runs = True
            except (OSError, ValueError):
                pass
        path = folder / "frames.bin"
        data = path.read_bytes() if path.exists() else b""
        self.frames = list(struct.iter_unpack("<5I", data[:len(data) - len(data) % 20]))
        self.index = {f[0]: i for i, f in enumerate(self.frames)}
        self.keyframes = list(self._jsonl("keyframes.jsonl"))

    def _jsonl(self, name: str):
        path = self.folder / name
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass                    # a line the end of the game cut off

    def t(self, i: int) -> float:
        return self.frames[i][1] / 1000.0

    @property
    def stopped(self) -> bool:
        return self.meta.get("state") == "stopped"

    def describe(self) -> str:
        m = self.meta
        dur = (m.get("duration_ms") or (self.frames[-1][1] if self.frames else 0)) / 1000.0
        state = (f"stopped by {m.get('stopped_by')}" if self.stopped else
                 "still recording, or the game ended before it was stopped")
        reasons = Counter(k.get("reason") for k in self.keyframes)
        kf = ", ".join(f"{n} {r}" for r, n in reasons.most_common()) or "none"
        return (f"{self.folder.name}: started {m.get('started_local')} by {m.get('started_by')}, {dur:.1f} s, "
                f"{len(self.frames)} frames recorded, {len(self.lines)} lines, {len(self.runs)} runs; keyframes: {kf}; "
                f"{state}")


def _union(runs: list[tuple]) -> list[tuple]:
    out: list[list] = []
    for a, b, most_draws, most_inst, draws in sorted(runs):
        if out and a <= out[-1][1] + 1:
            o = out[-1]
            o[1] = max(o[1], b)
            o[2], o[3], o[4] = max(o[2], most_draws), max(o[3], most_inst), o[4] + draws
        else:
            out.append([a, b, most_draws, most_inst, draws])
    return [tuple(o) for o in out]


def items_of(rec: Recording, origin: str, min_index_count: int, h: str, region_hashes: bool = True) -> list[dict]:
    """Per line its runs as indexes into the recorded frames: (first, last, most draws, most instances, draws).
    Lines without an index buffer hash (data the CPU rewrites every frame, drawn from another range each
    time) are merged by draw type, origin, buffer and shaders."""
    by_line: dict[int, list] = defaultdict(list)
    for lid, first, last, _n, most_draws, most_inst, draws in (r[:7] for r in rec.runs):
        a, b = rec.index.get(first), rec.index.get(last)
        if a is not None and b is not None and lid in rec.lines:
            by_line[lid].append((a, b, most_draws, most_inst, draws))
    out: list[dict] = []
    groups: dict[tuple, dict] = {}
    for lid, runs in by_line.items():
        line = dict(zip(LINE, rec.lines[lid]))
        if not region_hashes:
            line["ib"] = line["ib_res"]     # ZZMI and the other installations without region hashes
        if origin != "all" and (line["origin"] == "xxmi") != (origin == "xxmi"):
            continue
        if (line["ic"] or line["vc"]) < min_index_count:
            continue
        if h and h not in (line["ib"], line["ib_res"], line["vs"], line["ps"]):
            continue
        if line["ib"]:
            out.append({**line, "runs": sorted(runs), "merged": 1})
            continue
        key = (line["type"], line["origin"], line["ib_state"], line["ib_res"], line["vs"], line["ps"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {**line, "runs": [], "merged": 0}
            out.append(g)
        g["merged"] += 1
        g["runs"] += runs
        g["ic"], g["vc"] = max(g["ic"], line["ic"]), max(g["vc"], line["vc"])
    for it in out:
        it["runs"] = _union(it["runs"])
        it["first_ever"] = it["runs"][0][0]
    return out


def line_text(it: dict) -> str:
    ib = it["ib"] or it["ib_state"] or "-"
    count = f"ic={it['ic']}" if it["ic"] else f"vc={it['vc']}"
    if it["merged"] > 1:
        count = f"{it['merged']} ranges, {count.replace('=', '<=')}"
    parts = [f"{DRAW_NAMES.get(it['type'], str(it['type'])):<22}", f"{it['origin']:<5}", f"ib={ib:<8}", count]
    if it["fi"] and it["merged"] == 1:
        parts.append(f"fi={it['fi']}")
    parts.append(f"vs={it['vs'] or '-'} ps={it['ps'] or '-'}")
    most_draws = max(r[2] for r in it["runs"])
    most_inst = max(r[3] for r in it["runs"])
    if most_draws > 1:
        parts.append(f"draws<={most_draws}")
    if most_inst > 1:
        parts.append(f"inst<={most_inst}")
    return " ".join(parts)


def register(server, session, capture_report) -> None:
    s = session

    def open_recording(name: str = "") -> Recording:
        found = find_recordings(s.probe_dll, name if name and ("\\" in name or "/" in name) else "")
        if name and not ("\\" in name or "/" in name):
            found = [p for p in found if p.name == name or p.name.endswith(name)]
            if not found:
                raise ToolError(f"no recording named {name!r}; efmi_record_list lists them")
        if not found:
            raise ToolError("no recording yet: switch recording on in the game (" + _hotkey() + ") or with "
                            "efmi_record state='on', and off again when the moment is over")
        return Recording(found[-1])

    def _hotkey() -> str:
        return getattr(s, "record_hotkey", "") or "Ctrl+F9"

    # ================================================================ on / off
    @server.tool("efmi_record",
                 "Switches recording on or off, or says what it is doing (state='status'). The user can do the "
                 "same in the game with the hotkey (Ctrl+F9 by default; the game window has to be in front; a sound "
                 "says on or off). While it records, the probe writes into a folder of its own (by default "
                 "F:\\EFMI MCP\\captures\\recording-<date>-<time>), with or without a client: for every frame what "
                 "was drawn (as runs of frames per mesh/shader line), and keyframes: frames recorded in full (every "
                 "binding, hash, pipeline state and the constant buffers as each draw saw them) at the start, when "
                 "the game draws a vertex/pixel shader pair the recording has not seen yet (an effect appearing), "
                 "and every ten seconds. It costs what continuous mode costs (under a millisecond per frame) plus "
                 "about 10 ms for each keyframe; it stops by itself after an hour. Read it with "
                 "efmi_record_history and efmi_record_keyframe. Needs probe 0.1.11.",
                 {"state": {"type": "string", "enum": ["on", "off", "status"], "default": "status"}},
                 read_only=False)
    def efmi_record(state: str = "status"):
        st = s.call(lambda c: c.status())
        if FEATURE not in (st.get("features") or ()):
            raise ToolError(f"Probe {st.get('probe')} cannot record; this needs 0.1.11. Restart the game to load "
                            f"the updated {s.probe_dll}.")
        action = {"on": "start", "off": "stop"}.get(state, "status")
        r = s.call(lambda c: c.record(action, flush=action == "stop"))
        s.record_hotkey = r.get("hotkey") or ""
        lines = []
        if r.get("on"):
            lines.append(f"recording {r.get('id')} is on (started by {r.get('started_by')}, {r.get('seconds', 0):.1f} s "
                         f"ago): {r.get('frames', 0)} frames, {r.get('lines', 0)} lines, {r.get('keyframes', 0)} "
                         f"keyframes, {r.get('news', 0)} new shader pairs")
        elif r.get("id"):
            lines.append(f"recording {r.get('id')} is off (stopped by {r.get('stopped_by')} after "
                         f"{r.get('seconds', 0):.1f} s): {r.get('frames', 0)} frames, {r.get('lines', 0)} lines, "
                         f"{r.get('runs', 0)} runs, {r.get('keyframes', 0)} keyframes")
        else:
            lines.append("no recording since the game started")
        if r.get("folder"):
            lines.append(f"folder: {r['folder']} ({r.get('bytes', 0) / 1048576:.1f} MiB written"
                         + (f", {r['pending']} items still to write" if r.get("pending") else "") + ")")
        if r.get("keyframes_stopped"):
            lines.append("keyframes stopped: the limit of keyframes or disk space of a recording was reached; "
                         "the frames' summaries go on")
        if r.get("error"):
            lines.append(f"ERROR writing: {r['error']}")
        lines.append(f"hotkey in the game: {r.get('hotkey') or 'none'}")
        if not r.get("on") and r.get("id"):
            lines.append("next: efmi_record_history (what came and went, and how often), efmi_record_keyframe")
        return "\n".join(lines)

    # ================================================================ list
    @server.tool("efmi_record_list", "The recordings the probe wrote, newest last: when, how long, how many frames and "
                                     "keyframes, how they started and stopped. Reads files only; works without the game.",
                 {"folder": {"type": "string", "default": "",
                             "description": "another folder holding recordings (default: where the probe writes them)"}})
    def efmi_record_list(folder: str = ""):
        found = find_recordings(s.probe_dll, folder)
        if not found:
            places = [folder] if folder else [str(p) for p in roots(s.probe_dll)]
            return "no recordings in " + " or ".join(places)
        return "\n".join(Recording(p).describe() for p in found[-30:])

    # ================================================================ history
    @server.tool("efmi_record_history",
                 "What a recording saw, from its runs: for every mesh/shader line the stretches of frames it was drawn "
                 "in. show='events' (default) lists, in time order, the moments when lines started being drawn "
                 "together (an effect appearing, something coming into view), with how many of them were drawn for "
                 "the first time in the recording and the keyframe taken there. show='recurring' groups lines that "
                 "came and went at the same moments, e.g. an effect seen three times. show='lines' lists the lines "
                 "drawn in at most max_share of the frames with their stretches. Lines from index data the CPU "
                 "rewrites every frame ('volatile') are merged by draw type and shaders. Times are seconds since the "
                 "recording started. Reads files only.",
                 {"recording": {"type": "string", "default": "", "description": "folder name (default: the newest)"},
                  "show": {"type": "string", "enum": ["events", "recurring", "lines"], "default": "events"},
                  "from_s": {"type": "number", "minimum": 0, "default": 0},
                  "to_s": {"type": "number", "minimum": 0, "default": 0, "description": "0: to the end"},
                  "min_lines": {"type": "integer", "minimum": 1, "default": 2,
                                "description": "show='events': moments where at least this many lines start"},
                  "max_share": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5,
                                "description": "show='lines': lines drawn in at most this share of the frames"},
                  "origin": {"type": "string", "enum": ["game", "xxmi", "all"], "default": "game"},
                  "min_index_count": {"type": "integer", "minimum": 0, "default": 0},
                  "hash": {"type": "string", "default": "",
                           "description": "only lines with this index buffer (8 hex digits) or shader hash (16)"},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 40}})
    def efmi_record_history(recording: str = "", show: str = "events", from_s: float = 0, to_s: float = 0,
                            min_lines: int = 2, max_share: float = 0.5, origin: str = "game",
                            min_index_count: int = 0, hash: str = "", offset: int = 0, limit: int = 40):
        h = str(hash or "").strip().lower().removeprefix("0x")
        if h and (len(h) not in (8, 16) or any(ch not in "0123456789abcdef" for ch in h)):
            raise ToolError("hash: 8 hex digits for an index buffer, 16 for a shader")
        rec = open_recording(recording)
        if not rec.frames:
            raise ToolError(f"{rec.folder.name} has no recorded frames")
        ts = [f[1] / 1000.0 for f in rec.frames]
        i0 = next((i for i, t in enumerate(ts) if t >= from_s), len(ts) - 1)
        end = to_s if to_s > 0 else ts[-1]
        i1 = max(i0, max((i for i, t in enumerate(ts) if t <= end), default=i0))
        n = i1 - i0 + 1
        items = items_of(rec, origin, int(min_index_count), h, s.region_hashes)
        lines = [rec.describe(),
                 f"window {ts[i0]:.2f}-{ts[i1]:.2f} s: {n} recorded frames"
                 + (f" ({(n - 1) / (ts[i1] - ts[i0]):.0f} fps)" if ts[i1] > ts[i0] else "")
                 + f"; {len(items)} lines of origin '{origin}'" + (f" with {h}" if h else "")]
        if rec.open_runs:
            lines.append("note: the recording was not stopped; runs still open are taken from open-runs.json "
                         "(up to five seconds old)")
        limit = max(1, min(int(limit), 300))

        def kf_near(t: float, within: float = 1.5) -> str:
            ks = [k for k in rec.keyframes if t - 0.05 <= k.get("t_ms", 0) / 1000.0 <= t + within]
            if not ks:
                return ""
            k = ks[0]
            return f"; keyframe {k['frame']} at {k['t_ms'] / 1000.0:.2f} s ({k['reason']})"

        # the moments lines started at, from every run that starts inside the window and not at the very start
        starts = sorted(((ts[r[0]], it, r) for it in items for r in it["runs"] if i0 <= r[0] <= i1 and r[0] > 0),
                        key=lambda x: x[0])
        clusters: list[list] = []
        for t, it, r in starts:
            if not clusters or t - clusters[-1][0][0] > 0.15:
                clusters.append([])
            clusters[-1].append((t, it, r))
        cluster_of = {}
        for ci, cl in enumerate(clusters):
            for _t, it, r in cl:
                cluster_of[(id(it), r[0])] = ci

        def dur(r) -> float:
            return ts[r[1]] - ts[r[0]] + (ts[1] - ts[0] if len(ts) > 1 else 0)

        if show == "events":
            picked = [cl for cl in clusters if len({id(it) for _t, it, _r in cl}) >= min_lines]
            lines.append(f"events: {len(picked)} moments where at least {min_lines} lines started together "
                         f"(of {len(clusters)} with any)")
            for cl in picked[offset:offset + limit]:
                t = cl[0][0]
                new = [(it, r) for _t, it, r in cl if r[0] == it["first_ever"]]
                durs = [dur(r) for _t, _it, r in cl]
                lines.append(f"at {t:.2f} s: {len(cl)} lines start, {len(new)} of them for the first time; "
                             f"drawn {statistics.median(durs):.2f} s (median), up to {max(durs):.2f} s" + kf_near(t))
                shown = sorted(cl, key=lambda x: (x[2][0] != x[1]["first_ever"], -(x[1]["ic"] or x[1]["vc"])))
                for _t, it, r in shown[:6]:
                    tag = " [first time]" if r[0] == it["first_ever"] else ""
                    lines.append(f"    {dur(r):5.2f} s  {line_text(it)}{tag}{s.mod_note(it['ib'])}")
                if len(cl) > 6:
                    lines.append(f"    ... {len(cl) - 6} more")
            if offset + limit < len(picked):
                lines.append(f"... {len(picked) - offset - limit} more moments (offset={offset + limit})")
        elif show == "recurring":
            groups: dict[tuple, list] = defaultdict(list)
            for it in items:
                sig = tuple(sorted({cluster_of[(id(it), r[0])] for r in it["runs"] if (id(it), r[0]) in cluster_of}))
                if len(sig) >= 2:
                    groups[sig].append(it)
            ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            lines.append(f"recurring: {len(ordered)} groups of lines that started at the same moments, two or more "
                         "times (largest groups first)")
            for sig, its in ordered[offset:offset + limit]:
                when = []
                ids = {id(x) for x in its}
                for ci in sig[:12]:
                    cl = clusters[ci]
                    ds = [dur(r) for _t, it2, r in cl if id(it2) in ids]
                    when.append(f"{cl[0][0]:.2f} s ({max(ds):.2f} s)")
                lines.append(f"{len(sig)} times, {len(its)} lines: at " + ", ".join(when)
                             + (" ..." if len(sig) > 12 else ""))
                for it in its[:6]:
                    lines.append(f"    {line_text(it)}{s.mod_note(it['ib'])}")
                if len(its) > 6:
                    lines.append(f"    ... {len(its) - 6} more")
            if offset + limit < len(ordered):
                lines.append(f"... {len(ordered) - offset - limit} more groups (offset={offset + limit})")
        else:
            rows = []
            for it in items:
                inside = [(max(a, i0), min(b, i1)) for a, b, *_ in it["runs"] if b >= i0 and a <= i1]
                frames = sum(b - a + 1 for a, b in inside)
                if inside and (h or frames <= max_share * n):
                    rows.append((inside[0][0], frames, inside, it))
            rows.sort(key=lambda x: (x[0], -x[1]))
            lines.append(f"lines: {len(rows)} drawn in at most {max_share:.0%} of the frames, by when they first appear"
                         if not h else f"lines with {h}: {len(rows)}")
            for _first, frames, inside, it in rows[offset:offset + limit]:
                spans = ", ".join(f"{ts[a]:.2f}-{ts[b]:.2f}" for a, b in inside[:6]) + (" ..." if len(inside) > 6 else "")
                lines.append(f"  {frames}/{n} frames in {len(inside)} stretch(es): {spans} s")
                lines.append(f"    {line_text(it)}{s.mod_note(it['ib'])}")
            if offset + limit < len(rows):
                lines.append(f"... {len(rows) - offset - limit} more (offset={offset + limit})")
        lines.append("next: efmi_record_keyframe lists the keyframes and loads one as a capture")
        return "\n".join(lines)

    # ================================================================ keyframes
    @server.tool("efmi_record_keyframe",
                 "The keyframes of a recording (frames it recorded in full) or, with frame / at_s, one of them loaded as "
                 "a capture: efmi_list_draws, efmi_get_draw, efmi_get_texture_slots and efmi_diff_captures work on it, "
                 "and constant buffers read back as each draw saw them. Other buffers and textures were not kept with "
                 "it: reading them needs a capture made while the game draws them. Works without the game, except for "
                 "shader listings.",
                 {"recording": {"type": "string", "default": "", "description": "folder name (default: the newest)"},
                  "frame": {"type": "integer", "default": 0, "description": "the keyframe's frame number"},
                  "at_s": {"type": "number", "default": -1,
                           "description": "or the first keyframe at or after this time (seconds since the start)"}},
                 read_only=False)
    def efmi_record_keyframe(recording: str = "", frame: int = 0, at_s: float = -1):
        rec = open_recording(recording)
        if not rec.keyframes:
            raise ToolError(f"{rec.folder.name} has no keyframes")
        if not frame and at_s < 0:
            lines = [rec.describe(), f"{len(rec.keyframes)} keyframes:"]
            for k in rec.keyframes[:200]:
                size = (k.get("json_bytes", 0) + k.get("bin_bytes", 0)) / 1048576
                lines.append(f"  frame {k['frame']} at {k['t_ms'] / 1000.0:7.2f} s  {k['reason']:<16} {k.get('draws', 0):5d} "
                             f"draws, {size:.1f} MiB" + (f"  ({k['what']})" if k.get("what") else ""))
            lines.append("load one with frame=<frame> or at_s=<seconds>")
            return "\n".join(lines)
        if frame:
            k = next((k for k in rec.keyframes if k["frame"] == int(frame)), None)
            if k is None:
                raise ToolError(f"{rec.folder.name} has no keyframe at frame {frame}")
        else:
            k = next((k for k in rec.keyframes if k["t_ms"] / 1000.0 >= float(at_s)), rec.keyframes[-1])
        t0 = time.perf_counter()
        try:
            doc = json.loads((rec.folder / k["json"]).read_text(encoding="utf-8"))
            blob = (rec.folder / k["bin"]).read_bytes()
        except (OSError, ValueError) as exc:
            raise ToolError(f"keyframe {k['frame']} cannot be read: {exc}")
        cap = Capture(doc, blob)
        cap.held = False
        s.add_capture(cap)
        lines = [f"keyframe {k['frame']} of {rec.folder.name}, at {k['t_ms'] / 1000.0:.2f} s ({k['reason']}"
                 + (f": {k['what']}" if k.get("what") else "") + "), loaded as capture "
                 f"{cap.frame}, now the latest",
                 "kept with it: every draw's bindings, hashes, pipeline state and the constant buffers as each draw "
                 "saw them; not kept: the contents of other buffers and of textures"]
        lines += capture_report(s, cap, (time.perf_counter() - t0) * 1000)
        return "\n".join(lines)
