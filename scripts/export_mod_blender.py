"""Export a saved LoyalTools merged-skeleton project as a game mod, in background Blender.

Example (the output directory must be new or empty):
  blender --background --factory-startup --python-exit-code 1 --python SCRIPT -- \
    --blend "E:/Endfield mods/character/character.blend" --output "E:/exports/001/mod" \
    --report "E:/exports/001/export-report.json"

Runs the installed add-on's own "Generate Mod" operator with every step it
performs in the UI; nothing here writes INI or buffer files. The .blend is
opened as saved on disk and is never saved back: the output folder and the
"open folder afterwards" option are changed in memory only.

The operator returns FINISHED even when the export failed. A failure line in
its log fails the export here; whether the output is complete is decided from
the files, by the caller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True

LOG_TEXT = "导出流程日志"   # The add-on's name for the log it leaves in the text editor.


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True, type=Path, help="Saved project to export")
    parser.add_argument("--output", required=True, type=Path, help="New or empty directory for the mod")
    parser.add_argument("--report", required=True, type=Path, help="Where to write the JSON report")
    parser.add_argument("--blueprint", default="", help="Blueprint to export (default: the only merged-skeleton one)")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="Workspace root containing workplace/ (default: stored in the .blend, else next to it)")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(argv)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_object(obj, bones_count, report):
    """What must hold for an edited mesh; counts may differ from the extraction."""
    mesh = obj.data
    names = {group.index: group.name for group in obj.vertex_groups}
    cpu_posed = not bool(obj.get("LoyalTools:EFMIVGCount")) and not names
    bad_coordinates = sum(1 for vertex in mesh.vertices if not all(math.isfinite(v) for v in vertex.co))
    out_of_range = unweighted = 0
    used_vertices = {loop.vertex_index for loop in mesh.loops}
    for vertex in mesh.vertices:
        positive = False
        for assignment in vertex.groups:
            if assignment.weight <= 0:
                continue
            name = names.get(assignment.group, "")
            # Groups with other names are the add-on's to interpret or drop.
            if name.isdecimal():
                positive = True
                if int(name) >= bones_count:
                    out_of_range += 1
        if not positive and vertex.index in used_vertices:
            unweighted += 1
    entry = {
        "name": obj.name, "component_id": obj.get("LoyalTools:EFMIComponentId"),
        "vertices": len(mesh.vertices), "polygons": len(mesh.polygons),
        "uv_layers": [layer.name for layer in mesh.uv_layers], "vertex_groups": len(names),
        "weights_beyond_bones_count": out_of_range, "unweighted_used_vertices": unweighted,
        "modifiers": [modifier.type for modifier in obj.modifiers],
    }
    if bad_coordinates:
        report["errors"].append(f"{obj.name}: {bad_coordinates} vertices have non-finite coordinates")
    if out_of_range:
        report["errors"].append(f"{obj.name}: {out_of_range} weights use vertex groups beyond bones_count {bones_count}")
    if not mesh.polygons:
        report["warnings"].append(f"{obj.name}: no faces; the add-on decides how an empty component is exported")
    if not mesh.uv_layers:
        report["warnings"].append(f"{obj.name}: no UV map")
    if unweighted and not cpu_posed:
        report["warnings"].append(f"{obj.name}: {unweighted} used vertices have no weight in a numbered group")
    return entry


def run(args, report):
    import bpy

    if not bpy.app.background:
        raise RuntimeError("Run with --background")
    blend = args.blend.resolve()
    if not blend.is_file():
        raise FileNotFoundError(blend)
    output = args.output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    report["blend_sha256"] = sha256(blend)
    report["blend_modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(blend.stat().st_mtime))

    result = bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    if result != {"FINISHED"}:
        raise RuntimeError(f"Could not open {blend}: {result}")
    bpy.ops.preferences.addon_enable(module="LoyalTools")
    import LoyalTools
    from LoyalTools.common.efmi_merged_skeleton import load_profile
    from LoyalTools.common.global_config import GlobalConfig
    from LoyalTools.common.logic_name import LogicName

    report.update(blender_version=bpy.app.version_string, addon_path=LoyalTools.__file__,
                  addon_version=list(LoyalTools.bl_info.get("version")))
    props = bpy.context.scene.global_properties

    # Where the workspace is: as asked, else as stored if it still exists, else
    # next to the .blend (a project folder that was moved or renamed).
    stored = Path(props.custom_workspace_folder_path) if props.custom_workspace_folder_path else None
    candidates = [("argument", args.workspace), ("stored in the .blend", stored), ("next to the .blend", blend.parent)]
    root = source = None
    for source, candidate in candidates:
        if candidate and (Path(candidate) / "workplace" / "EFMI_MergedSkeleton.json").is_file():
            root = Path(candidate).resolve()
            break
    if root is None:
        raise ValueError("No workspace with workplace/EFMI_MergedSkeleton.json was found for this project")
    report["workspace"] = str(root)
    report["workspace_found"] = source
    profile_path = root / "workplace" / "EFMI_MergedSkeleton.json"
    profile = load_profile(str(root / "workplace"), required=True)
    report["profile_sha256"] = sha256(profile_path)
    report["profile"] = {"object_name": profile.get("object_name", ""), "components": profile["component_count"],
                         "bones_count": profile["bones_count"], "max_lod_count": profile["max_lod_count"],
                         "lod_objects": profile["lod_object_names"]}

    trees = [tree for tree in bpy.data.node_groups if tree.bl_idname == "SSMTBlueprintTreeType"]
    merged = [tree for tree in trees if tree.get("LoyalTools:EFMIMergedSkeleton")]
    report["blueprints"] = [{"name": tree.name, "merged_skeleton": tree in merged} for tree in trees]
    if args.blueprint:
        tree = next((tree for tree in trees if tree.name == args.blueprint), None)
        if tree is None:
            raise ValueError(f"Blueprint {args.blueprint!r} is not in this file; it has {[t.name for t in trees]}")
    elif len(merged) == 1:
        tree = merged[0]
    else:
        raise ValueError(f"Name the blueprint to export; merged-skeleton blueprints here: {[t.name for t in merged]}")
    report["blueprint"] = tree.name

    referenced = sorted({node.object_name for node in tree.nodes
                         if node.bl_idname == "SSMTNode_Object_Info" and not node.mute and node.object_name})
    report["objects"] = []
    for name in referenced:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            report["errors"].append(f"Blueprint references {name!r}, which is not a mesh object in this file")
            continue
        report["objects"].append(check_object(obj, profile["bones_count"], report))
    if not referenced:
        report["errors"].append("The blueprint references no objects")
    if report["errors"]:
        raise RuntimeError(f"The project is not exportable: {len(report['errors'])} problem(s)")

    # In memory only; the file on disk keeps the user's settings.
    report["stored_settings"] = {name: getattr(props, name) for name in (
        "workspace_source_mode", "custom_workspace_folder_path", "use_specific_generate_mod_folder_path",
        "generate_mod_folder_path", "force_standalone_preset", "standalone_game_preset")}
    props.workspace_source_mode = "CUSTOM"
    props.custom_workspace_folder_path = str(root)
    props.use_specific_generate_mod_folder_path = True
    props.generate_mod_folder_path = str(output)
    props.open_mod_folder_after_generate_mod = False
    GlobalConfig.read_from_main_json_ssmt4()
    if Path(GlobalConfig.path_workspace_folder()).resolve() != root / "workplace":
        raise ValueError("The add-on resolved a different workspace: " + str(GlobalConfig.path_workspace_folder()))
    if Path(GlobalConfig.path_generate_mod_folder()).resolve() != output:
        raise ValueError("The add-on resolved a different output folder: " + str(GlobalConfig.path_generate_mod_folder()))
    if GlobalConfig.logic_name != LogicName.EFMI:
        raise ValueError(f"The project's game preset is {GlobalConfig.logic_name!r}, not EFMI")

    started = time.perf_counter()
    result = bpy.ops.ssmt.generate_mod_blueprint(blueprint_name=tree.name)
    report["export_seconds"] = round(time.perf_counter() - started, 2)
    report["operator_result"] = sorted(result)

    log = bpy.data.texts.get(LOG_TEXT)
    text = log.as_string() if log else ""
    args.report.with_suffix(".log.txt").write_text(text, encoding="utf-8")
    lines = text.splitlines()
    report["export_log"] = str(args.report.with_suffix(".log.txt"))
    # The saved log has its emoji stripped; these are the add-on's own phrases.
    report["log_failures"] = [line.strip() for line in lines if "Mod生成失败" in line or "❌" in line]
    report["log_warnings"] = [line.strip() for line in lines if "⚠" in line or "警告" in line or "WARNING" in line.upper()][:60]
    completed = any(marker in line for line in lines for marker in ("Mod生成完成", "导出完成!", "直出完成"))

    files = sorted(path for path in output.rglob("*") if path.is_file())
    report["files"] = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size} for path in files]
    report["ini_files"] = [item["path"] for item in report["files"] if item["path"].lower().endswith(".ini")]
    if result != {"FINISHED"}:
        report["errors"].append(f"The export operator returned {sorted(result)}")
    if report["log_failures"]:
        report["errors"].append("The add-on's export log reports a failure: " + "; ".join(report["log_failures"][:3]))
    elif not completed:
        # A reworded log must not turn a good export into a failure: the files
        # are checked separately, after this script.
        report["warnings"].append("The add-on's log has no completion line this script recognises; "
                                  "the export is judged by the files it produced")
    if not report["ini_files"]:
        report["errors"].append("No INI file was produced")
    if sha256(profile_path) != report["profile_sha256"]:
        report["warnings"].append("The export changed EFMI_MergedSkeleton.json")
    if sha256(blend) != report["blend_sha256"]:
        report["errors"].append("The .blend on disk changed during the export")
    if report["errors"]:
        raise RuntimeError(f"Export failed: {len(report['errors'])} problem(s)")
    report["status"] = "passed"


def main():
    args = arguments()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "blend": str(args.blend.resolve()), "output": str(args.output.resolve()),
              "errors": [], "warnings": []}
    try:
        run(args, report)
    except Exception:
        report["exception"] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("EXPORT_MOD_REPORT=" + str(args.report), flush=True)
        print("EXPORT_MOD_STATUS=" + report["status"], flush=True)


if __name__ == "__main__":
    main()
