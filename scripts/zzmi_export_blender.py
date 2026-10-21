"""Export a saved LoyalTools Zenless Zone Zero project as a game mod, in background Blender.

Example (the output directory must be new or empty):
  blender --background --factory-startup --python-exit-code 1 --python SCRIPT -- \
    --blend "E:/ZZZ mods/character/character.blend" --output "E:/exports/001/mod" \
    --report "E:/exports/001/export-report.json"

Runs the installed add-on's own "Generate Mod" operator with the ZZMI preset;
nothing here writes INI or buffer files. The .blend is opened as saved on disk
and never saved back: the output folder and the "open folder afterwards" option
are changed in memory only. Whether the export is complete is decided from the
files it produced (here and by the caller), not from the wording of the add-on's
log, which is only kept for diagnosis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New or empty folder for the mod")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=None, help="Project folder containing workplace/")
    parser.add_argument("--blueprint", default="", help="Blueprint to export (default: the only one)")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(argv)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    from LoyalTools.common import zzmi_merged_skeleton as zms
    from LoyalTools.common.global_config import GlobalConfig
    from LoyalTools.common.logic_name import LogicName

    report.update(blender_version=bpy.app.version_string, addon_path=LoyalTools.__file__,
                  addon_version=list(LoyalTools.bl_info.get("version")))
    props = bpy.context.scene.global_properties

    # Where the workspace is: as asked, else as stored if it still exists, else next to the .blend.
    stored = Path(props.custom_workspace_folder_path) if props.custom_workspace_folder_path else None
    root = source = None
    for source, candidate in (("argument", args.workspace), ("stored in the .blend", stored),
                              ("next to the .blend", blend.parent)):
        if candidate and (Path(candidate) / "workplace" / "Import.json").is_file():
            root = Path(candidate).resolve()
            break
    if root is None:
        raise ValueError("No workspace with workplace/Import.json was found for this project")
    report["workspace"] = str(root)
    report["workspace_found"] = source
    profile_path = root / "workplace" / "ZZMI_MergedSkeleton.json"
    report["profile_sha256"] = sha256(profile_path) if profile_path.is_file() else None

    trees = [tree for tree in bpy.data.node_groups if tree.bl_idname == "SSMTBlueprintTreeType"]
    report["blueprints"] = [{"name": tree.name, "merged_skeleton": bool(tree.get(zms.TREE_MARKER_MERGED_SKELETON))}
                            for tree in trees]
    if args.blueprint:
        tree = next((tree for tree in trees if tree.name == args.blueprint), None)
        if tree is None:
            raise ValueError(f"Blueprint {args.blueprint!r} is not in this file; it has {[t.name for t in trees]}")
    elif len(trees) == 1:
        tree = trees[0]
    else:
        raise ValueError(f"Name the blueprint to export; this file has {[t.name for t in trees]}")
    report["blueprint"] = tree.name
    referenced = sorted({node.object_name for node in tree.nodes
                         if node.bl_idname == "SSMTNode_Object_Info" and not node.mute and node.object_name})
    report["objects"] = []
    for name in referenced:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            report["errors"].append(f"Blueprint references {name!r}, which is not a mesh object in this file")
            continue
        mesh = obj.data
        report["objects"].append({"name": name, "vertices": len(mesh.vertices), "polygons": len(mesh.polygons),
                                  "vertex_groups": len(obj.vertex_groups), "role": obj.get(zms.OBJECT_MARKER_ROLE)})
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
    props.force_standalone_preset = True
    props.standalone_game_preset = LogicName.ZZMI
    props.use_specific_generate_mod_folder_path = True
    props.generate_mod_folder_path = str(output)
    props.open_mod_folder_after_generate_mod = False
    GlobalConfig.read_from_main_json_ssmt4()
    if GlobalConfig.logic_name != LogicName.ZZMI:
        raise ValueError(f"The add-on did not take the ZZMI preset ({GlobalConfig.logic_name!r})")
    if Path(GlobalConfig.path_workspace_folder()).resolve() != root / "workplace":
        raise ValueError("The add-on resolved a different workspace: " + str(GlobalConfig.path_workspace_folder()))
    if Path(GlobalConfig.path_generate_mod_folder()).resolve() != output:
        raise ValueError("The add-on resolved a different output folder: " + str(GlobalConfig.path_generate_mod_folder()))

    texts_before = set(bpy.data.texts)
    started = time.perf_counter()
    result = bpy.ops.ssmt.generate_mod_blueprint(blueprint_name=tree.name)
    report["export_seconds"] = round(time.perf_counter() - started, 2)
    report["operator_result"] = sorted(result)
    # Whatever log the add-on left in the text editor, kept for diagnosis only.
    logs = [text for text in bpy.data.texts if text not in texts_before or text.is_modified]
    log_path = args.report.with_suffix(".log.txt")
    log_path.write_text("\n\n".join(text.as_string() for text in logs), encoding="utf-8")
    report["export_log"] = str(log_path)

    files = sorted(path for path in output.rglob("*") if path.is_file())
    report["files"] = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size} for path in files]
    report["ini_files"] = [item["path"] for item in report["files"] if item["path"].lower().endswith(".ini")]
    if result != {"FINISHED"}:
        report["errors"].append(f"The export operator returned {sorted(result)}")
    if not report["ini_files"]:
        report["errors"].append("No INI file was produced")
    if profile_path.is_file() and sha256(profile_path) != report["profile_sha256"]:
        report["warnings"].append("The export changed ZZMI_MergedSkeleton.json")
    if sha256(blend) != report["blend_sha256"]:
        report["errors"].append("The .blend on disk changed during the export")
    if report["errors"]:
        raise RuntimeError(f"Export failed: {len(report['errors'])} problem(s)")
    report["status"] = "passed"


def main():
    args = arguments()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "game": "ZZMI", "blend": str(args.blend.resolve()), "output": str(args.output.resolve()),
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
