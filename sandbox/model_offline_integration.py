"""Model workflow with the real LoyalTools and Blender, without the game.

Copies an existing LoyalTools merged-skeleton workspace into a scratch project
under sandbox/build and drives the MCP tools on it: Blender import with
diffuse-only materials, LoD mapping from a stored JSON, the add-on's own export
and the file checks of its result, then an edit made in Blender and a second
export that has to carry it. The source workspace is only read.

  python sandbox/model_offline_integration.py --workspace "E:/Endfield mods/<character>/workplace"

Needs a Blender with the LoyalTools add-on installed and a workspace on this
machine, so it is not part of run_all.py. What it cannot cover: sampling the
game, deployment, reload and the draw comparison.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import tools  # noqa: E402
from efmi_mcp.probe_client import ProbeNotFound  # noqa: E402

failures = 0
# A running job redirects sys.stdout for the whole process; keep writing to the console.
console = sys.stdout


def check(name, condition, detail=""):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not condition else ""),
          file=console, flush=True)
    failures += not condition


# Runs in Blender: the edit a user would make by hand, here the same move for
# every mesh so that the exported effect is known without knowing the exporter.
EDIT_SCRIPT = '''
import argparse, sys
import bpy
from mathutils import Vector

parser = argparse.ArgumentParser()
parser.add_argument("--blend", required=True)
parser.add_argument("--delta", type=float, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
if bpy.ops.wm.open_mainfile(filepath=args.blend, load_ui=False) != {"FINISHED"}:
    raise RuntimeError("could not open " + args.blend)
bpy.ops.preferences.addon_enable(module="LoyalTools")   # as in the user's Blender, so its data is kept on save
moved, seen = 0, set()
for obj in bpy.data.objects:
    if obj.type != "MESH" or obj.data.name in seen:
        continue
    seen.add(obj.data.name)
    local = obj.matrix_world.inverted().to_3x3() @ Vector((args.delta, 0.0, 0.0))   # one world-space vector
    for vertex in obj.data.vertices:
        vertex.co += local
    obj.data.update()
    moved += 1
if not moved:
    raise RuntimeError("no mesh in the file")
bpy.ops.wm.save_mainfile()
print("EDITED_MESHES=%d" % moved, flush=True)
'''


def moved_by(before: bytes, after: bytes, stride: int):
    """The vector by which every vertex position moved, or why there is none."""
    if not stride or stride < 12 or len(before) != len(after) or len(before) % stride:
        return None, f"sizes {len(before)} / {len(after)}, stride {stride}"
    layout = struct.Struct(f"<3f{stride - 12}x")    # the position leads each vertex of vb0
    deltas = [tuple(b - a for a, b in zip(old, new))
              for old, new in zip(layout.iter_unpack(before), layout.iter_unpack(after))]
    first = deltas[0]
    spread = max(abs(delta[axis] - first[axis]) for delta in deltas for axis in range(3))
    if spread > 1e-4:
        return None, f"vertices moved differently (spread {spread:.6f})"
    return first, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True, help="An existing workplace/ folder (read only)")
    parser.add_argument("--lod-json", default=None,
                        help="Profile JSON or workspace holding a LoD mapping (default: the source workspace itself)")
    parser.add_argument("--blender", default="")
    parser.add_argument("--skip-edit", action="store_true", help="Skip the edit in Blender and the second export")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch project")
    args = parser.parse_args()
    source = args.workspace.resolve()
    profile = json.loads((source / "EFMI_MergedSkeleton.json").read_text(encoding="utf-8"))
    has_lod = any(component.get("lods") for component in profile["components"])
    build = ROOT / "sandbox" / "build"
    build.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="model-", dir=build))
    project = scratch / "offline-project"
    shutil.copytree(source, project / "workplace")
    # The copy starts without LoD levels, as a fresh extraction would.
    stripped = json.loads(json.dumps(profile))
    for component in stripped["components"]:
        component["lods"] = []
    for key in ("lod_sources", "lod_preview_workspaces", "last_lod_object_name"):
        stripped.pop(key, None)
    (project / "workplace" / "EFMI_MergedSkeleton.json").write_text(
        json.dumps(stripped, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"scratch project: {project}")

    efmi = scratch / "EFMI"
    (efmi / "Mods").mkdir(parents=True)
    server = tools.build_server(efmi, scratch / "out", scratch / "probe.dll", blender_path=args.blender)

    def call(name, **arguments):
        reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
        text = reply["result"]["content"][0]["text"]
        return (json.loads(text) if text.lstrip().startswith("{") else text), reply["result"]["isError"]

    try:
        with patch.object(tools.ProbeClient, "connect", side_effect=ProbeNotFound("offline test")):
            print("== Blender import, diffuse only")
            from efmi_mcp import blender_runner, model_workflow
            imported = model_workflow.import_workspace(model_workflow.Project(project),
                                                       blender_runner.find_blender(args.blender), "diffuse")
            check("the workspace imports and passes its checks", imported["objects"] == profile["component_count"], str(imported))
            report = json.loads((project / "offline-project.verification.json").read_text(encoding="utf-8"))
            links = [(image["name"], image["links"]) for obj in report["objects"] for material in obj["materials"]
                     for image in material["images"] if image["connected_to_output"]]
            check("only diffuse maps are connected", bool(links) and all("DiffuseMap" in name for name, _ in links), str(links[:3]))
            check("each drives Base Color and at most its own alpha", all(
                "Color->BSDF_PRINCIPLED.Base Color" in sockets and set(sockets) <= {
                    "Color->BSDF_PRINCIPLED.Base Color", "Alpha->BSDF_PRINCIPLED.Alpha"} for _, sockets in links))
            check("image paths are relative to the .blend", report.get("image_paths_relative") is True)
            check("every connected diffuse map has a recorded source", all(
                obj["diffuse_source"]["source"] for obj in report["objects"] if obj["diffuse_source"]))

            if has_lod:
                print("== LoD mapping from JSON")
                result, error = call("efmi_map_lod", project_dir=str(project), method="json",
                                     source=str(args.lod_json or source), wait_s=120)
                check("the JSON match job finishes", not error and result.get("status") == "done", str(result)[:400])
                lod = result.get("result", {})
                check("it applied the stored LoD object(s)", bool(lod.get("applied_lod_objects")), str(lod)[:300])
                print("     ", lod.get("applied_lod_objects"), "matched", lod.get("matched_component_counts"),
                      "of", lod.get("component_count"), "| warnings", len(lod.get("warnings", [])))
            else:
                print("== LoD mapping skipped: the source profile has none")

            print("== export with the add-on")
            result, error = call("efmi_export_mod", project_dir=str(project), wait_s=1)
            check("the export starts as a job", not error and result.get("job_id", "").startswith("export-"), str(result)[:300])
            result, error = call("efmi_model_job", action="wait", wait_s=300)
            export = result.get("result", {})
            check("the export finishes and is valid", result.get("status") == "done" and export.get("export_valid") is True,
                  json.dumps(result, ensure_ascii=False)[:800])
            check("it has an INI and no file errors", bool(export.get("ini_files")) and not export.get("errors"), str(export.get("errors")))
            coverage = export.get("entry_point_coverage") or {}
            check("every component has an entry point", coverage.get("missing") == [], str(coverage.get("missing")))
            if has_lod:
                check("LoD entry points were generated", bool(coverage.get("lod")), str(coverage))
            manifest = json.loads(Path(export["manifest"]).read_text(encoding="utf-8"))
            check("the manifest lists every file with its SHA256", manifest["valid"] and all(
                len(item["sha256"]) == 64 for item in manifest["files"]) and len(manifest["files"]) == export["files"])
            check("draw ranges were checked against the buffers", all("max_index" in draw for draw in manifest["draws"]))
            check("the .blend was not modified by the export", manifest["blend"]["sha256"] == __import__(
                "hashlib").sha256((project / "offline-project.blend").read_bytes()).hexdigest())
            print("     ", export["files"], "files,", export["bytes"], "bytes;", len(manifest["draws"]), "draws;",
                  "LoD entry points:", {name: len(items) for name, items in coverage.get("lod", {}).items()})

            if not args.skip_edit:
                print("== an edit made in Blender reaches the export")
                delta = 0.01
                script = scratch / "edit_in_blender.py"
                script.write_text(EDIT_SCRIPT, encoding="utf-8")
                edit = blender_runner.run_script(blender_runner.find_blender(args.blender), script,
                                                 ["--blend", project / "offline-project.blend", "--delta", delta],
                                                 scratch / "edit.log")
                check("Blender moved every mesh and saved the project",
                      edit["exit_code"] == 0 and int(edit["markers"].get("EDITED_MESHES", 0)) > 0,
                      str(blender_runner.tail(scratch / "edit.log", 8)))
                result, error = call("efmi_export_mod", project_dir=str(project), wait_s=300)
                second = result.get("result", {}) if isinstance(result, dict) else {}
                check("the edited project exports and is valid",
                      not error and result.get("status") == "done" and second.get("export_valid") is True,
                      json.dumps(result, ensure_ascii=False)[:800])
                edited = json.loads(Path(second["manifest"]).read_text(encoding="utf-8"))
                check("the export was made from the edited file", edited["blend"]["sha256"] != manifest["blend"]["sha256"])
                old = {item["path"]: item for item in manifest["files"]}
                new = {item["path"]: item for item in edited["files"]}
                check("it has the same files as before the edit", set(old) == set(new),
                      str(sorted(set(old) ^ set(new))[:6]))
                changed = sorted(path for path in new if path in old and new[path]["sha256"] != old[path]["sha256"])
                vertex_files = {draw["vb0_file"]: draw for draw in edited["draws"] if draw.get("vb0_file")}
                index_files = {draw["ib_file"] for draw in edited["draws"] if draw.get("ib_file")}
                check("every vertex buffer that is drawn changed", bool(vertex_files) and set(vertex_files) <= set(changed),
                      str(sorted(set(vertex_files) - set(changed))))
                check("no index buffer changed", not index_files & set(changed), str(sorted(index_files & set(changed))))
                vectors = {}
                for path, draw in sorted(vertex_files.items()):
                    stride = new[path]["bytes"] // draw["vertex_count"] if draw.get("vertex_count") else 0
                    vector, why = moved_by((Path(manifest["mod_dir"]) / path).read_bytes(),
                                           (Path(edited["mod_dir"]) / path).read_bytes(), stride)
                    vectors[path] = vector
                    check(f"all vertices of {Path(path).name} moved alike", vector is not None, why)
                lengths = {round(sum(axis * axis for axis in vector) ** 0.5, 5) for vector in vectors.values() if vector}
                check("by one distance in every buffer, and not zero", len(lengths) == 1 and min(lengths) > 0, str(lengths))
                print("     ", len(changed), "of", len(new), "files changed:",
                      sorted({Path(path).name.rsplit("-", 1)[-1] for path in changed}),
                      "| moved by", next((tuple(round(axis, 5) for axis in v) for v in vectors.values() if v), None),
                      "for an edit of", delta, "along world X", file=console)

            print("== project summary and deployment rules (no game)")
            result, error = call("efmi_model_project", project_dir=str(project))
            check("the project lists its export", not error and result["exports"] and result["exports"][-1]["valid"])
            result, error = call("efmi_verify_exported_mod", project_dir=str(project), action="status")
            check("nothing is deployed yet", not error and result.get("managed") is False, str(result))
            result, error = call("efmi_verify_exported_mod", project_dir=str(project), wait_s=30)
            check("verification without the game fails as a job, before deploying",
                  result.get("status") == "failed" and not (efmi / "Mods" / "EFMI-MCP").exists(), str(result)[:300])
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{'PASS' if not failures else 'FAIL'}: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
