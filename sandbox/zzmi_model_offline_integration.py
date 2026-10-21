"""Zenless Zone Zero model workflow with the real LoyalTools and Blender, without the game.

Drives the MCP tools on an XXMI frame analysis folder of Zenless Zone Zero
(made with ZZMI's F8): the listing of LoyalTools' ZZMI extractor, the
merged-skeleton extraction and import in background Blender, the add-on's own
export and the file checks of its result, then an edit made in Blender and a
second export that has to carry it. The frame folder is only read; everything
is written into a scratch folder under sandbox/build.

  python sandbox/zzmi_model_offline_integration.py --dump "D:/XXMI/ZZMI/FrameAnalysis-..."

Needs a Blender with LoyalTools 1.7 or later and a frame of a character, so it
is not part of run_all.py. What it cannot cover: a frame captured live,
deployment, reload and the draw comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "sandbox"))

from efmi_mcp import blender_runner, model_tools, tools  # noqa: E402
from efmi_mcp.model_workflow import read_ini_sections  # noqa: E402
from efmi_mcp.probe_client import ProbeNotFound  # noqa: E402
from model_offline_integration import EDIT_SCRIPT, moved_by  # noqa: E402

DEFAULT_DUMP = Path(r"D:\XXMI-Launcher-Portable-v2.1.6\ZZMI\FrameAnalysis-2026-10-21-120040")
failures = 0
console = sys.stdout


def check(name, condition, detail=""):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not condition else ""),
          file=console, flush=True)
    failures += not condition


def positions(mod: Path) -> dict[str, tuple[bytes, int]]:
    """Every vertex buffer of the mod that the skinning draw reads as vb0, with its stride (from the INI)."""
    out = {}
    for ini in mod.glob("*.ini"):
        sections = dict((name.lower(), lines) for name, lines in read_ini_sections(ini))
        for name, lines in sections.items():
            for line in lines:
                key, _, value = (part.strip() for part in line.partition("="))
                if key.lower() == "vb0" and value.lower() in sections:
                    values = dict((k.strip().lower(), v.strip()) for k, _, v in
                                  (entry.partition("=") for entry in sections[value.lower()]))
                    path = mod / values["filename"].replace("\\", "/")
                    out[path.relative_to(mod).as_posix()] = (path.read_bytes(), int(values.get("stride", 0)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP, help="Frame analysis folder of ZZMI (read only)")
    parser.add_argument("--blender", default="")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch folder")
    args = parser.parse_args()
    dump = args.dump.resolve()
    build = ROOT / "sandbox" / "build"
    build.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="zzmi-model-", dir=build))
    project = scratch / "zzmi-offline"
    # An installation folder that is ZZMI by its d3dx.ini, so the server takes the ZZMI path without a game.
    zzmi = scratch / "ZZMI"
    (zzmi / "Mods").mkdir(parents=True)
    (zzmi / "d3dx.ini").write_text("[Include]\ninclude = Core\\ZZMI\\main.ini\n[Rendering]\ntrack_region_hashes = 0\n",
                                   encoding="utf-8")
    print(f"scratch: {scratch}", file=console)
    files_before = sorted((p.name, p.stat().st_size) for p in dump.iterdir() if p.is_file())
    server = tools.build_server(zzmi, scratch / "out", scratch / "probe.dll", blender_path=args.blender)

    def call(name, **arguments):
        reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
        text = reply["result"]["content"][0]["text"]
        return (json.loads(text) if text.lstrip().startswith("{") else text), reply["result"]["isError"]

    try:
        with patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("offline test")), \
                patch.object(tools, "find_probes", return_value=[]), \
                patch.object(model_tools, "find_probes", return_value=[]):
            print("== listing the frame (LoyalTools' ZZMI extractor)", file=console)
            listing, error = call("efmi_list_models", frame_dump=str(dump))
            check("the frame lists meshes", not error and bool(listing.get("meshes")), str(listing)[:400])
            check("skinned meshes come first", not error and listing["meshes"][0]["skinned"]
                  and listing["skinned"] == [m["ib_hash"] for m in listing["meshes"] if m["skinned"]])
            print("     ", len(listing["meshes"]), "meshes,", len(listing["skinned"]), "skinned", file=console)

            print("== extraction and import (merged skeleton, Blender)", file=console)
            result, error = call("efmi_extract_model", output_dir=str(project), frame_dump=str(dump), wait_s=300)
            extracted = result.get("result", {}) if isinstance(result, dict) else {}
            check("the job finishes", not error and result.get("status") == "done", json.dumps(result, ensure_ascii=False)[:800])
            blender = extracted.get("stages", {}).get("blender", {})
            roles = {obj["role"] for obj in blender.get("objects", [])}
            check("the .blend is saved", Path(extracted.get("blend", "-")).is_file())
            check("merged parts and shape-keyed faces", "merged" in roles, str(roles))
            check("the frame was only read", sorted((p.name, p.stat().st_size) for p in dump.iterdir() if p.is_file())
                  == files_before)
            print("     ", blender.get("submeshes"), "submeshes,", blender.get("vertices"), "vertices,", roles, file=console)
            described, error = call("efmi_model_project", project_dir=str(project))
            check("the project is a ZZMI project", not error and described.get("game") == "ZZMI", str(described)[:300])
            text, error = call("efmi_map_lod", project_dir=str(project), method="json")
            check("a LoD mapping is refused: one level of detail", error and "one level of detail" in str(text), str(text))

            print("== export with the add-on", file=console)
            result, error = call("efmi_export_mod", project_dir=str(project), wait_s=300)
            export = result.get("result", {}) if isinstance(result, dict) else {}
            check("the export finishes and is valid", not error and result.get("status") == "done"
                  and export.get("export_valid") is True, json.dumps(result, ensure_ascii=False)[:800])
            coverage = export.get("entry_point_coverage") or {}
            check("every part has an entry point", coverage.get("missing") == [] and bool(coverage.get("parts")),
                  str(coverage))
            manifest = json.loads(Path(export["manifest"]).read_text(encoding="utf-8"))
            merged = [c for c in (blender.get("profile") or {}).get("components", []) if c["role"] == "merged"]
            check("one skinning draw per merged part", len(manifest["vertex_draws"]) == len(merged),
                  f"{len(manifest['vertex_draws'])} vs {len(merged)}")
            check("draw ranges were checked against the index buffers", bool(manifest["draws"])
                  and all("max_index" in draw for draw in manifest["draws"]))
            check("the manifest says the game", manifest.get("game") == "ZZMI")
            check("the .blend was not modified by the export", manifest["blend"]["sha256"] ==
                  hashlib.sha256(Path(extracted["blend"]).read_bytes()).hexdigest())
            print("     ", export["files"], "files,", export["bytes"], "bytes;", len(manifest["draws"]), "draws,",
                  len(manifest["vertex_draws"]), "skinning draws", file=console)

            print("== an edit made in Blender reaches the export", file=console)
            before = positions(Path(export["mod_dir"]))
            script = scratch / "edit_in_blender.py"
            script.write_text(EDIT_SCRIPT, encoding="utf-8")
            edit = blender_runner.run_script(blender_runner.find_blender(args.blender), script,
                                             ["--blend", extracted["blend"], "--delta", 0.01], scratch / "edit.log")
            check("Blender moved every mesh and saved the project",
                  edit["exit_code"] == 0 and int(edit["markers"].get("EDITED_MESHES", 0)) > 0,
                  str(blender_runner.tail(scratch / "edit.log", 8)))
            result, error = call("efmi_export_mod", project_dir=str(project), wait_s=300)
            second = result.get("result", {}) if isinstance(result, dict) else {}
            check("the edited project exports and is valid", not error and second.get("export_valid") is True,
                  json.dumps(result, ensure_ascii=False)[:600])
            after = positions(Path(second["mod_dir"]))
            check("the same vertex files", set(before) == set(after), str(sorted(set(before) ^ set(after))))
            for name in sorted(before):
                delta, why = moved_by(before[name][0], after.get(name, (b"", 0))[0], before[name][1])
                check(f"{name}: every vertex moved by one vector", delta is not None and max(map(abs, delta)) > 1e-4,
                      why or str(delta))
    finally:
        if not args.keep and not failures:
            shutil.rmtree(scratch, ignore_errors=True)
    print(("PASS" if not failures else f"FAIL: {failures} failed") + ("" if not failures and not args.keep else f"; {scratch}"),
          file=console, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
