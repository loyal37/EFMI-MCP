"""Extract a Zenless Zone Zero character from a frame analysis folder with LoyalTools, in background Blender.

Example (the output .blend and its .verification.json must not exist):
  blender --background --factory-startup --python-exit-code 1 --python SCRIPT -- \
    --dump "D:/XXMI/ZZMI/FrameAnalysis-..." --workspace "E:/ZZZ mods/character" \
    --output "E:/ZZZ mods/character/character.blend"

Runs the installed add-on's own "extract from dump" operator with the ZZMI game
preset: by default its merged-skeleton workflow (the character is found in the
frame by the bones its parts share), or with --mode skinned every skinned mesh of
the frame as it is. The add-on writes the workspace into <workspace>/workplace,
imports it and wires the blueprint; this script checks what that left as files
and objects, and saves a .blend only after the checks pass. The frame analysis
folder is only read. Preferences are not saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
import sys
import traceback

# Enabling the installed add-on must not create or refresh its __pycache__.
sys.dont_write_bytecode = True


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True, type=Path, help="Frame analysis folder with log.txt (read only)")
    parser.add_argument("--workspace", required=True, type=Path, help="Project folder; the add-on writes workplace/ in it")
    parser.add_argument("--output", required=True, type=Path, help="New .blend path")
    parser.add_argument("--mode", choices=("merged", "skinned"), default="merged",
                        help="merged: the add-on's merged-skeleton workflow; skinned: every skinned mesh as it is")
    parser.add_argument("--materials", choices=("diffuse", "native"), default="diffuse",
                        help="diffuse: without the normal map; native: the add-on's setup with the normal map")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(argv)


def check(report, condition, message):
    if not condition:
        report["errors"].append(message)


def stride(entries) -> int:
    return sum(int(element.get("ByteWidth", 0)) for element in entries)


def part_report(bpy, obj, submesh: dict, folder: Path, report: dict) -> dict:
    """An imported part against the files the add-on wrote for it."""
    buffers = {}
    for entry in submesh.get("CategoryBufferList", []):
        path = folder / entry["FileName"]
        width = stride(entry.get("D3D11ElementList", []))
        size = path.stat().st_size if path.is_file() else -1
        check(report, size >= 0, f"{obj.name}: missing {entry['FileName']}")
        if size >= 0 and width:
            check(report, size % width == 0, f"{obj.name}: {entry['FileName']} is not a whole number of {width}-byte rows")
        buffers[entry["FileName"]] = {"bytes": size, "stride": width, "rows": size // width if width and size >= 0 else None}
    indices = None
    distinct = None         # triangles Blender can hold: one face per set of three vertices
    for entry in submesh.get("IndexBufferList", []):
        path = folder / entry["FileName"]
        width = 2 if entry.get("DXGI_FORMAT") == "R16_UINT" else 4
        check(report, path.is_file(), f"{obj.name}: missing {entry['FileName']}")
        if path.is_file():
            data = path.read_bytes()
            indices = len(data) // width
            values = struct.unpack(f"<{indices}{'H' if width == 2 else 'I'}", data[:indices * width])
            corners = [values[n:n + 3] for n in range(0, indices - indices % 3, 3)]
            distinct = len({frozenset(t) for t in corners if len(set(t)) == 3})
    mesh = obj.data
    triangles = sum(len(polygon.vertices) - 2 for polygon in mesh.polygons)
    check(report, len(mesh.vertices) > 0 and triangles > 0, f"{obj.name}: the imported mesh is empty")
    if indices is not None:
        check(report, triangles in (indices // 3, distinct),
              f"{obj.name}: {triangles} triangles imported, the index file has {indices // 3} ({distinct} distinct)")
        if triangles == distinct != indices // 3:
            # Blender keeps one face per set of vertices: a face drawn twice (both sides) or a degenerate one is
            # dropped on import, and the export will not have it.
            report["warnings"].append(f"{obj.name}: {indices // 3 - triangles} of {indices // 3} triangles repeat the "
                                      "vertices of another or have two equal corners; Blender keeps one face per set "
                                      "of vertices, so the export will not draw them")
    return {"name": obj.name, "vertices": len(mesh.vertices), "triangles": triangles, "index_count": indices,
            "vertex_groups": len(obj.vertex_groups), "buffers": buffers,
            "materials": [slot.material.name for slot in obj.material_slots if slot.material],
            "images": sorted({node.image.filepath for slot in obj.material_slots if slot.material and slot.material.node_tree
                              for node in slot.material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image})}


def run(args, report):
    import bpy

    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Run in background with --factory-startup, without opening an existing .blend")
    dump = args.dump.resolve()
    if not (dump / "log.txt").is_file():
        raise ValueError(f"{dump} is not a frame analysis folder (no log.txt)")
    bpy.ops.preferences.addon_enable(module="LoyalTools")
    import LoyalTools
    from LoyalTools.common import zzmi_merged_skeleton as zms
    from LoyalTools.common.global_config import GlobalConfig
    from LoyalTools.common.logic_name import LogicName

    root = args.workspace.resolve()
    data_root = root / "workplace"
    if data_root.exists() and any(data_root.iterdir()):
        raise FileExistsError(f"{data_root} is not empty; extraction always starts a new workspace")
    props = bpy.context.scene.global_properties
    props.workspace_source_mode = "CUSTOM"
    props.custom_workspace_folder_path = str(root)
    props.force_standalone_preset = True
    props.standalone_game_preset = LogicName.ZZMI
    props.use_normal_map = args.materials == "native"
    extract = bpy.context.scene.loyal_extract_props
    extract.workflow_mode = "MERGED_SKELETON" if args.mode == "merged" else "STANDARD"
    extract.frame_dump_folder = str(dump)
    GlobalConfig.read_from_main_json_ssmt4()
    if GlobalConfig.logic_name != LogicName.ZZMI:
        raise ValueError(f"LoyalTools did not take the ZZMI preset ({GlobalConfig.logic_name!r})")
    if Path(GlobalConfig.path_workspace_folder()).resolve() != data_root:
        raise ValueError("LoyalTools resolved a different workspace data directory")
    # Remove factory defaults only from this unsaved background scene.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    before = set(bpy.data.node_groups)
    dump_files = sum(1 for _ in dump.iterdir())
    result = bpy.ops.loyal.extract_from_dump() if args.mode == "merged" else bpy.ops.loyal.extract_zzmi_whole_frame()
    report["operator_result"] = sorted(result)
    report["addon_report"] = extract.last_report
    check(report, result == {"FINISHED"}, f"The extract operator returned {sorted(result)}")
    check(report, sum(1 for _ in dump.iterdir()) == dump_files, "The frame analysis folder changed during extraction")

    import_path = data_root / "Import.json"
    if not import_path.is_file():
        raise RuntimeError("The add-on wrote no Import.json: nothing was extracted (see addon_report)")
    import_map = json.loads(import_path.read_text(encoding="utf-8-sig"))
    profile = None
    if args.mode == "merged":
        profile = zms.load_profile(str(data_root), required=True)
        report["profile"] = {"bones_count": profile["bones_count"], "components": [
            {"ib_hash": c["ib_hash"], "role": c["role"], "unique_strs": c["unique_strs"]} for c in profile["components"]]}
    roles = {u: c["role"] for c in (profile or {}).get("components", []) for u in c["unique_strs"]}
    objects = {obj.name: obj for obj in bpy.context.scene.objects if obj.type == "MESH"}
    check(report, set(objects) == set(import_map),
          f"Imported objects {sorted(set(objects) ^ set(import_map))[:6]} differ from Import.json")
    for unique_str, gametype in sorted(import_map.items()):
        folder = data_root / unique_str / ("TYPE_" + gametype)
        path = folder / (unique_str + ".json")
        if not path.is_file():
            report["errors"].append(f"Missing submesh JSON {path}")
            continue
        submesh = json.loads(path.read_text(encoding="utf-8-sig"))
        check(report, submesh.get("GamePreset") == LogicName.ZZMI, f"{unique_str}: game preset {submesh.get('GamePreset')!r}")
        obj = objects.get(unique_str)
        if obj is None:
            continue
        item = part_report(bpy, obj, submesh, folder, report)
        item["role"] = roles.get(unique_str)
        if item["role"] == zms.ROLE_MERGED:
            # Merged parts share one set of vertex groups, the size of the merged skeleton.
            check(report, item["vertex_groups"] == profile["bones_count"],
                  f"{unique_str}: {item['vertex_groups']} vertex groups, the merged skeleton has {profile['bones_count']}")
        report["objects"].append(item)
    blueprints = [tree for tree in bpy.data.node_groups if tree not in before and tree.bl_idname == "SSMTBlueprintTreeType"]
    check(report, len(blueprints) == 1, f"Expected one new blueprint; found {len(blueprints)}")
    for tree in blueprints:
        if args.mode == "merged":
            check(report, bool(tree.get(zms.TREE_MARKER_MERGED_SKELETON)), "Blueprint missing the merged-skeleton marker")
        linked = {node.object_name for node in tree.nodes if node.bl_idname == "SSMTNode_Object_Info"}
        check(report, linked == set(objects), "Blueprint object membership differs from the imported objects")
    report.update(blender_version=bpy.app.version_string, addon_path=LoyalTools.__file__,
                  addon_version=LoyalTools.bl_info.get("version"), blueprints=[tree.name for tree in blueprints],
                  submeshes=len(import_map), total_vertices=sum(item["vertices"] for item in report["objects"]),
                  total_triangles=sum(item["triangles"] for item in report["objects"]))
    if report["errors"]:
        raise RuntimeError(f"Extraction check failed with {len(report['errors'])} error(s)")
    if args.output.exists():
        raise FileExistsError(args.output)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=True)
    if result != {"FINISHED"} or not args.output.is_file():
        raise RuntimeError(f"Saving .blend failed: {result}")
    # Image paths relative to the .blend survive moving the project folder.
    bpy.ops.file.make_paths_relative()
    absolute = [image.filepath for image in bpy.data.images if image.filepath and not image.filepath.startswith("//")]
    if absolute:
        report["warnings"].append(f"{len(absolute)} image path(s) could not be made relative, e.g. {absolute[0]}")
    result = bpy.ops.wm.save_mainfile()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Saving .blend failed: {result}")
    report["image_paths_relative"] = not absolute
    report["saved_blend"] = str(args.output)
    report["blend_sha256"] = hashlib.sha256(args.output.read_bytes()).hexdigest()
    report["status"] = "passed"


def main():
    args = arguments()
    args.output = args.output.resolve()
    if args.output.suffix.lower() != ".blend":
        raise ValueError("--output must end in .blend")
    report_path = args.output.with_suffix(".verification.json")
    if args.output.exists() or report_path.exists():
        raise FileExistsError("Choose a new output name: .blend or verification report already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "game": "ZZMI", "mode": args.mode, "dump": str(args.dump.resolve()),
              "workspace": str(args.workspace.resolve()), "output": str(args.output),
              "materials_mode": args.materials, "objects": [], "errors": [], "warnings": []}
    try:
        run(args, report)
    except Exception:
        report["exception"] = traceback.format_exc()
        raise
    finally:
        with report_path.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        print("ZZMI_EXTRACT_REPORT=" + str(report_path), flush=True)
        print("ZZMI_EXTRACT_STATUS=" + report["status"], flush=True)


if __name__ == "__main__":
    main()
