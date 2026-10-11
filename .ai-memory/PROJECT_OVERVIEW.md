# EFMI MCP — Project overview

> Created 2026-10-05. The structure follows `E:\loyal tools\.ai-memory`; the content covers this project only.

## Goal

Give agents the D3D11 draw data of Arknights: Endfield while it runs, so that less depends on manual EFMI/XXMI FrameAnalysis dumps. The focus is on what characters use: IB/VB, shaders, textures, constant buffers, and hashes compatible with XXMI.

## Components and technology

- `probe/`: a C++17, MSVC x64 probe built as `dist/efmi_probe.dll`, injected by the launcher before the game creates its device.
- `server/`: a Python stdio MCP server that connects to the probe through a Windows named pipe. The basic functions use the standard library only; texture previews can use Pillow/numpy.
- `sandbox/`: a D3D11 test program and regression scripts, with a copy of the XXMI runtime inside the project for compatibility checks.
- `captures/`: meshes, textures, shaders and the like, exported on demand; not a directory of continuous frame logs.

## Current functions

Status query, full capture of a single frame, draw call filtering, hash lookup, IB/VB/CB reads, texture reads, shader disassembly, mesh export, summaries of consecutive frames, comparison of captures, read-only scanning of mods, and comparison with offline frame dumps.

`efmi_reload_and_capture` (probe 0.1.2 / server 0.1.4): checks the game, the installation and the default double F10 binding, stops live mode and invalidates old captures, has the probe press F10 once inside the game process (taking the foreground if needed and giving it back), waits, captures a new frame, and reports `reload_confirmed` with "XXMI re-created, after the key press, resources/shaders that the new frame uses" as the evidence of the reload. Hunting mode is not needed. With an old probe it falls back to an external key press and says that the game has to be restarted.

Server 0.1.5 adds the model workflow: `efmi_list_models` / `efmi_extract_model` / `efmi_map_lod` / `efmi_export_mod` / `efmi_verify_exported_mod` / `efmi_model_project` / `efmi_model_job`. Grouping, skeleton merging, LOD matching and INI/BUF generation are done by the LoyalTools add-on installed in Blender; this project only calls its entry points and checks the results. The offline part is verified; live extraction and the deployment check were awaiting verification in the real game (T-008, D-014).

Probe 0.1.3 / server 0.1.6: `efmi_toggle_mods` has the probe press EFMI's own F11 inside the game, switching all mods off or on and recording a new frame; the on/off state is taken from EFMI's own notification. It is used to compare the original draws with the mods' draws, and to extract characters that a mod replaces. Passed with a real key in the sandbox; was awaiting verification in the real game (T-009, D-015).

Probe 0.1.5 / server 0.1.8–0.1.9 (2026-10-08, verified in the sandbox and in the real game): a full capture records blend, depth-stencil and rasterizer state, scissor rectangles, samplers, view parameters and the actual arguments of indirect draws, and `efmi_get_draw` shows them; `efmi_capture_draw_outputs` copies the render target once before and once after each selected draw, reports which pixels the draw changed and saves pictures; `efmi_reload_and_capture` looks at several more frames after a reload and returns the frame in which the mods are drawn again.

The failure of 0.1.3 in the real game was caused by UIPI (the game runs as administrator and drops key presses from processes with ordinary rights). The new path, in which the probe presses the key, is verified in the sandbox and in the real game (2026-10-06, PID 43816, once with the game in the foreground and once in the background); see BUG-004 / T-007 / D-012. Ordinary capture and IB/VB read-back work normally.

`0.1.1` fixed an auxiliary device overriding the capture target and in-flight frames being back-filled into a live session; the whole local regression suite passes and the new DLL is in place. Capture and resource read-back with the new version on a real-game screen showing Yvonne are verified as well; see `CURRENT_STATE.md` / T-003.

## Entry points on this machine

- Project: `F:\EFMI MCP`; server entry point: `server/run_server.py`.
- Python configured in the README: `D:\Python312\python.exe`.
- Default EFMI directory of the server: `D:\XXMI-Launcher-Portable-v2.1.6\EFMI`, overridable with `--efmi-root` / `EFMI_ROOT`; the tool does not write into its files directly.
- Probe log: `dist/efmi_probe.log`. The server version is `0.1.13`, the probe on disk is `0.1.5` with protocol number `1` (installed 2026-10-08; the game restarted that day, PID `9736`, loaded exactly that one); the output of `efmi_status` is authoritative for versions. History: the real game, PID `38252`, was once confirmed to report `0.1.1`; the empty captures of `0.1.0` before that are only evidence of a historical fault.
- Source repository: `https://github.com/loyal37/EFMI-MCP`. On 2026-10-06 the user asked for commit and push after verification from then on; see `AGENTS.md` / D-007.

## Long-term constraints

Change only our own tool; do not modify EFMI/XXMI, the launcher configuration on this machine, existing mods or game files. Reading real runtime data and looking at EFMI files read-only are part of this project's purpose; sandbox writes happen only in the project's private directories. Injection, connection, a non-empty capture and usable character data are verified separately and cannot stand in for one another.

The authorized automatic F10 reload does not change original files directly, but XXMI's own behaviour may save persistent variables, so it cannot be called a purely read-only operation. Ordinary capture of the latest picture does not need F10.
