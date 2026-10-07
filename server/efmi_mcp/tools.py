"""The MCP tools: live access to what the game is drawing."""
from __future__ import annotations

import json
import struct
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from . import __version__, decode, model_tools
from .capture_model import Capture
from .fa_dump import DRAW_NAMES, find_frame_dumps, parse_frame_dump
from .hashes import crc32c, hex32, index_region
from . import blender_runner, loyal_bridge
from .mcp_stdio import Image, Server, ToolError
from .model_jobs import JobManager
from .mods import ModCache
from .mods_toggle import toggle_mods
from .parity import compare_sets
from .probe_client import ProbeClient, ProbeError, ProbeNotFound, find_probes
from .reload_workflow import reload_and_capture
from .shader import disassemble

INSTRUCTIONS = """\
Live view of what Arknights: Endfield is drawing, taken from inside the running game by a probe DLL that sits \
next to EFMI/XXMI (3DMigoto). Normal capture replaces frame-analysis dumps without pressing a hotkey or \
asking the game to write a dump. Two separate tools deliberately press a key inside the game: \
efmi_reload_and_capture (F10) and efmi_toggle_mods (F11).

Hashes are the ones XXMI uses: the `ib` hash of a draw is the region hash that mods put in \
`[TextureOverride...] hash = ...`; shader hashes are 16 hex digits; texture and buffer hashes are 8.

Typical flow: efmi_status -> efmi_capture_frame -> efmi_list_draws / efmi_find_hash -> efmi_get_draw -> \
efmi_read_buffer / efmi_read_texture / efmi_get_shader. For "what is on screen right now, continuously" use \
efmi_live_start, then efmi_live_now or efmi_watch_hash.

After changing mod INI/BUF files, use efmi_reload_and_capture. It brings the game to the front, has the probe \
press the configured unmodified F10 inside the game, hands the focus back and retains a fresh frame. (The game \
runs as administrator: a key sent from outside is dropped by Windows without an error, so only the probe can \
press it.) This stops continuous capture and invalidates old captures. It changes game state and must not be \
used just to get newer data: ordinary capture is already live.
reload_confirmed is true only when the fresh frame uses resources or shaders that XXMI created after the key \
press, which is what a reload causes for mods that are on screen. When it is false, or to check that an edit \
parsed as intended, inspect the draws/resources you changed. On a post-input error use efmi_capture_frame \
rather than repeating F10. A probe older than 0.1.2 cannot press the key: the error then says that the game \
has to be restarted to load the updated efmi_probe.dll.

To compare what the game draws by itself with what mods draw, use efmi_toggle_mods. The probe presses EFMI's F11 \
inside the game, which switches every mod off (state='off') or back on ('on') without reloading or touching a \
file, and the next frame is kept as 'latest'. Typical: efmi_capture_frame with mods on -> efmi_toggle_mods \
state='off' -> efmi_diff_captures a=<the first frame> -> read the original buffers or textures -> efmi_toggle_mods \
state='on'. mods_enabled in the result is what EFMI itself printed after the key press; null means unknown. With \
mods off, a character that a mod replaces is drawn by the game again, so efmi_list_models and efmi_extract_model \
work on it. Mods stay off until the next F11, a reload (F10 switches them on again) or a game restart: always \
switch them back on when you are done, and say so if you could not. Needs probe 0.1.3 or later.

Things to know:
- Capture and live mode work with hunting disabled; do not ask the user to press numpad 0 to collect data.
- origin "xxmi" marks draws and resources issued by XXMI or a mod rather than by the game (a heuristic based on \
the call stack). With a mod active the game's original draw of that mesh is replaced, so it does not appear; \
efmi_toggle_mods state='off' brings the original draws back for as long as mods are off.
- Only the most recent capture keeps its resources readable; capture again if a read says the capture is gone.
- Buffer and texture reads return the contents at the time of the read. Constant buffers as they were at each \
draw are available when the frame was captured with cb_data=true.
- Offline tools (efmi_list_mods, efmi_frame_dump_summary) work without the game.

Model workflow (LoyalTools merged-skeleton projects): efmi_list_models -> efmi_extract_model (three samples, \
LoyalTools workspace, a .blend with only the diffuse map connected) -> the user edits and saves the .blend -> \
efmi_export_mod (the add-on's own Generate Mod; the .blend as saved on disk) -> efmi_verify_exported_mod (deploys \
into its own folder under Mods, reloads once, compares what XXMI draws with the exported files). LoD levels come \
from efmi_map_lod: 'json' reuses a mapping stored in a profile JSON, 'frame' and 'capture' match the full-detail \
frame against a frame of the character at LoD distance. The full-detail and LoD draws share one edited mesh; the \
add-on generates the LoD entry points at export. These steps run as jobs: a call returns the result if it finishes \
within wait_s, otherwise a job_id for efmi_model_job; while a job uses the probe the other live tools are refused. \
Report each result with its own scope: an extraction, an export that passed its file checks, a confirmed reload \
and a verified mod are four different things.
"""

NO_PROBE = """\
The probe is not running in any process, so live data is unavailable.

The probe (efmi_probe.dll) has to be loaded when the game starts, by XXMI Launcher itself:
  XXMI Launcher -> Settings -> Advanced -> tick "Inject Libraries" (注入库) and add this path on its own line:
  {dll}
then start the game from the launcher. It cannot be attached to a game that is already running.

Tools that do not need the game still work: efmi_list_mods, efmi_frame_dump_summary.\
"""


def _norm_hash(h: str | None) -> str | None:
    if h is None:
        return None
    h = str(h).strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    return h or None


class Session:
    def __init__(self, efmi_root: Path, out_dir: Path, probe_dll: Path,
                 loyaltools_root: Path | None = None, blender_path: str = ""):
        self.efmi_root = efmi_root
        self.out_dir = out_dir
        self.probe_dll = probe_dll
        self.loyaltools_configured = Path(loyaltools_root) if loyaltools_root else None
        self.blender_path = blender_path
        self.jobs = JobManager()
        self._loyaltools: dict[str, dict] = {}
        self.client: ProbeClient | None = None
        self.captures: "OrderedDict[int, Capture]" = OrderedDict()
        self.latest: int | None = None
        self.mods = ModCache(efmi_root)
        # What EFMI reported after this server's last F11: {"enabled", "frame"}; None when unknown.
        self.mods_display: dict | None = None

    # -- LoyalTools -----------------------------------------------------
    def loyaltools(self, blender_path: str = "") -> dict:
        """The LoyalTools every step calls: {"path", "version", "origin"}.

        By default the add-on installed for the Blender in use, so the steps
        run outside Blender and the ones inside it are the same code.
        """
        key = blender_path or self.blender_path
        if key not in self._loyaltools:
            try:
                addon = None
                if not self.loyaltools_configured:
                    try:
                        addon = blender_runner.find_addon(blender_runner.find_blender(key))
                    except blender_runner.BlenderError:
                        if key:
                            raise           # a Blender that was named has to exist
                path, origin = loyal_bridge.resolve_loyaltools(self.loyaltools_configured, addon)
            except (loyal_bridge.BridgeError, blender_runner.BlenderError) as exc:
                raise ToolError(str(exc))
            self._loyaltools[key] = {"path": str(path), "version": loyal_bridge.loyaltools_version(path),
                                     "origin": origin}
        return self._loyaltools[key]

    @property
    def loyaltools_root(self) -> Path:
        return Path(self.loyaltools()["path"])

    # -- probe ----------------------------------------------------------
    def probe(self) -> ProbeClient:
        # A model job samples several frames through this connection; another
        # capture in between would release the resources it is still reading.
        job = self.jobs.active()
        if job is not None and job.uses_probe and threading.current_thread() is not job.thread:
            raise ToolError(f"The probe is in use by job {job.id} ({job.stage}). "
                            "Follow it with efmi_model_job, or cancel it there.")
        if self.client is not None and self.client.connected:
            return self.client
        try:
            self.client = ProbeClient.connect()
        except ProbeNotFound:
            raise ToolError(NO_PROBE.format(dll=self.probe_dll))
        except ProbeError as e:
            raise ToolError(f"The probe is loaded but its pipe could not be opened ({e}). "
                            "It serves one client at a time: is another tool connected to it?")
        return self.client

    def call(self, fn, *args, **kwargs):
        """Runs a probe request, turning transport problems into readable tool errors."""
        client = self.probe()
        try:
            return fn(client, *args, **kwargs)
        except ProbeError as e:
            if not client.connected:
                self.client = None
            raise ToolError(str(e))

    # -- captures -------------------------------------------------------
    def add_capture(self, cap: Capture) -> None:
        self.captures[cap.frame] = cap
        self.latest = cap.frame
        while len(self.captures) > 6:
            self.captures.popitem(last=False)

    def capture(self, ref) -> Capture:
        if ref in (None, "", "latest"):
            if self.latest is None or self.latest not in self.captures:
                raise ToolError("No capture yet. Call efmi_capture_frame first.")
            return self.captures[self.latest]
        try:
            frame = int(ref)
        except (TypeError, ValueError):
            raise ToolError(f"capture must be 'latest' or a frame number, not {ref!r}")
        if frame not in self.captures:
            raise ToolError(f"capture {frame} is not in memory (kept: {list(self.captures)})")
        return self.captures[frame]

    def frame_dir(self, cap: Capture) -> Path:
        d = self.out_dir / f"frame-{cap.frame}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def mod_note(self, h: str | None) -> str:
        if not h:
            return ""
        refs = self.mods.get().by_hash.get(h)
        if not refs:
            return ""
        names = sorted({r.mod for r in refs})
        return " [mod: " + ", ".join(names) + "]"


def _draw_line(s: Session, cap: Capture, d: dict) -> str:
    a = d["args"]
    parts = [f"#{d['index']:<5}", f"{d['call']:<22}", f"{d['origin']:<5}"]
    ib = d.get("ib")
    if ib:
        parts.append(f"ib={ib['hash'] or '-':<8}")
    if "index_count" in a:
        parts.append(f"ic={a['index_count']}")
    if "vertex_count" in a:
        parts.append(f"vc={a['vertex_count']}")
    if a.get("first_index"):
        parts.append(f"fi={a['first_index']}")
    if a.get("instance_count", 0) > 1:
        parts.append(f"inst={a['instance_count']}")
    if "thread_groups" in a:
        parts.append("tg=" + "x".join(str(x) for x in a["thread_groups"]))
    for stage in ("vs", "ps", "cs"):
        if d["shaders"].get(stage):
            parts.append(f"{stage}={d['shaders'][stage]}")
    out = d.get("out", {})
    if "o0" in out:
        r = cap.resource(out["o0"]["res"])
        size = f" {r['w']}x{r['h']}" if r and "w" in r and "h" in r else ""
        parts.append(f"o0={out['o0']['hash'] or 'backbuffer?'}{size}")
    line = " ".join(parts)
    if ib:
        line += s.mod_note(ib["hash"])
    return line


def _res_desc(r: dict | None) -> str:
    if not r:
        return "?"
    kind = r.get("kind")
    if kind == "buffer":
        return f"buffer {r['size']} bytes (bind 0x{r['bind']:x})"
    if kind == "tex2d":
        extra = f" x{r['array']}" if r.get("array", 1) > 1 else ""
        return f"tex2d {r['w']}x{r['h']}{extra} {r['fmt_name']} mips={r['mips']}"
    if kind == "tex1d":
        return f"tex1d {r['w']} {r['fmt_name']}"
    if kind == "tex3d":
        return f"tex3d {r['w']}x{r['h']}x{r['d']} {r['fmt_name']}"
    return str(kind)


def build_server(efmi_root: Path, out_dir: Path, probe_dll: Path,
                 loyaltools_root: Path | None = None, blender_path: str = "") -> Server:
    s = Session(efmi_root, out_dir, probe_dll, loyaltools_root, blender_path)
    srv = Server("efmi-mcp", __version__, INSTRUCTIONS)

    CAPTURE_PROP = {"capture": {"type": ["string", "integer"], "description": "'latest' (default) or the frame number of a capture still in memory"}}

    # ================================================================ status
    @srv.tool("efmi_status", "Is the game running with the probe attached? Shows the game process, XXMI, resolution, frame counter, "
                             "what the probe has seen so far, and the mods installed in the EFMI folder.")
    def efmi_status():
        lines = []
        probes = find_probes()
        if not probes:
            lines.append("probe: not loaded in any process (live tools unavailable)")
            lines.append(NO_PROBE.format(dll=s.probe_dll).split("\n\n")[1])
        else:
            st = s.call(lambda c: c.status())
            lines.append(f"game: {st['exe']} (pid {st['pid']}), probe {st['probe']}")
            lines.append(f"attached: {st['attached']} via {st['attached_via'] or '-'}; swap chain found: {st['swap_chain']}; "
                         f"frame {st['frame']}; resolution {st['resolution'][0]}x{st['resolution'][1]}")
            lines.append(f"xxmi: {'present, ' + st['xxmi']['path'] if st['xxmi']['present'] else 'not present in the process'}")
            xs = st["xxmi_settings"]
            lines.append(f"hash settings mirrored from d3dx.ini: track_region_hashes={int(xs['track_region_hashes'])} "
                         f"texture_hash={xs['texture_hash']} shader_hash={['3dmigoto', 'embedded', 'bytecode'][xs['shader_hash']]}")
            c, r = st["counters"], st["registry"]
            if "capture_target" in st:
                target = st["capture_target"]
                lines.append(f"capture target: device {target['device']}, context {target['context']}, "
                             f"swap chain {target['swap_chain']}; {c.get('tracked_draw_calls', 0)} draws on the selected context")
            lines.append(f"seen so far: {c['draw_calls']} draw calls, {c['presents']} frames; known: {r['resources']} resources, "
                         f"{r['shaders']} shaders ({r['bytecode_bytes'] // 1024} KB of bytecode kept), {r['layouts']} input layouts")
            cap = st["capture"]
            lines.append(f"continuous mode: {'on' if cap['live'] else 'off'}; cached region hashes: {cap['region_cache']}")
            if "reload_key" in st.get("features", ()):
                level = "elevated" if st.get("process", {}).get("elevated") else "not elevated"
                lines.append(f"reload: the probe presses F10 inside the game (game process is {level})")
            else:
                lines.append(f"reload: probe {st['probe']} cannot press F10 itself; efmi_reload_and_capture needs 0.1.2+ "
                             f"when the game runs as administrator. Restart the game to load {s.probe_dll}")
            if "mods_key" in st.get("features", ()):
                lines.append("mods switch: the probe presses F11 inside the game (efmi_toggle_mods)")
            else:
                lines.append(f"mods switch: probe {st['probe']} cannot press F11; efmi_toggle_mods needs 0.1.3+. "
                             f"Restart the game to load {s.probe_dll}")
            if s.mods_display:
                lines.append(f"mods display: {'ON' if s.mods_display['enabled'] else 'OFF'} according to EFMI after this "
                             f"server's last F11 (frame {s.mods_display['frame']}); a manual F11 since then is not seen")
            if c["command_lists"]:
                lines.append(f"note: {c['command_lists']} deferred command lists were executed; draws inside them are not visible")
            if not st["attached"]:
                lines.append("NOT ATTACHED: no D3D11 device is selected yet. Load the probe at game start, "
                             "wait for rendering, and check efmi_probe.log if this persists.")
        idx = s.mods.get()
        lines.append(f"EFMI folder: {s.efmi_root} ({len(idx.mods)} active mods, {len(idx.by_hash)} distinct hashes referenced)")
        if s.latest is not None:
            lines.append(f"captures in memory: {list(s.captures)} (latest {s.latest})")
        return "\n".join(lines)

    # ================================================================ capture
    @srv.tool("efmi_capture_frame",
              "Records the next frame the game draws and returns a summary. detail='full' records every binding of every draw "
              "(index/vertex buffers, shaders, constant buffers, textures, render targets); 'light' records only geometry and "
              "shaders; cb_data=true additionally keeps the contents of every constant buffer as it was at each draw. "
              "The capture stays in memory for the other tools.",
              {"detail": {"type": "string", "enum": ["full", "light"], "default": "full"},
               "cb_data": {"type": "boolean", "default": False}})
    def efmi_capture_frame(detail: str = "full", cb_data: bool = False):
        t0 = time.perf_counter()
        doc, blob = s.call(lambda c: c.capture(detail=detail, cb_data=cb_data, timeout_ms=8000))
        cap = Capture(doc, blob)
        s.add_capture(cap)
        ms = (time.perf_counter() - t0) * 1000
        draws = list(cap.draws())
        game = [d for d in draws if d["origin"] != "xxmi"]
        st = cap.stats
        lines = [f"capture {cap.frame} ({cap.detail}): {len(draws)} draws ({len(game)} from the game, {len(draws) - len(game)} from XXMI/mods), "
                 f"{len(cap.resources)} resources, {len(cap.shaders)} shaders; took {ms:.0f} ms "
                 f"(recording cost inside the game: {st['record_ms']:.2f} ms)"]
        if st.get("skipped_draws"):
            lines.append(f"WARNING: {st['skipped_draws']} draws beyond the per-frame limit were not recorded")
        if st.get("arena_full"):
            lines.append("WARNING: readback budget exhausted, some hashes are missing (state 'skipped')")
        if not draws:
            lines.append("WARNING: this frame contains no recorded draws; character/resource capture is not verified. "
                         "Check the selected device/context in efmi_status and efmi_probe.log.")
        if s.mods_display and not s.mods_display["enabled"]:
            lines.append(f"note: efmi_toggle_mods switched all mods OFF at frame {s.mods_display['frame']}; unless F11 was "
                         "pressed by hand since, this frame shows the game's own drawing. Switch them back on when done.")

        if cap.detail == "full":
            passes: "OrderedDict[tuple, list[int]]" = OrderedDict()
            for d in draws:
                o0 = d.get("out", {}).get("o0")
                key = (o0["res"] if o0 else -1,)
                passes.setdefault(key, []).append(d["index"])
            lines.append(f"render targets used ({len(passes)}):")
            for (res,), idxs in list(passes.items())[:14]:
                r = cap.resource(res)
                name = "no colour target (compute/depth only)" if res < 0 else f"o0={r.get('hash') or 'backbuffer/unknown'} {_res_desc(r)}"
                lines.append(f"  draws {idxs[0]}-{idxs[-1]} ({len(idxs)}): {name}")
            if len(passes) > 14:
                lines.append(f"  ... {len(passes) - 14} more")

        counts: Counter = Counter()
        info: dict = {}
        for d in game:
            ib = d.get("ib")
            if ib and ib["hash"]:
                counts[ib["hash"]] += 1
                info.setdefault(ib["hash"], d)
        lines.append(f"distinct index-buffer hashes drawn by the game: {len(counts)}")
        modded = [h for h in counts if s.mods.get().by_hash.get(h)]
        if modded:
            lines.append("of these, referenced by installed mods:")
            for h in modded[:20]:
                lines.append(f"  ib={h} ic={info[h]['args'].get('index_count')} first draw #{info[h]['index']}{s.mod_note(h)}")
        lines.append("next: efmi_list_draws (filterable), efmi_find_hash, efmi_get_draw")
        return "\n".join(lines)

    # ================================================================ reload
    @srv.tool("efmi_reload_and_capture",
              "After editing mod files, make XXMI reload them and capture a strictly later nonempty frame as 'latest'. "
              "The probe presses one unmodified F10 inside the game while the game window has the foreground "
              "(brought there if needed and handed back afterwards), so it works although the game runs elevated. "
              "Stops live mode and invalidates old captures. Requires this EFMI installation's default F10 "
              "reload_config and reload_fixes bindings. Refuses held modifiers/F10, failed focus/input, "
              "or an F10 pressed state that was never observed. "
              "Does not edit mod/Core/launcher files or inject a new DLL. Normal F10 side effects apply, "
              "including XXMI saving persistent settings; this tool never sends wipe-settings modifiers or another key. "
              "A reload also switches mods back on if efmi_toggle_mods had switched them off. "
              "reload_confirmed is true when the fresh frame uses resources/shaders XXMI created after the key press "
              "(mods on screen are loaded again after a reload); otherwise verify the edited draws/resources. "
              "Ordinary frame capture already obtains current data: do not use this just to refresh.",
              {"detail": {"type": "string", "enum": ["full", "light"], "default": "full"},
               "cb_data": {"type": "boolean", "default": True},
               "settle_ms": {"type": "integer", "minimum": 250, "maximum": 10000, "default": 2000,
                             "description": "Wait after F10 release before establishing the fresh-frame barrier."},
               "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 30000, "default": 15000,
                              "description": "Probe capture timeout; F10 is not retried on timeout."},
               "focus_game": {"type": "boolean", "default": True,
                              "description": "Try to bring the exact connected process's window to the foreground. "
                                             "False requires it to be already foreground."},
               "restore_focus": {"type": "boolean", "default": True,
                                 "description": "Give the foreground back to the window that had it once F10 is "
                                                "released. Ignored when the game was already in front."}},
              read_only=False, annotations={"destructiveHint": False, "idempotentHint": False})
    def efmi_reload_and_capture(detail="full", cb_data=True, settle_ms=2000, timeout_ms=15000, focus_game=True,
                                restore_focus=True):
        return reload_and_capture(s, detail=detail, cb_data=cb_data, settle_ms=settle_ms,
                                  timeout_ms=timeout_ms, focus_game=focus_game, restore_focus=restore_focus)

    # ================================================================ mods on/off
    @srv.tool("efmi_toggle_mods",
              "Switches ALL mods off or on with EFMI's own F11 key and captures the next frame as 'latest', so that what "
              "the game draws by itself can be compared with what the mods draw. The probe presses one unmodified F11 "
              "inside the game (the window is brought to the front if needed and the focus handed back). While mods "
              "are off, EFMI no longer applies the mods' index-buffer overrides: characters look as the game draws "
              "them, their original draws appear with origin 'game', and a modded character can be listed or "
              "extracted. Nothing is reloaded and no file is touched; mods that hook shaders on their own are not "
              "affected. state='toggle' presses once; 'on' and 'off' press once and, only if EFMI then reports the "
              "other state, once more; they press nothing when this server already read the requested state from "
              "EFMI's last notification and the current frame's draws agree. mods_enabled is read from the "
              "notification EFMI prints after each press "
              "('Mods Display: Enabled/Disabled') in the captured frame; null means it could not be read and the "
              "state is unknown. Earlier captures are kept for efmi_diff_captures. Mods stay off until the next "
              "F11, a reload (F10 switches them on again) or a game restart: switch them back on when the "
              "comparison is done. Requires EFMI's default [KeyToggleMods] binding and probe 0.1.3 or later; "
              "refuses held modifiers/F11 and failed focus or input, and never repeats a failed press.",
              {"state": {"type": "string", "enum": ["toggle", "on", "off"], "default": "toggle"},
               "cb_data": {"type": "boolean", "default": False,
                           "description": "Also keep every constant buffer as it was at each draw of the captured frame."},
               "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 30000, "default": 8000,
                              "description": "Probe capture timeout; F11 is not retried on timeout."},
               "focus_game": {"type": "boolean", "default": True,
                              "description": "Try to bring the exact connected process's window to the foreground. "
                                             "False requires it to be already foreground."},
               "restore_focus": {"type": "boolean", "default": True,
                                 "description": "Give the foreground back to the window that had it once F11 is "
                                                "released. Ignored when the game was already in front."}},
              read_only=False, annotations={"destructiveHint": False, "idempotentHint": False})
    def efmi_toggle_mods(state="toggle", cb_data=False, timeout_ms=8000, focus_game=True, restore_focus=True):
        return toggle_mods(s, state=state, cb_data=cb_data, timeout_ms=timeout_ms, focus_game=focus_game,
                           restore_focus=restore_focus)

    # ================================================================ list
    @srv.tool("efmi_list_draws",
              "Lists the draw calls of a capture, one per line, optionally filtered. Hash filters accept any hash the draw "
              "uses in that role; `hash` matches in any role (index/vertex buffer, shader, texture, constant buffer, render target).",
              {**CAPTURE_PROP,
               "ib_hash": {"type": "string"}, "shader": {"type": "string", "description": "vs/ps/cs/... hash"},
               "texture": {"type": "string"}, "hash": {"type": "string", "description": "match in any role"},
               "origin": {"type": "string", "enum": ["game", "xxmi"]},
               "call": {"type": "string", "description": "e.g. DrawIndexedInstanced, Dispatch"},
               "min_index_count": {"type": "integer"}, "max_index_count": {"type": "integer"},
               "offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 40}})
    def efmi_list_draws(capture="latest", ib_hash=None, shader=None, texture=None, hash=None, origin=None, call=None,
                        min_index_count=None, max_index_count=None, offset: int = 0, limit: int = 40):
        cap = s.capture(capture)
        ib_hash, shader, texture, any_hash = map(_norm_hash, (ib_hash, shader, texture, hash))
        rows = []
        for d in cap.draws():
            if origin and d["origin"] != origin:
                continue
            if call and d["call"].lower() != call.lower():
                continue
            ic = d["args"].get("index_count", 0)
            if min_index_count is not None and ic < min_index_count:
                continue
            if max_index_count is not None and ic > max_index_count:
                continue
            if ib_hash and (d.get("ib") or {}).get("hash") != ib_hash:
                continue
            if shader and shader not in d["shaders"].values():
                continue
            if texture and texture not in [v["hash"] for v in d.get("tex", {}).values()]:
                continue
            if any_hash and any_hash not in _all_hashes(d):
                continue
            rows.append(d)
        total = len(rows)
        limit = max(1, min(int(limit), 200))
        page = rows[offset:offset + limit]
        lines = [f"capture {cap.frame}: {total} matching draws of {len(cap)}; showing {offset}-{offset + len(page) - 1}"
                 if page else f"capture {cap.frame}: no draw matches"]
        lines += [_draw_line(s, cap, d) for d in page]
        if offset + len(page) < total:
            lines.append(f"... {total - offset - len(page)} more (use offset={offset + len(page)})")
        return "\n".join(lines)

    def _all_hashes(d: dict) -> set:
        out = set(h for h in d["shaders"].values() if h)
        ib = d.get("ib")
        if ib:
            out.update(h for h in (ib["hash"], ib["resource_hash"]) if h)
        for v in d.get("vb", {}).values():
            out.update(h for h in (v["hash"], v["resource_hash"]) if h)
        for group in ("cb", "tex", "out"):
            out.update(v["hash"] for v in d.get(group, {}).values() if v["hash"])
        return out

    # ================================================================ one draw
    @srv.tool("efmi_get_draw", "Everything bound for one draw call of a capture: arguments, index and vertex buffers with their "
                               "hashes, offsets and strides, the input layout, all shader stages, constant buffers (with the "
                               "bound window), textures per slot, render targets and viewport.",
              {**CAPTURE_PROP, "draw": {"type": "integer", "description": "draw index as listed"}}, ["draw"])
    def efmi_get_draw(draw: int, capture="latest"):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        out: dict = {"capture": cap.frame, "index": d["index"], "call": d["call"], "origin": d["origin"], "args": d["args"]}
        if "xxmi_depth" in d:
            out["called_through_xxmi_frames"] = d["xxmi_depth"]
        if "indirect" in d:
            out["indirect_args"] = d["indirect"]
        if "topology" in d:
            out["topology"] = d["topology"]
        if "ib" in d:
            ib = d["ib"]
            out["ib"] = {"hash": ib["hash"], "resource_hash": ib["resource_hash"], "format": decode.format_name(ib["format"]),
                         "bind_offset": ib["offset"], "region": ib["region"] and {k: ib["region"][k] for k in ("offset", "size", "state")},
                         "resource": _res_desc(cap.resource(ib["res"])), "mods": s.mod_note(ib["hash"]).strip() or None}
        if "vb" in d:
            out["vb"] = {f"vb{slot}": {"hash": v["hash"], "resource_hash": v["resource_hash"], "stride": v["stride"],
                                      "bind_offset": v["offset"], "resource": _res_desc(cap.resource(v["res"]))}
                         for slot, v in d["vb"].items()}
        lay = cap.layout(d.get("layout", -1))
        if lay is not None:
            out["input_layout"] = [f"{e['semantic']}{e['index'] or ''} {decode.format_name(e['format'])} slot={e['slot']} "
                                   f"offset={e['offset']}{' per-instance' if e['per_instance'] else ''}" for e in lay]
        out["shaders"] = d["shaders"]
        if "cb" in d:
            out["constant_buffers"] = {k: {"hash": v["hash"], "first_constant": v["first_constant"], "num_constants": v["num_constants"],
                                            "contents_captured": v["snap"] >= 0, "resource": _res_desc(cap.resource(v["res"]))}
                                       for k, v in d["cb"].items()}
        if "tex" in d:
            out["textures"] = {}
            for k, v in d["tex"].items():
                r = cap.resource(v["res"])
                out["textures"][k] = f"{v['hash'] or 'unknown'} {_res_desc(r)}" + (" (xxmi)" if r and r.get("origin") == "xxmi" else "") + s.mod_note(v["hash"])
        if "out" in d:
            out["outputs"] = {k: f"{v['hash'] or 'not created by the game (back buffer?)'} {_res_desc(cap.resource(v['res']))}"
                              for k, v in d["out"].items()}
        if "viewport" in d:
            out["viewport"] = d["viewport"]
        return out

    # ================================================================ find
    @srv.tool("efmi_find_hash", "Where does a hash occur? Searches a capture for the hash in every role and lists the mods in the "
                                "EFMI folder whose ini files refer to it.",
              {**CAPTURE_PROP, "hash": {"type": "string"}}, ["hash"])
    def efmi_find_hash(hash: str, capture="latest"):
        h = _norm_hash(hash)
        lines = []
        refs = s.mods.get().by_hash.get(h, [])
        if refs:
            lines.append(f"referenced by {len(refs)} mod section(s):")
            for r in refs[:20]:
                match = (" " + " ".join(f"{k}={v}" for k, v in r.match.items())) if r.match else ""
                lines.append(f"  {r.ini} [{r.section}]{match}")
        else:
            lines.append("not referenced by any active mod ini")
        if s.latest is None and capture in (None, "", "latest"):
            lines.append("no capture in memory to search (efmi_capture_frame)")
            return "\n".join(lines)
        cap = s.capture(capture)
        roles: dict[str, list[int]] = {}
        for d in cap.draws():
            ib = d.get("ib")
            if ib:
                if ib["hash"] == h: roles.setdefault("index buffer (region hash)", []).append(d["index"])
                if ib["resource_hash"] == h: roles.setdefault("index buffer (whole resource)", []).append(d["index"])
            for slot, v in d.get("vb", {}).items():
                if v["hash"] == h: roles.setdefault(f"vb{slot} (region hash)", []).append(d["index"])
                if v["resource_hash"] == h: roles.setdefault(f"vb{slot} (whole resource)", []).append(d["index"])
            for stage, sh in d["shaders"].items():
                if sh == h: roles.setdefault(f"{stage} shader", []).append(d["index"])
            for group, label in (("cb", "constant buffer"), ("tex", "texture"), ("out", "output")):
                for k, v in d.get(group, {}).items():
                    if v["hash"] == h: roles.setdefault(f"{label} {k}", []).append(d["index"])
        if not roles:
            lines.append(f"capture {cap.frame}: not used by any draw of this frame")
        else:
            lines.append(f"capture {cap.frame}:")
            for role, idxs in roles.items():
                shown = ", ".join(str(i) for i in idxs[:24]) + (f", ... ({len(idxs)} draws)" if len(idxs) > 24 else "")
                lines.append(f"  {role}: draws {shown}")
        for r in cap.resources:
            if r.get("hash") == h:
                lines.append(f"resource: {_res_desc(r)}, origin {r.get('origin')}, data hash {r.get('data_hash')}")
                break
        for sh in cap.shaders:
            if sh.get("hash") == h:
                lines.append(f"shader: {sh['type']} {sh['size']} bytes, origin {sh.get('origin')} (efmi_get_shader for the listing)")
                break
        return "\n".join(lines)

    # ================================================================ buffers
    def _read(cap: Capture, res: int, off: int, size: int) -> tuple[dict, bytes]:
        return s.call(lambda c: c.read_buffer(cap.frame, res, off, size))

    def _index_data(cap: Capture, d: dict) -> tuple[list[int], dict]:
        ib = d.get("ib")
        if not ib:
            raise ToolError("this draw has no index buffer bound")
        a = d["args"]
        off, size = index_region(ib["format"], ib["offset"], a.get("first_index", 0), a.get("index_count", 0))
        if not size:
            raise ToolError("this draw is not indexed")
        meta, data = _read(cap, ib["res"], off, size)
        meta["hash_now"] = hex32(crc32c(data))
        meta["hash_at_capture"] = ib["hash"]
        return decode.decode_indices(data, ib["format"]), meta

    def _vertex_range(cap: Capture, d: dict, slot: int, per_instance: bool) -> tuple[int, int]:
        """(first element, count) of the vertices (or instances) this draw reads from a slot."""
        a = d["args"]
        if per_instance:
            return a.get("first_instance", 0), max(a.get("instance_count", 1), 1)
        if a.get("index_count"):
            idx, _ = _index_data(cap, d)
            base = a.get("first_vertex", 0)
            return min(idx) + base, max(idx) - min(idx) + 1
        return a.get("first_vertex", 0), a.get("vertex_count", 0)

    @srv.tool("efmi_read_buffer",
              "Reads the data a draw uses from one of its buffers and decodes it. slot: 'ib' (indices), 'vb0'.. (vertices, decoded "
              "with the input layout), or a constant buffer such as 'vs-cb0' / 'ps-cb1' (rows of four floats). `first`/`count` page "
              "through the decoded elements. Index and vertex data are read at the time of the call and checked against the hash "
              "from the capture; constant buffers come from the capture itself when it was taken with cb_data=true.",
              {**CAPTURE_PROP, "draw": {"type": "integer"}, "slot": {"type": "string"},
               "first": {"type": "integer", "default": 0}, "count": {"type": "integer", "default": 16},
               "view": {"type": "string", "enum": ["float", "uint", "hex"], "default": "float",
                        "description": "how to show constant buffer registers"},
               "save": {"type": "boolean", "default": False, "description": "also write the raw bytes to a .buf file"}},
              ["draw", "slot"])
    def efmi_read_buffer(draw: int, slot: str, capture="latest", first: int = 0, count: int = 16, view: str = "float", save: bool = False):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        slot = slot.strip().lower()
        count = max(1, min(int(count), 256))
        out: dict = {"capture": cap.frame, "draw": draw, "slot": slot}
        raw = b""

        if slot == "ib":
            idx, meta = _index_data(cap, d)
            out.update(format=decode.format_name(d["ib"]["format"]), index_count=len(idx), min=min(idx), max=max(idx),
                       hash=meta["hash_at_capture"], unchanged_since_capture=meta["hash_now"] == meta["hash_at_capture"],
                       first=first, indices=idx[first:first + count * 3])
            raw = struct.pack(f"<{len(idx)}{'I' if d['ib']['format'] == 42 else 'H'}", *idx)
        elif slot.startswith("vb"):
            n = int(slot[2:])
            vb = d.get("vb", {}).get(n)
            if not vb:
                raise ToolError(f"nothing bound to {slot}; bound: {['vb%d' % k for k in d.get('vb', {})]}")
            lay = cap.layout(d.get("layout", -1)) or []
            elems = [e for e in lay if e["slot"] == n]
            per_instance = bool(elems) and all(e["per_instance"] for e in elems)
            start, total = _vertex_range(cap, d, n, per_instance)
            meta, raw = _read(cap, vb["res"], vb["offset"] + vb["stride"] * start, vb["stride"] * total)
            out.update(stride=vb["stride"], first_element=start, element_count=total,
                       kind="per-instance data" if per_instance else "vertices",
                       region_hash_at_capture=vb["hash"], read_frame=meta.get("read_frame"),
                       unchanged_since_capture=meta.get("unchanged_since_capture"))
            if elems:
                out["layout"] = [f"{e['semantic']}{e['index'] or ''}:{decode.format_name(e['format'])}@{e['offset']}" for e in elems]
                out["first"] = first
                out["elements"] = decode.decode_vertices(raw, vb["stride"], elems, first, count)
            else:
                out["note"] = "no input layout element uses this slot; showing raw bytes"
                out["hex"] = [raw[i:i + vb["stride"]].hex() for i in range(first * vb["stride"], min(len(raw), (first + count) * vb["stride"]), vb["stride"])]
        elif "-cb" in slot:
            cb = d.get("cb", {}).get(slot)
            if not cb:
                raise ToolError(f"nothing bound to {slot}; bound: {list(d.get('cb', {}))}")
            raw = cap.snap_bytes(cb["snap"]) if cb["snap"] >= 0 else None
            if raw is not None:
                out["source"] = "contents as bound for this draw (from the capture)"
            else:
                meta, raw = _read(cap, cb["res"], cb["first_constant"] * 16, cb["num_constants"] * 16)
                out["source"] = (f"read at frame {meta.get('read_frame')}, not at the draw; "
                                 "capture with cb_data=true for the exact contents")
            rows = len(raw) // 16
            out.update(hash=cb["hash"], first_constant=cb["first_constant"], registers=rows, first=first)
            regs = {}
            for i in range(first, min(rows, first + count)):
                chunk = raw[i * 16:i * 16 + 16]
                if view == "hex":
                    regs[f"c{i}"] = chunk.hex()
                elif view == "uint":
                    regs[f"c{i}"] = list(struct.unpack("<4I", chunk))
                else:
                    regs[f"c{i}"] = [round(v, 6) for v in struct.unpack("<4f", chunk)]
            out["values"] = regs
        else:
            raise ToolError("slot must be 'ib', 'vbN' or a constant buffer like 'vs-cb0'")

        if save and raw:
            path = s.frame_dir(cap) / f"draw{draw}-{slot}.buf"
            path.write_bytes(raw)
            out["saved"] = str(path)
        return out

    # ================================================================ mesh export
    @srv.tool("efmi_export_mesh",
              "Writes the mesh of one indexed draw to disk for inspection: a Wavefront .obj (positions, normals and UVs when the "
              "input layout has them), the raw index and vertex bytes, and a mesh.json describing layout, hashes and draw arguments. "
              "Returns the folder and basic statistics.",
              {**CAPTURE_PROP, "draw": {"type": "integer"}}, ["draw"], read_only=False)
    def efmi_export_mesh(draw: int, capture="latest"):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        idx, meta = _index_data(cap, d)
        lay = cap.layout(d.get("layout", -1)) or []
        folder = s.frame_dir(cap) / f"draw{draw}-mesh"
        folder.mkdir(parents=True, exist_ok=True)
        ib = d["ib"]
        (folder / "ib.buf").write_bytes(struct.pack(f"<{len(idx)}{'I' if ib['format'] == 42 else 'H'}", *idx))

        lo, hi = min(idx), max(idx)
        base = d["args"].get("first_vertex", 0)
        streams: dict[str, list] = {}
        slots_info = {}
        for n, vb in d.get("vb", {}).items():
            elems = [e for e in lay if e["slot"] == n and not e["per_instance"]]
            if not elems or not vb["stride"]:
                continue
            _m, raw = _read(cap, vb["res"], vb["offset"] + vb["stride"] * (lo + base), vb["stride"] * (hi - lo + 1))
            (folder / f"vb{n}.buf").write_bytes(raw)
            slots_info[f"vb{n}"] = {"stride": vb["stride"], "hash": vb["hash"], "resource_hash": vb["resource_hash"],
                                    "elements": [{"semantic": e["semantic"], "index": e["index"],
                                                  "format": decode.format_name(e["format"]), "offset": e["offset"]} for e in elems]}
            for v in decode.decode_vertices(raw, vb["stride"], elems):
                for k, val in v.items():
                    streams.setdefault(k, []).append(val)

        pos = streams.get("POSITION")
        nrm = streams.get("NORMAL")
        uv = streams.get("TEXCOORD")
        stats = {"folder": str(folder), "indices": len(idx), "vertices": hi - lo + 1, "topology": d.get("topology"),
                 "ib_hash": ib["hash"], "index_data_unchanged_since_capture": meta["hash_now"] == meta["hash_at_capture"]}
        if pos and all(p is not None for p in pos):
            with open(folder / "mesh.obj", "w", encoding="ascii") as f:
                f.write(f"# capture {cap.frame} draw {draw} ib={ib['hash']}\n")
                for p in pos:
                    f.write("v %g %g %g\n" % tuple(p[:3]))
                has_uv = bool(uv) and all(t is not None for t in uv)
                has_n = bool(nrm) and all(t is not None and len(t) >= 3 for t in nrm)
                if has_uv:
                    for t in uv:
                        f.write("vt %g %g\n" % (t[0], 1.0 - t[1]))
                if has_n:
                    for t in nrm:
                        f.write("vn %g %g %g\n" % tuple(t[:3]))
                if d.get("topology") == "trianglelist":
                    for i in range(0, len(idx) - 2, 3):
                        tri = [idx[i] - lo + 1, idx[i + 1] - lo + 1, idx[i + 2] - lo + 1]
                        if has_uv and has_n:
                            f.write("f " + " ".join(f"{v}/{v}/{v}" for v in tri) + "\n")
                        elif has_uv:
                            f.write("f " + " ".join(f"{v}/{v}" for v in tri) + "\n")
                        elif has_n:
                            f.write("f " + " ".join(f"{v}//{v}" for v in tri) + "\n")
                        else:
                            f.write("f %d %d %d\n" % tuple(tri))
            xs, ys, zs = zip(*[p[:3] for p in pos])
            stats["obj"] = str(folder / "mesh.obj")
            stats["bounds"] = {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]}
            stats["triangles"] = len(idx) // 3 if d.get("topology") == "trianglelist" else 0
        else:
            stats["obj"] = None
            stats["note"] = "no decodable POSITION element in the input layout; raw buffers were written"
        info = {"capture": cap.frame, "draw": draw, "call": d["call"], "args": d["args"], "shaders": d["shaders"],
                "ib": {"hash": ib["hash"], "resource_hash": ib["resource_hash"], "format": decode.format_name(ib["format"]),
                       "first_referenced_vertex": lo, "base_vertex": base},
                "vertex_buffers": slots_info,
                "textures": {k: v["hash"] for k, v in d.get("tex", {}).items()}}
        (folder / "mesh.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        return stats

    # ================================================================ textures
    @srv.tool("efmi_read_texture",
              "Reads a texture bound for a draw (slot such as 'ps-t0', or an output: 'o0', 'oD') as it is at the time of the call. "
              "Saves it as .dds (and .png when the format can be converted) and returns a downscaled preview image.",
              {**CAPTURE_PROP, "draw": {"type": "integer"}, "slot": {"type": "string"},
               "mip": {"type": "integer", "default": 0}, "slice": {"type": "integer", "default": 0,
                                                                     "description": "array slice or cube face"},
               "preview": {"type": "boolean", "default": True}},
              ["draw", "slot"], read_only=False)
    def efmi_read_texture(draw: int, slot: str, capture="latest", mip: int = 0, slice: int = 0, preview: bool = True):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        slot = slot.strip()
        entry = d.get("tex", {}).get(slot.lower()) or d.get("out", {}).get(slot)
        if not entry:
            raise ToolError(f"nothing bound to {slot}; textures: {list(d.get('tex', {}))}; outputs: {list(d.get('out', {}))}")
        meta, data = s.call(lambda c: c.read_texture(cap.frame, entry["res"], mip, slice))
        w, h, fmt = meta["width"], meta["height"], meta["format"]
        stem = f"{entry['hash'] or 'res%d' % entry['res']}-mip{mip}" + (f"-slice{slice}" if slice else "")
        folder = s.frame_dir(cap)
        dds_path = folder / f"{stem}.dds"
        dds_path.write_bytes(decode.make_dds(w, h, fmt, data))
        text = [f"{slot} of draw {draw}: hash {entry['hash'] or 'unknown'}, {meta['tex_width']}x{meta['tex_height']} {meta['format_name']}, "
                f"{meta['mips']} mips, array {meta['array']}{', cube' if meta.get('cube') else ''}",
                f"read mip {mip} ({w}x{h}) at frame {meta['read_frame']} (current contents, not necessarily those at the draw)",
                f"saved: {dds_path}"]
        result: list = []
        png, small = decode.texture_to_pngs(w, h, fmt, data, preview_max_size=512 if preview else 0)
        if png:
            png_path = folder / f"{stem}.png"
            png_path.write_bytes(png)
            text.append(f"saved: {png_path}")
            if small:
                result.append(Image(small))
        else:
            text.append("no PNG: this format is not converted here (the .dds holds the exact data)")
        return ["\n".join(text)] + result

    # ================================================================ shaders
    @srv.tool("efmi_get_shader", "Disassembly of a shader the game (or a mod) created, by hash. The full listing is saved to a file; "
                                 "`first_line`/`max_lines` page through it.",
              {"hash": {"type": "string"}, "first_line": {"type": "integer", "default": 1},
               "max_lines": {"type": "integer", "default": 160}}, ["hash"], read_only=False)
    def efmi_get_shader(hash: str, first_line: int = 1, max_lines: int = 160):
        h = _norm_hash(hash)
        folder = s.out_dir / "shaders"
        folder.mkdir(parents=True, exist_ok=True)
        asm_path = None
        for cand in folder.glob(f"{h}-*.asm"):
            asm_path = cand
        if asm_path is None:
            info, code = s.call(lambda c: c.shader(h))
            (folder / f"{h}-{info['type']}.bin").write_bytes(code)
            try:
                text = disassemble(code)
            except OSError as e:
                raise ToolError(f"bytecode saved to {folder / (h + '-' + info['type'] + '.bin')} but it could not be disassembled: {e}")
            asm_path = folder / f"{h}-{info['type']}.asm"
            asm_path.write_text(text, encoding="utf-8")
        lines = asm_path.read_text(encoding="utf-8").splitlines()
        first_line = max(1, int(first_line))
        max_lines = max(1, min(int(max_lines), 600))
        page = lines[first_line - 1:first_line - 1 + max_lines]
        head = f"{asm_path} ({len(lines)} lines); showing {first_line}-{first_line + len(page) - 1}"
        return head + "\n" + "\n".join(page)

    # ================================================================ continuous mode
    def _live_item(it: list) -> dict:
        t, origin, ib, state, ib_res, ic, fi, vc, vs, ps, draws, inst = it
        return {"call": DRAW_NAMES.get(t, str(t)), "origin": origin, "ib": ib, "ib_state": state, "ib_resource": ib_res,
                "index_count": ic, "first_index": fi, "vertex_count": vc, "vs": vs, "ps": ps, "draws": draws, "instances": inst}

    @srv.tool("efmi_live_start",
              "Switches the continuous mode on: from now on every frame is recorded at minimal detail inside the game (which mesh, "
              "which shaders, how many instances) and the last few seconds are kept in memory there. Costs well under a millisecond "
              "per frame; nothing is written to disk. Use efmi_live_now / efmi_watch_hash to look at it, efmi_live_stop to end it.",
              {"every": {"type": "integer", "default": 1, "description": "record every n-th frame"},
               "keep_frames": {"type": "integer", "default": 600}}, read_only=False)
    def efmi_live_start(every: int = 1, keep_frames: int = 600):
        r = s.call(lambda c: c.live(enable=True, every=max(1, int(every)), keep_frames=max(10, int(keep_frames))))
        return f"continuous mode on (frame {r['frame']}); every {max(1, int(every))} frame(s), keeping {keep_frames}"

    @srv.tool("efmi_live_stop", "Switches the continuous mode off and discards the frames kept in the game process.", read_only=False)
    def efmi_live_stop():
        s.call(lambda c: c.live(enable=False))
        return "continuous mode off"

    def _live_frames(since: int = 0, max_frames: int = 0) -> tuple[int, list[dict]]:
        now, frames = s.call(lambda c: c.live_get(since=since, max_frames=max_frames))
        return now, frames

    @srv.tool("efmi_live_now",
              "What is being drawn right now: the most recent frame of the continuous mode, one line per distinct mesh/shader "
              "combination with how many draws and instances it had. Requires efmi_live_start.",
              {"origin": {"type": "string", "enum": ["game", "xxmi", "all"], "default": "game"},
               "min_index_count": {"type": "integer", "default": 0},
               "offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 40}})
    def efmi_live_now(origin: str = "game", min_index_count: int = 0, offset: int = 0, limit: int = 40):
        now, frames = _live_frames(max_frames=1)
        if not frames:
            raise ToolError("no frames yet: call efmi_live_start first (and the game has to be rendering)")
        f = frames[-1]
        items = [_live_item(it) for it in f["items"]]
        if origin != "all":
            items = [i for i in items if (i["origin"] == "xxmi") == (origin == "xxmi")]
        items = [i for i in items if i["index_count"] >= min_index_count]
        limit = max(1, min(int(limit), 200))
        page = items[offset:offset + limit]
        lines = [f"frame {f['frame']} (current {now}): {f['draws']} draws, {f['game_draws']} from the game; "
                 f"{len(items)} distinct items after filtering; probe cost {f['cost_ms']:.2f} ms"]
        for i in page:
            ib = i["ib"] or (i["ib_state"] if i["ib_state"] else "-")
            line = (f"{i['call']:<22} {i['origin']:<5} ib={ib:<8} ic={i['index_count']:<7} fi={i['first_index']:<7} "
                    f"vs={i['vs'] or '-'} ps={i['ps'] or '-'} draws={i['draws']} inst={i['instances']}")
            lines.append(line + s.mod_note(i["ib"]))
        if offset + len(page) < len(items):
            lines.append(f"... {len(items) - offset - len(page)} more (offset={offset + len(page)})")
        if f["volatile"]:
            lines.append(f"({f['volatile']} index regions shown as 'volatile': their bytes are rewritten every frame, so they have no stable hash)")
        return "\n".join(lines)

    @srv.tool("efmi_watch_hash",
              "Watches the continuous mode for a while and reports, frame by frame, whether a hash was drawn: in how many frames, "
              "with how many draws and instances, and with which shaders. `hash` may be an index-buffer hash or a vs/ps hash.",
              {"hash": {"type": "string"}, "seconds": {"type": "number", "default": 2.0}}, ["hash"])
    def efmi_watch_hash(hash: str, seconds: float = 2.0):
        h = _norm_hash(hash)
        seconds = max(0.2, min(float(seconds), 30.0))
        start, frames = _live_frames(max_frames=1)
        if not frames:
            s.call(lambda c: c.live(enable=True))
            time.sleep(0.2)
            start, _ = _live_frames(max_frames=1)
            started_here = True
        else:
            started_here = False
        time.sleep(seconds)
        _now, frames = _live_frames(since=start)
        if started_here:
            s.call(lambda c: c.live(enable=False))
        if not frames:
            raise ToolError("the game presented no frames in that time")
        seen = 0
        draws: list[int] = []
        insts: list[int] = []
        combos: Counter = Counter()
        gaps: list[int] = []
        for f in frames:
            hits = [_live_item(it) for it in f["items"] if h in (it[2], it[4], it[8], it[9])]
            if hits:
                seen += 1
                draws.append(sum(i["draws"] for i in hits))
                insts.append(sum(i["instances"] for i in hits))
                for i in hits:
                    combos[(i["origin"], i["ib"], i["index_count"], i["vs"], i["ps"])] += 1
            else:
                gaps.append(f["frame"])
        lines = [f"watched {len(frames)} frames ({frames[0]['frame']}-{frames[-1]['frame']}, {seconds:.1f} s): "
                 f"{h} drawn in {seen} of them"]
        if seen:
            lines.append(f"draws per frame: {min(draws)}-{max(draws)}; instances per frame: {min(insts)}-{max(insts)}")
            lines.append("as:")
            for (origin, ib, ic, vs, ps), n in combos.most_common(12):
                lines.append(f"  {origin:<5} ib={ib or '-'} ic={ic} vs={vs or '-'} ps={ps or '-'}  ({n} frames)")
            if gaps and len(gaps) <= 12:
                lines.append(f"not drawn in frames: {gaps}")
            elif gaps:
                lines.append(f"not drawn in {len(gaps)} frames (first {gaps[0]}, last {gaps[-1]})")
        lines.append(s.mod_note(h).strip() or "not referenced by any active mod ini")
        return "\n".join(lines)

    # ================================================================ diff
    @srv.tool("efmi_diff_captures", "What changed between two captures: meshes (index-buffer hash with shaders) that appear in one "
                                    "but not in the other. Useful around toggling a mod or an in-game action. By default only "
                                    "the game's own draws are compared; origin='xxmi' compares what XXMI and mods issued "
                                    "(XXMI's on-screen text changes from frame to frame and shows up there).",
              {"a": {"type": ["string", "integer"]}, "b": {"type": ["string", "integer"], "default": "latest"},
               "origin": {"type": "string", "enum": ["game", "xxmi", "all"], "default": "game"}}, ["a"])
    def efmi_diff_captures(a, b="latest", origin: str = "game"):
        ca, cb = s.capture(a), s.capture(b)

        def items(cap: Capture) -> Counter:
            c: Counter = Counter()
            for d in cap.draws():
                if origin != "all" and (d["origin"] == "xxmi") != (origin == "xxmi"):
                    continue
                ib = d.get("ib") or {}
                c[(d["origin"], ib.get("hash"), d["args"].get("index_count", 0), d["shaders"].get("vs"), d["shaders"].get("ps"))] += 1
            return c

        ia, ib_ = items(ca), items(cb)
        only_a = [k for k in ia if k not in ib_]
        only_b = [k for k in ib_ if k not in ia]
        lines = [f"comparing draws of origin '{origin}': capture {ca.frame}: {sum(ia.values())} draws, {len(ia)} distinct; "
                 f"capture {cb.frame}: {sum(ib_.values())} draws, {len(ib_)} distinct"]
        for label, keys, src in ((f"only in {ca.frame}", only_a, ia), (f"only in {cb.frame}", only_b, ib_)):
            lines.append(f"{label}: {len(keys)}")
            for k in keys[:30]:
                origin, h, ic, vs, ps = k
                lines.append(f"  {origin:<5} ib={h or '-'} ic={ic} vs={vs or '-'} ps={ps or '-'} x{src[k]}" + s.mod_note(h))
            if len(keys) > 30:
                lines.append(f"  ... {len(keys) - 30} more")
        return "\n".join(lines)

    # ================================================================ offline
    @srv.tool("efmi_list_mods", "The mods XXMI loads from the EFMI folder (same include/exclude rules as d3dx.ini), with their ini "
                                "files, namespaces and how many hashes each refers to. Reads files only; works without the game.")
    def efmi_list_mods():
        idx = s.mods.get()
        lines = [f"{s.efmi_root}: include {idx.include_roots}, exclude {idx.excludes}; {len(idx.mods)} mods, "
                 f"{len(idx.refs)} hash references ({len(idx.by_hash)} distinct)"]
        for name, info in sorted(idx.mods.items()):
            ns = ", ".join(info["namespaces"]) or "-"
            lines.append(f"  {name}: {len(info['inis'])} ini, {info['hashes']} hashes, namespace {ns}")
        return "\n".join(lines)

    @srv.tool("efmi_frame_dump_summary",
              "Summarises an existing XXMI frame analysis folder (FrameAnalysis-...): options, number of draws and files, and the "
              "first draws with their hashes. `path` may also be a folder containing such dumps (the newest is used). Offline.",
              {"path": {"type": "string"}, "limit": {"type": "integer", "default": 25}}, ["path"])
    def efmi_frame_dump_summary(path: str, limit: int = 25):
        p = Path(path)
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        if not (p / "log.txt").exists():
            dumps = find_frame_dumps(p)
            if not dumps:
                raise ToolError(f"no FrameAnalysis-* folder with a log.txt in {p}")
            p = dumps[-1]
        dump = parse_frame_dump(p)
        nums = [n for n in dump.draw_numbers() if dump.draws[n].call]
        lines = [f"{p}: analyse_options {dump.analyse_options}, {len(nums)} draws, {dump.file_count} dumped files"]
        for n in nums[:max(1, min(int(limit), 200))]:
            d = dump.draws[n]
            ib = d.slots.get("ib")
            ibs = f" ib={ib.hash}" + (f"({ib.full_hash})" if ib and ib.full_hash else "") if ib else ""
            sh = " ".join(f"{k}={v}" for k, v in d.shaders.items())
            ic = d.args.get("IndexCount", d.args.get("IndexCountPerInstance"))
            lines.append(f"  {n:06d} {d.call}{ibs}{' ic=%d' % ic if ic else ''} {sh}" + s.mod_note(ib.hash if ib else None))
        if len(nums) > limit:
            lines.append(f"  ... {len(nums) - limit} more")
        return "\n".join(lines)

    @srv.tool("efmi_verify_against_dump",
              "Self-check: compares a capture with an XXMI frame analysis dump of the same scene and reports whether the probe's "
              "hashes agree with the ones XXMI wrote. Worth running once after XXMI has been updated.",
              {**CAPTURE_PROP, "path": {"type": "string", "description": "a FrameAnalysis-... folder, or a folder containing them"}},
              ["path"])
    def efmi_verify_against_dump(path: str, capture="latest"):
        cap = s.capture(capture)
        p = Path(path)
        if not (p / "log.txt").exists():
            dumps = find_frame_dumps(p)
            if not dumps:
                raise ToolError(f"no FrameAnalysis-* folder with a log.txt in {p}")
            p = dumps[-1]
        res = compare_sets(cap, parse_frame_dump(p))
        verdict = "AGREES" if res.ok else ("DISAGREES" if res.mismatches else "NOTHING IN COMMON TO COMPARE")
        return "\n".join([f"capture {cap.frame} vs {p.name}: {verdict} ({res.checks} comparisons, {res.mismatches} mismatches)"] + res.messages)

    model_tools.register(srv, s)
    return srv
