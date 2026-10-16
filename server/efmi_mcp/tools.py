"""The MCP tools: live access to what the game is drawing."""
from __future__ import annotations

import json
import struct
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from . import __version__, contents, decode, draw_outputs, model_tools, pipeline, slot_data, texture_slots
from .capture_model import Capture, STAGES
from .draw_outputs import capture_draw_outputs
from .fa_dump import DRAW_NAMES, MARKS, find_frame_dumps, parse_frame_dump
from .hashes import crc32c, hex32, index_region
from . import blender_runner, loyal_bridge
from .mcp_stdio import Image, Server, ToolError
from .model_jobs import JobManager
from .mods import ModCache
from .mods_toggle import toggle_mods
from .parity import compare_sets
from .probe_client import ProbeClient, ProbeError, ProbeNotFound, find_probes
from .reload_workflow import reload_and_capture
from .shader import ShaderCache

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

For texture slots use efmi_get_texture_slots after a full capture: select draw numbers or an IB hash to get \
per-shader sampling evidence and texture bindings in one batch. It filters stale bindings by default and \
caches shader analysis in memory; do not write helper scripts or export shaders just to look up slots. \
efmi_get_shader pages through the cached assembly without writing files; save=true explicitly exports it.

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
- Buffer and texture reads return the contents at the time of the read unless the capture kept them: \
efmi_capture_frame contents='inputs' has the probe keep what every call read (index, vertex and constant buffers, \
buffers and textures bound as shader resources) exactly as it was when the call was issued, contents='outputs' also \
what calls left in their render targets and unordered access views. Nothing is copied while the frame is drawn: \
the probe copies a resource only when the game is about to overwrite it, so this is cheap for static data. \
efmi_read_buffer and efmi_read_texture then say `exact` and where the bytes come from. The contents last until the \
next capture or three minutes without a request. efmi_list_writes shows the maps, updates, copies and clears between \
the calls; efmi_dump_frame writes the frame out as a folder laid out like an XXMI frame analysis dump. Needs probe \
0.1.6. Without it, cb_data=true still keeps the bound window of every constant buffer.
- What is not visible from here: XXMI's own command lists, variables and the calls it skipped (that is in XXMI's \
log.txt only). XXMI's IniParams, as shaders see them, are a 1D texture in slot t120 and can be read per call.
- To see what a draw paints, use efmi_capture_draw_outputs: it records a new frame and copies the render target \
just before and just after the draws you select (by index buffer hash, shader hash or draw numbers of a capture), \
then reports the pixels each one changed, with pictures. efmi_read_texture on an output only shows the target as \
it is when you ask. Needs probe 0.1.5.
- efmi_get_draw of a full capture includes blend, depth-stencil and rasterizer state, scissor rectangles, samplers, \
the part of each resource a bound view shows, and the resolved arguments of indirect draws (probe 0.1.5).
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
  XXMI Launcher -> Settings -> Advanced -> tick "Inject Libraries" and add this path on its own line:
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
        self._shader_client: ProbeClient | None = None
        self._shader_cache = ShaderCache()
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

    def shader_listing(self, h: str):
        h = texture_slots._hash(h, 16, "shader hash")
        if not h:
            raise ToolError("shader hash must be 16 hex digits")
        client = self.probe()
        if client is not self._shader_client:
            self._shader_cache.clear()
            self._shader_client = client
        try:
            return self._shader_cache.get(h, lambda: self.call(lambda c: c.shader(h)))
        except OSError as exc:
            raise ToolError(f"shader {h} could not be disassembled: {exc}") from exc

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
            if "pipeline_state" not in st.get("features", ()):
                lines.append(f"pipeline state and render target copies around draws: need probe 0.1.5 (this is "
                             f"{st['probe']}). Restart the game to load {s.probe_dll}")
            if contents.FEATURE not in st.get("features", ()):
                lines.append(f"contents as each call saw them (efmi_capture_frame contents=..., efmi_dump_frame): need probe "
                             f"0.1.6 (this is {st['probe']}). Restart the game to load {s.probe_dll}")
            elif cap.get("contents_kept"):
                lines.append("contents: the probe is keeping what the calls of the latest capture saw (released by the next "
                             "capture or after three minutes without a request)"
                             + (f": {cap['contents_copies']} copies, {cap['contents_bytes'] / 1048576:.1f} MiB so far"
                                + (f", {cap['contents_lost']} versions lost" if cap.get("contents_lost") else "")
                                if "contents_bytes" in cap else ""))
            if "region_hash_per_object" not in st.get("features", ()):
                lines.append(f"hashes: probe {st['probe']} can record a destroyed buffer's region hash for a new buffer "
                             "at the same address (mod buffers after a reload); read the buffer to be sure. "
                             f"Fixed in 0.1.4: restart the game to load {s.probe_dll}")
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
    contents_feature, contents_request, contents_summary = contents.FEATURE, contents.request, contents.summary

    @srv.tool("efmi_capture_frame",
              "Records the next frame the game draws and returns a summary. detail='full' records every binding of every draw "
              "(index/vertex buffers, shaders, constant buffers, textures, render targets); 'light' records only geometry and "
              "shaders; cb_data=true additionally keeps the contents of every constant buffer as it was at each draw. "
              "contents='inputs' has the probe keep what every call read (index, vertex and constant buffers, buffers "
              "and textures bound as shader resources) exactly as it was when the call was issued; 'buffers' the same "
              "without the textures, which is all that geometry and skinning need and the cheapest; 'outputs' also keeps "
              "what calls left in their render targets and unordered access views: of the calls selected with hash / "
              "draws (as in efmi_capture_draw_outputs) or first / last (call numbers in the new frame), or of every "
              "call when nothing selects (a render target per draw is large: mind budget_mb). Kept contents are read "
              "with efmi_read_buffer / efmi_read_texture and written out with efmi_dump_frame; they last until the "
              "next capture or three minutes without a request. A resource is copied only when the game is about to "
              "overwrite it, in the frames after the capture too, so the game can stutter briefly then. Needs probe "
              "0.1.6. The capture stays in memory for the other tools.",
              {"detail": {"type": "string", "enum": ["full", "light"], "default": "full"},
               "cb_data": {"type": "boolean", "default": False},
               "contents": {"type": "string", "enum": list(contents.LEVELS), "default": "none"},
               "hash": {"type": "string", "default": "",
                        "description": "contents='outputs': index buffer hash (8 hex digits) or shader hash (16) of the calls whose outputs to keep."},
               "draws": {"type": "array", "items": {"type": "integer"}, "default": [],
                         "description": "contents='outputs': call numbers in `capture` to find again in the new frame."},
               **CAPTURE_PROP,
               "first": {"type": "integer", "default": -1, "description": "contents='outputs': first call number in the new frame."},
               "last": {"type": "integer", "default": -1, "description": "contents='outputs': last call number in the new frame."},
               "depth": {"type": "boolean", "default": False, "description": "contents='outputs': keep depth targets too."},
               "budget_mb": {"type": "integer", "minimum": 16, "maximum": 8192, "default": contents.BUDGET_MB,
                             "description": "How much the probe may copy for this capture, in the game's memory."}},
              read_only=False)
    def efmi_capture_frame(detail: str = "full", cb_data: bool = False, contents: str = "none", hash: str = "",
                           draws=None, capture="latest", first: int = -1, last: int = -1, depth: bool = False,
                           budget_mb: int = 1024):
        t0 = time.perf_counter()
        wanted, chosen = None, ""
        if contents not in (None, "", "none"):
            status = s.call(lambda c: c.status())
            if contents_feature not in (status.get("features") or ()):
                raise ToolError(f"Probe {status.get('probe')} cannot keep contents; this needs 0.1.6. "
                                f"Restart the game to load the updated {s.probe_dll}.")
            matchers = None
            if contents == "outputs" and (hash or draws):
                reference = s.capture(capture) if (s.latest is not None or capture not in (None, "", "latest")) else None
                matchers, chosen, _hashes = draw_outputs.select(reference, hash, list(draws or []))
            wanted = contents_request(contents, matchers, int(first), int(last), bool(depth), budget_mb)
        doc, blob = s.call(lambda c: c.capture(detail=detail, cb_data=cb_data, timeout_ms=8000, contents=wanted))
        cap = Capture(doc, blob)
        s.add_capture(cap)
        ms = (time.perf_counter() - t0) * 1000
        draws_made = list(cap.draws())
        game = [d for d in draws_made if d["origin"] != "xxmi"]
        st = cap.stats
        lines = [f"capture {cap.frame} ({cap.detail}): {len(draws_made)} draws ({len(game)} from the game, {len(draws_made) - len(game)} from XXMI/mods), "
                 f"{len(cap.resources)} resources, {len(cap.shaders)} shaders; took {ms:.0f} ms "
                 f"(recording cost inside the game: {st['record_ms']:.2f} ms)"]
        if st.get("skipped_draws"):
            lines.append(f"WARNING: {st['skipped_draws']} draws beyond the per-frame limit were not recorded")
        if st.get("arena_full"):
            lines.append("WARNING: readback budget exhausted, some hashes are missing (state 'skipped')")
        if not draws_made:
            lines.append("WARNING: this frame contains no recorded draws; character/resource capture is not verified. "
                         "Check the selected device/context in efmi_status and efmi_probe.log.")
        lines += contents_summary(cap)
        if chosen:
            lines.append(f"  outputs kept for: {chosen}")
        if s.mods_display and not s.mods_display["enabled"]:
            lines.append(f"note: efmi_toggle_mods switched all mods OFF at frame {s.mods_display['frame']}; unless F11 was "
                         "pressed by hand since, this frame shows the game's own drawing. Switch them back on when done.")

        if cap.detail == "full":
            passes: "OrderedDict[tuple, list[int]]" = OrderedDict()
            for d in draws_made:
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
              "After a reload mods come back over several frames (none, then some, then all): light frames are "
              "looked at until what XXMI draws of the objects it created since the key press has stopped growing, "
              "for up to confirm_wait_ms, and the frame returned as 'latest' is taken after that "
              "(frames_looked_at, waited_for_xxmi_ms). mods_settled=false means it was still growing when the wait "
              "ended: capture again with efmi_capture_frame, without another F10. "
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
                                                "released. Ignored when the game was already in front."},
               "confirm_wait_ms": {"type": "integer", "minimum": 0, "maximum": 15000, "default": 3000,
                                   "description": "How long to go on looking at frames while mods are still coming "
                                                  "back. 0 returns the first frame after settle_ms, which may show "
                                                  "none or only some of them."}},
              read_only=False, annotations={"destructiveHint": False, "idempotentHint": False})
    def efmi_reload_and_capture(detail="full", cb_data=True, settle_ms=2000, timeout_ms=15000, focus_game=True,
                                restore_focus=True, confirm_wait_ms=3000):
        return reload_and_capture(s, detail=detail, cb_data=cb_data, settle_ms=settle_ms,
                                  timeout_ms=timeout_ms, focus_game=focus_game, restore_focus=restore_focus,
                                  confirm_wait_ms=confirm_wait_ms)

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
                               "bound window), textures per slot, render targets and viewport. For a full capture also the "
                               "blend, depth-stencil and rasterizer state, scissor rectangles, samplers, the part of a "
                               "resource a view shows when it is not all of it, and the arguments an indirect draw read "
                               "from its buffer (probe 0.1.5).",
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
            # "args" are the values the call read from its buffer (probe 0.1.5).
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
            out["constant_buffers"] = {}
            for k, v in d["cb"].items():
                entry = {"hash": v["hash"], "first_constant": v["first_constant"], "num_constants": v["num_constants"],
                         "contents_captured": cap.snap_bytes(v["snap"]) is not None or v.get("content") is not None,
                         "resource": _res_desc(cap.resource(v["res"]))}
                if v["snap"] >= 0 and cap.snap_bytes(v["snap"]) is None and v.get("content") is None:
                    # Asked for (cb_data) and not delivered: the probe's copy space was full or the read-back failed.
                    entry["contents_missing"] = ("the probe could not keep this window"
                                                 + (" (its copy space was full)" if cap.stats.get("arena_full") else ""))
                out["constant_buffers"][k] = entry
        def view_note(v):
            # Only where the view shows a part of its resource, or reads it in another format.
            view = v.get("view")
            if not view or pipeline.view_is_plain(view, cap.resource(v["res"])):
                return ""
            return f" | view: {pipeline.view_text(view)}"

        if "tex" in d:
            out["textures"] = {}
            for k, v in d["tex"].items():
                r = cap.resource(v["res"])
                out["textures"][k] = (f"{v['hash'] or 'unknown'} {_res_desc(r)}" + (" (xxmi)" if r and r.get("origin") == "xxmi" else "")
                                      + view_note(v) + s.mod_note(v["hash"]))
        if "out" in d:
            out["outputs"] = {k: f"{v['hash'] or 'not created by the game (back buffer?)'} {_res_desc(cap.resource(v['res']))}"
                                 + view_note(v) for k, v in d["out"].items()}
        if "viewport" in d:
            out["viewport"] = d["viewport"]
        if "viewports" in d:
            out["viewports"] = d["viewports"]       # all of them: [left, top, width, height, min depth, max depth]
        if "blend" in d:
            if d.get("viewport_count", 1) != 1:
                out["viewport_count"] = d["viewport_count"]
            out["viewport_depth_range"] = d["depth_range"]
            slots = [int(k[1:]) + 1 for k in d.get("out", {}) if k[1:].isdigit() and k[0] == "o"]
            out["blend"] = pipeline.describe_blend(d["blend"], max(slots, default=0))
            out["depth_stencil"] = pipeline.describe_depth(d["depth_stencil"])
            out["rasterizer"] = pipeline.describe_raster(d["rasterizer"])
            if d["scissors"] and out["rasterizer"].get("scissor"):
                out["scissor_rects"] = d["scissors"]      # [left, top, right, bottom], one per viewport
        elif cap.detail == "full" and d["type"] < 8:
            out["pipeline_state"] = ("not recorded: the game runs a probe older than 0.1.5 "
                                     "(restart the game to load the updated efmi_probe.dll)")
        if "samplers" in d:
            out["samplers"] = {k: pipeline.describe_sampler(v) for k, v in d["samplers"].items()}
        if cap.has_contents:
            # Which slots the probe keeps as this call saw them (efmi_read_buffer / efmi_read_texture).
            kept = (["ib"] if (d.get("ib") or {}).get("content") is not None else []) \
                + [f"vb{n}" for n, v in d.get("vb", {}).items() if v.get("content") is not None] \
                + [k for group in ("cb", "tex") for k, v in d.get(group, {}).items() if v.get("content") is not None]
            left = [k for k, v in d.get("out", {}).items() if v.get("content") is not None]
            out["contents_kept"] = {"as_the_call_saw_them": kept, "as_the_call_left_them": left}
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
    def _slot(cap: Capture, d: dict, slot: str) -> dict:
        try:
            return slot_data.buffer_binding(cap, d, slot)
        except slot_data.SlotError as exc:
            raise ToolError(str(exc))

    def _index_data(cap: Capture, d: dict) -> tuple[list[int], dict]:
        b = _slot(cap, d, "ib")
        if not b["exact_range"]:
            raise ToolError("the index count of this indirect draw is not known (its arguments were not recorded)")
        if not b["size"]:
            raise ToolError("this draw is not indexed")
        data, info = slot_data.read_slot(s, cap, b)
        ib = d["ib"]
        meta = {**info, "hash_now": hex32(crc32c(data)), "hash_at_capture": ib["hash"]}
        # The region hash of the capture is the stronger witness where there is one.
        meta["unchanged"] = meta["hash_now"] == ib["hash"] if ib["hash"] else info["exact"]
        return decode.decode_indices(data, ib["format"]), meta

    def _vertex_range(cap: Capture, d: dict, slot: int, per_instance: bool) -> tuple[int, int]:
        """(first element, count) of the vertices (or instances) this draw reads from a slot."""
        a = dict(d["args"])
        a.update((d.get("indirect") or {}).get("args") or {})
        if per_instance:
            return a.get("first_instance", 0), max(a.get("instance_count", 1), 1)
        if a.get("index_count"):
            idx, _ = _index_data(cap, d)
            base = a.get("first_vertex", 0)
            return min(idx) + base, max(idx) - min(idx) + 1
        return a.get("first_vertex", 0), a.get("vertex_count", 0)

    def _words(chunk: bytes, view: str):
        """One element or register as numbers; bytes that do not fill a 32-bit word are shown in hex."""
        if view == "hex" or len(chunk) < 4:
            return chunk.hex()
        n = len(chunk) // 4
        values = list(struct.unpack_from(f"<{n}{'I' if view == 'uint' else 'f'}", chunk))
        if view != "uint":
            values = [round(v, 6) for v in values]
        return values + [chunk[n * 4:].hex()] if len(chunk) % 4 else values

    @srv.tool("efmi_read_buffer",
              "Reads the data a draw or dispatch uses from one of its buffers and decodes it. slot: 'ib' (indices), 'vb0'.. "
              "(vertices, decoded with the input layout), a constant buffer such as 'vs-cb0' / 'ps-cb1' (rows of four "
              "floats), a buffer bound as a shader resource such as 'vs-t0' (a bone or instance pool: one row per "
              "element of the view) or an unordered access view 'u0'.. (what a compute shader or a draw writes). "
              "`first`/`count` page through the decoded elements. By default the part the binding exposes is read: the "
              "indices and vertices the draw uses, the bound window of a constant buffer, the elements of a view; "
              "whole=true reads the whole buffer (indices and vertices are then not decoded: use save). `source` and "
              "`exact` say where the bytes come from: the capture's own contents (a capture taken with contents, or a "
              "constant buffer window with cb_data=true) are those of the call; otherwise the buffer is read now and "
              "`exact` is true only when nothing wrote to those bytes since the capture.",
              {**CAPTURE_PROP, "draw": {"type": "integer"}, "slot": {"type": "string"},
               "first": {"type": "integer", "default": 0}, "count": {"type": "integer", "default": 16},
               "view": {"type": "string", "enum": ["float", "uint", "hex"], "default": "float",
                        "description": "how to show constant buffer registers and the elements of resource views"},
               "whole": {"type": "boolean", "default": False,
                         "description": "the whole buffer instead of the part the binding exposes"},
               "save": {"type": "boolean", "default": False, "description": "also write the raw bytes to a .buf file"}},
              ["draw", "slot"])
    def efmi_read_buffer(draw: int, slot: str, capture="latest", first: int = 0, count: int = 16, view: str = "float",
                         save: bool = False, whole: bool = False):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        b = _slot(cap, d, slot)
        slot, role = b["slot"], b["role"]
        first = max(0, int(first))
        count = max(1, min(int(count), 256))
        out: dict = {"capture": cap.frame, "draw": draw, "slot": slot}
        raw = b""

        if whole and role in ("ib", "vb"):
            raw, info = slot_data.read_slot(s, cap, b, whole=True)
            out.update(bytes=len(raw), crc32c=hex32(crc32c(raw)), source=info["note"], exact=info["exact"],
                       bind_offset=b["binding"]["offset"],
                       note="the whole buffer, not decoded: the part this draw uses starts at bind_offset")
        elif role == "ib":
            idx, meta = _index_data(cap, d)
            out.update(format=decode.format_name(d["ib"]["format"]), index_count=len(idx), min=min(idx), max=max(idx),
                       hash=meta["hash_at_capture"], unchanged_since_capture=meta["unchanged"], source=meta["note"],
                       exact=meta["exact"] or meta["unchanged"], first=first, indices=idx[first:first + count * 3])
            raw = struct.pack(f"<{len(idx)}{'I' if d['ib']['format'] == 42 else 'H'}", *idx)
        elif role == "vb":
            vb, n = b["binding"], int(slot[2:])
            lay = cap.layout(d.get("layout", -1)) or []
            elems = [e for e in lay if e["slot"] == n]
            per_instance = bool(elems) and all(e["per_instance"] for e in elems)
            start, total = _vertex_range(cap, d, n, per_instance)
            raw, info = slot_data.read_slot(s, cap, b, vb["offset"] + vb["stride"] * start, vb["stride"] * total)
            out.update(stride=vb["stride"], first_element=start, element_count=total,
                       kind="per-instance data" if per_instance else "vertices",
                       region_hash_at_capture=vb["hash"], read_frame=info.get("read_frame"),
                       unchanged_since_capture=True if info["source"] == "contents" else info.get("unchanged_since_capture"),
                       source=info["note"], exact=info["exact"])
            if elems:
                out["layout"] = [f"{e['semantic']}{e['index'] or ''}:{decode.format_name(e['format'])}@{e['offset']}" for e in elems]
                out["first"] = first
                out["elements"] = decode.decode_vertices(raw, vb["stride"], elems, first, count)
            else:
                out["note"] = "no input layout element uses this slot; showing raw bytes"
                out["hex"] = [raw[i:i + vb["stride"]].hex() for i in range(first * vb["stride"], min(len(raw), (first + count) * vb["stride"]), vb["stride"])]
        elif role == "cb":
            cb = b["binding"]
            raw, info = slot_data.read_slot(s, cap, b, whole=whole)
            rows = len(raw) // 16
            out.update(hash=cb["hash"], first_constant=0 if whole else cb["first_constant"], registers=rows, first=first,
                       source=info["note"], exact=info["exact"])
            if whole:
                out["bound_window"] = {"first_constant": cb["first_constant"], "num_constants": cb["num_constants"]}
            out["values"] = {f"c{i}": _words(raw[i * 16:i * 16 + 16], view) for i in range(first, min(rows, first + count))}
        else:
            raw, info = slot_data.read_slot(s, cap, b, whole=whole)
            element = b["element"]
            total = len(raw) // element
            out.update(kind="unordered access view" if role == "uav" else "shader resource view",
                       hash=b["binding"].get("hash"), resource=_res_desc(b["resource"]), element_bytes=element,
                       first_element_of_buffer=info["offset"] // element, elements=total, first=first,
                       source=info["note"], exact=info["exact"])
            if b.get("view"):
                out["view"] = pipeline.view_text(b["view"])
            out["values"] = {f"e{i}": _words(raw[i * element:(i + 1) * element], view)
                             for i in range(first, min(total, first + count))}

        if save and raw:
            path = s.frame_dir(cap) / f"draw{draw}-{slot}{'-whole' if whole else ''}.buf"
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
            raw, _info = slot_data.read_slot(s, cap, _slot(cap, d, f"vb{n}"), vb["offset"] + vb["stride"] * (lo + base),
                                             vb["stride"] * (hi - lo + 1))
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
                 "ib_hash": ib["hash"], "index_data_unchanged_since_capture": meta["unchanged"]}
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
              "Reads a texture bound for a draw (slot such as 'ps-t0', an output 'o0' / 'oD', or an unordered access view "
              "'u0'). When the capture kept contents (efmi_capture_frame contents=...), the answer is the texture as the "
              "call saw it, or for an output as the call left it; otherwise it is the texture as it is now, which the "
              "answer says. mip / slice default to the subresource the bound view shows. 3D textures are read whole "
              "(slice then picks the depth slice of the picture), multisampled colour textures resolved. Saves .dds "
              "(and .png when the format can be converted) and returns a downscaled preview image.",
              {**CAPTURE_PROP, "draw": {"type": "integer"}, "slot": {"type": "string"},
               "mip": {"type": "integer", "default": -1, "description": "-1: the first mip the view shows"},
               "slice": {"type": "integer", "default": -1,
                         "description": "array slice or cube face; -1: the first the view shows. For a 3D texture: the depth slice shown."},
               "preview": {"type": "boolean", "default": True}},
              ["draw", "slot"], read_only=False)
    def efmi_read_texture(draw: int, slot: str, capture="latest", mip: int = -1, slice: int = -1, preview: bool = True):
        cap = s.capture(capture)
        if not 0 <= draw < len(cap):
            raise ToolError(f"draw must be 0..{len(cap) - 1}")
        d = cap.draw(draw)
        slot = slot.strip()
        entry = d.get("tex", {}).get(slot.lower()) or d.get("out", {}).get(slot)
        if not entry:
            raise ToolError(f"nothing bound to {slot}; textures: {list(d.get('tex', {}))}; outputs: {list(d.get('out', {}))}")
        resource = cap.resource(entry["res"]) or {}
        if resource.get("kind") == "buffer":
            raise ToolError(f"{slot} holds a buffer, not a texture: use efmi_read_buffer")
        volume = resource.get("kind") == "tex3d"
        view = entry.get("view") or {}
        kept = cap.content(entry.get("content"))
        # What the view shows first; a kept entry is exactly that subresource.
        shown = (kept["mip"], kept["slice"]) if kept else (
            view.get("most_detailed_mip", view.get("mip_slice", 0)), 0 if volume else view.get("first_slice", view.get("first_face", 0)))
        want_mip = shown[0] if mip < 0 else mip
        want_slice = (0 if volume else shown[1]) if slice < 0 else (0 if volume else slice)
        meta = data = None
        when, from_capture = "", False
        if kept and kept["kind"] == "texture" and (kept["mip"], kept["slice"]) == (want_mip, want_slice):
            try:
                meta, data = s.call(lambda c: c.read_content(cap.frame, kept["id"]))
                from_capture = True
                when = ("as the call left it" if slot in d.get("out", {}) else "as it was when the call was issued")
                if not meta.get("exact", True):
                    when += f" (NOT CERTAIN: {meta.get('why', 'unknown writes happened since')})"
            except ToolError as exc:
                when = f"current contents, not necessarily those at the draw (what the capture kept could not be used: {exc})"
        if data is None:
            meta, data = s.call(lambda c: c.read_texture(cap.frame, entry["res"], want_mip, want_slice))
            when = when or ("current contents, not necessarily those at the draw"
                            + ("; the capture kept another subresource of this texture" if kept else
                               "; capture with contents to get it as the call saw it" if not cap.has_contents else ""))
        w, h, fmt = meta["width"], meta["height"], meta["format"]
        depth = meta.get("depth", 0)
        stem = f"{entry['hash'] or 'res%d' % entry['res']}-mip{want_mip}" + (f"-slice{want_slice}" if want_slice else "")
        if from_capture:
            stem += f"-draw{draw}"            # one file per version, not one per texture
        folder = s.frame_dir(cap)
        dds_path = folder / f"{stem}.dds"
        dds_path.write_bytes(decode.make_dds(w, h, fmt, data, depth=depth))
        shape = f"{meta['tex_width']}x{meta['tex_height']}" + (f"x{meta['tex_depth']}" if depth else "")
        text = [f"{slot} of draw {draw}: hash {entry['hash'] or 'unknown'}, {shape} {meta['format_name']}, "
                f"{meta['mips']} mips, array {meta['array']}{', cube' if meta.get('cube') else ''}",
                f"read mip {want_mip} ({w}x{h}{'x%d' % depth if depth else ''}) at frame {meta['read_frame']} ({when})",
                f"saved: {dds_path}"]
        if meta.get("resolved"):
            text.append(f"multisampled ({meta.get('samples')} samples): these are the resolved pixels")
        result: list = []
        picture = data
        if depth:
            shown_slice = max(0, min(slice if slice >= 0 else 0, depth - 1))
            size = len(data) // depth
            picture = data[shown_slice * size:(shown_slice + 1) * size]
            text.append(f"picture: depth slice {shown_slice} of {depth}")
        png, small = decode.texture_to_pngs(w, h, fmt, picture, preview_max_size=512 if preview else 0)
        if png:
            png_path = folder / f"{stem}.png"
            png_path.write_bytes(png)
            text.append(f"saved: {png_path}")
            if small:
                result.append(Image(small))
        else:
            text.append("no PNG: this format is not converted here (the .dds holds the exact data)")
        return ["\n".join(text)] + result

    # ================================================================ the writes between the calls
    @srv.tool("efmi_list_writes",
              "The maps, updates, copies, clears, mip generations and resolves a frame issued between its calls, in "
              "order: what changed which resource before which call. From a capture taken with contents (probe 0.1.6). "
              "Filter by a range of calls, by the hash of the resource written to or read from, or by kind. Draws and "
              "dispatches also write (their render targets and unordered access views): those are in efmi_get_draw. "
              "bindings=true adds, for every call in the range, what it has bound differently from the call of its "
              "kind before it (shaders, buffers, constant buffer windows, textures, targets, state): the picture the "
              "Set* calls of a frame analysis log give. Those lines are worked out from the calls' bindings, so their "
              "place among the writes before the same call is not known. Works on any full capture.",
              {**CAPTURE_PROP, "first": {"type": "integer", "default": 0, "description": "writes issued before this call or later"},
               "last": {"type": "integer", "default": -1, "description": "writes issued before this call or earlier; -1: to the end"},
               "hash": {"type": "string", "default": "", "description": "only writes to or from the resource with this hash"},
               "kind": {"type": "string", "default": "", "description": "e.g. Map, UpdateSubresource, CopySubresourceRegion, ClearRenderTargetView"},
               "bindings": {"type": "boolean", "default": False,
                            "description": "also what each call has bound differently from the call before it"},
               "offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 60}})
    def efmi_list_writes(capture="latest", first: int = 0, last: int = -1, hash: str = "", kind: str = "",
                         bindings: bool = False, offset: int = 0, limit: int = 60):
        cap = s.capture(capture)
        listed = "ev" in cap.doc
        if not listed and not bindings:
            raise ToolError(f"capture {cap.frame} has no list of writes: take one with efmi_capture_frame contents='inputs' "
                            "(probe 0.1.6), or ask for bindings=true, which any full capture can answer")
        if bindings and cap.detail != "full":
            raise ToolError("bindings need a full capture")
        wanted = _norm_hash(hash)
        events = []
        for event in cap.events():
            if event["before_call"] < first or (last >= 0 and event["before_call"] > last):
                continue
            if kind and event["type"].lower() != kind.lower():
                continue
            if wanted and wanted not in [(cap.resource(event[k]) or {}).get("hash") for k in ("dst", "src")]:
                continue
            events.append(event)
        rows = [(event["before_call"], contents.event_text(cap, event)) for event in events]
        changed = 0
        if bindings:
            # After the writes before the same call: where among them the game set these is not known.
            end = len(cap) - 1 if last < 0 else min(last, len(cap) - 1)
            start = max(0, min(first, end))
            bound = {False: contents.previous_bindings(cap, start, False), True: contents.previous_bindings(cap, start, True)}
            merged: list[tuple[int, str]] = []
            by_call: dict[int, list[str]] = {}
            for number, text in rows:
                by_call.setdefault(number, []).append(text)
            for number in range(start, end + 1):
                d = cap.draw(number)
                merged += [(number, text) for text in by_call.pop(number, [])]
                now = contents.bindings_of(cap, d)
                for line in contents.binding_changes(bound[d["type"] >= 8], now):
                    if not wanted or wanted in line:
                        merged.append((number, line))
                        changed += 1
                bound[d["type"] >= 8] = now
            rows = merged + [(number, text) for number, texts in sorted(by_call.items()) for text in texts]
        limit = max(1, min(int(limit), 300))
        page = rows[offset:offset + limit]
        kinds = Counter(event["type"] for event in events)
        lines = [(f"capture {cap.frame}: {len(events)} writes match of {len(cap.raw_events)} listed"
                  + (f" ({', '.join(f'{n} {k}' for k, n in kinds.most_common())})" if events else "")) if listed else
                 f"capture {cap.frame}: no writes were listed (it was taken without contents)"]
        if bindings:
            lines.append(f"{changed} binding changes (each call against the call of its kind before it; derived from the "
                         "bindings, not observed Set* calls)")
        if cap.stats.get("events_dropped"):
            lines.append(f"WARNING: {cap.stats['events_dropped']} more writes were not listed (the list was full)")
        lines += [f"before #{number:<5} {text}" for number, text in page]
        if offset + len(page) < len(rows):
            lines.append(f"... {len(rows) - offset - len(page)} more (use offset={offset + len(page)})")
        return "\n".join(lines)

    # ================================================================ a frame as a folder
    @srv.tool("efmi_dump_frame",
              "Writes calls of a capture to a folder laid out like an XXMI frame analysis dump: per call and slot a file "
              "named NNNNNN-<slot>=<hash>-vs=...-ps=..., linked to one copy of each distinct content under deduped/. "
              "Buffers are written whole as .buf, with the .txt XXMI writes for index buffers (the indices of the draw), "
              "vertex buffers (layout and the vertices the draw uses) and constant buffers (the bound registers); "
              "textures as .dds holding the subresource the view shows (XXMI: always the first); outputs as the call "
              "left them; log.txt lists the calls with the writes between them. Use a capture taken with contents: "
              "then every file holds what the call saw. From a capture without, only what provably has not changed "
              "since is written unless only_exact=false. Calls are numbered as in the capture (from 0). XXMI's own "
              "command list and variable trace is not available here. A whole game frame is gigabytes: select calls "
              "with first/last and kinds; the answer says where to continue when a limit stops it.",
              {**CAPTURE_PROP, "first": {"type": "integer", "default": 0}, "last": {"type": "integer", "default": -1, "description": "-1: the last call"},
               "kinds": {"type": "array", "items": {"type": "string", "enum": ["ib", "vb", "cb", "buf", "tex", "out"]}, "default": [],
                         "description": "empty: all. buf: buffers bound as shader resources, tex: textures, out: what calls wrote."},
               "txt": {"type": "boolean", "default": True},
               "only_exact": {"type": "boolean", "default": True},
               "max_mb": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 2048},
               "max_seconds": {"type": "integer", "minimum": 5, "maximum": 1800, "default": 120},
               "folder": {"type": "string", "default": "", "description": "default: <output folder>/frame-<n>/dump"}},
              read_only=False)
    def efmi_dump_frame(capture="latest", first: int = 0, last: int = -1, kinds=None, txt: bool = True,
                        only_exact: bool = True, max_mb: int = 2048, max_seconds: int = 120, folder: str = ""):
        cap = s.capture(capture)
        return contents.dump_frame(s, cap, first=int(first), last=int(last), kinds=list(kinds or []), txt=bool(txt),
                                   only_exact=bool(only_exact), max_mb=int(max_mb), max_seconds=float(max_seconds),
                                   folder=folder)

    # ================================================================ what a draw paints
    @srv.tool("efmi_capture_draw_outputs",
              "Shows what chosen draws paint. Records a new frame and has the probe copy the render target just before "
              "and just after each selected draw; reports per draw how many pixels it changed and where, saves the "
              "pictures and returns previews of the changed areas. Select with `hash` (an index buffer hash as listed "
              "by efmi_list_draws: every pass that draws that mesh; or a 16-digit vs/ps hash) or with `draws` (draw "
              "numbers of an existing capture: exactly those meshes with those shaders). The draws are found again in "
              "the new frame by index buffer object, range and shaders, so the mesh has to be on screen still. At most "
              "max_draws draws are copied, in frame order; a mesh is usually drawn in several passes (shadow, depth, "
              "colour, outline), each into its own target. Passes without a colour target are only counted, unless "
              "depth=true. An indirect draw has no index buffer hash: select it by draw number or shader. `box` limits "
              "the copy to a part of the target, which keeps large targets cheap; depth targets are always copied "
              "whole, and shadow maps are large (4096x4096), so depth=true costs tens of MiB per shadow pass. The game "
              "keeps its off-screen targets upside down relative to the screen: pictures and boxes use the target's "
              "own rows. The new frame becomes 'latest'. Multisampled targets are not copied. Needs probe 0.1.5.",
              {**CAPTURE_PROP,
               "hash": {"type": "string", "default": "", "description": "Index buffer hash (8 hex digits) or shader hash (16)."},
               "draws": {"type": "array", "items": {"type": "integer"}, "default": [],
                         "description": "Draw numbers in `capture` to find again."},
               "max_draws": {"type": "integer", "minimum": 1, "maximum": 32, "default": 8},
               "before": {"type": "boolean", "default": True,
                          "description": "Also copy the target before the draw, which is what shows the changed pixels."},
               "targets": {"type": "string", "enum": ["first", "all"], "default": "first",
                           "description": "first: the first bound colour target; all: every colour target (G-buffer passes)."},
               "depth": {"type": "boolean", "default": False,
                         "description": "Copy the depth-stencil target as well; shadow and depth passes have nothing else."},
               "box": {"type": "array", "items": {"type": "integer"}, "default": [],
                       "description": "[left, top, right, bottom] in target pixels; empty: the whole target."},
               "preview": {"type": "boolean", "default": True}},
              read_only=False)
    def efmi_capture_draw_outputs(hash="", draws=None, capture="latest", max_draws=8, before=True, targets="first",
                                  depth=False, box=None, preview=True):
        return capture_draw_outputs(s, hash=hash, draws=list(draws or []), capture=capture, max_draws=max_draws,
                                    before=before, targets=targets, depth=depth, box=list(box) if box else None,
                                    preview=preview)

    # ================================================================ shaders
    @srv.tool("efmi_get_shader", "Disassembly of a shader the game (or a mod) created, by hash. Cached in memory; "
                                 "`first_line`/`max_lines` page through it without writing files. Use "
                                 "efmi_get_texture_slots for batch slot lookup; save=true explicitly exports BIN/ASM.",
              {"hash": {"type": "string"}, "first_line": {"type": "integer", "default": 1},
               "max_lines": {"type": "integer", "default": 160},
               "save": {"type": "boolean", "default": False}}, ["hash"], read_only=False)
    def efmi_get_shader(hash: str, first_line: int = 1, max_lines: int = 160, save: bool = False):
        h = _norm_hash(hash)
        listing = s.shader_listing(h)
        label = f"shader {h}"
        if save:
            folder = s.out_dir / "shaders"
            folder.mkdir(parents=True, exist_ok=True)
            stage = listing.info.get("type")
            if stage not in STAGES:
                raise ToolError(f"unknown shader stage {stage!r}")
            (folder / f"{h}-{stage}.bin").write_bytes(listing.bytecode)
            asm_path = folder / f"{h}-{stage}.asm"
            asm_path.write_text(listing.text, encoding="utf-8")
            label = str(asm_path)
        lines = listing.lines
        first_line = max(1, int(first_line))
        max_lines = max(1, min(int(max_lines), 600))
        page = lines[first_line - 1:first_line - 1 + max_lines]
        head = f"{label} ({len(lines)} lines); showing {first_line}-{first_line + len(page) - 1}"
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
        slots = [slot for d in dump.draws.values() for slot in d.slots.values()]
        marked = Counter(slot.marks for slot in slots if slot.marks)
        if marked:
            # XXMI's own note that a resource was written to after its hash was taken.
            lines.append("  resources XXMI marked as changed since their hash was taken: "
                         + ", ".join(f"{n} by {'+'.join(MARKS.get(c, c) for c in marks)}" for marks, n in sorted(marked.items())))
        nameless = sum(1 for slot in slots if slot.hash is None)
        if nameless:
            lines.append(f"  slots XXMI dumped without a hash: {nameless}")
        for n in nums[:max(1, min(int(limit), 200))]:
            d = dump.draws[n]
            ib = d.slots.get("ib")
            ibs = f" ib={ib.hash or '-'}" + (f"({ib.full_hash})" if ib and ib.full_hash else "") if ib else ""
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

    texture_slots.register(srv, s, CAPTURE_PROP)
    model_tools.register(srv, s)
    return srv
