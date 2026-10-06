"""Import a LoyalTools merged workspace in background Blender and verify it.

Example (the output .blend and its .verification.json must not exist):
  blender --background --factory-startup --python-exit-code 1 --python SCRIPT -- \
    --workspace "E:/Endfield mods/character" --output "E:/Endfield mods/character/character.blend"

Uses the installed LoyalTools add-on. Does not extract data, export a mod, save
preferences, or change workspace inputs. Saves a .blend only after validation.

Materials: by default only the component's diffuse map reaches the material
output (Base Color, and its alpha as the add-on connects it). The other game
textures stay in the workspace for the mod export; --materials native restores
the add-on's own setup with the normal map.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import traceback

# Enabling the installed add-on must not create or refresh its __pycache__.
sys.dont_write_bytecode = True


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path,
                        help="Custom workspace root containing workplace/")
    parser.add_argument("--output", required=True, type=Path, help="New .blend path")
    parser.add_argument("--materials", choices=("diffuse", "native"), default="diffuse",
                        help="diffuse: only the diffuse map is connected; native: the add-on's setup with normal map")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(argv)


def check(report, condition, message):
    if not condition:
        report["errors"].append(message)


def texture_source(alias: Path) -> dict:
    """Which captured texture a workspace alias such as -DiffuseMap.dds is a copy of."""
    info = {"alias": str(alias), "sha256": hashlib.sha256(alias.read_bytes()).hexdigest(), "source": None, "slots": []}
    for candidate in sorted(alias.parent.glob("t-*.dds")):
        if candidate.stat().st_size == alias.stat().st_size and \
                hashlib.sha256(candidate.read_bytes()).hexdigest() == info["sha256"]:
            info["source"] = candidate.name
            break
    slots_path = alias.parent / "TextureSlots.json"
    if info["source"] and slots_path.is_file():
        slots = json.loads(slots_path.read_text(encoding="utf-8-sig")).get("slots", {})
        for slot, entries in sorted(slots.items()):
            for entry in entries:
                if entry.get("filename") == info["source"]:
                    info["slots"].append({"slot": slot, "hash": entry.get("hash"), "format": entry.get("format"),
                                          "draw": entry.get("call_id"), "width": entry.get("width"),
                                          "height": entry.get("height")})
    return info


def material_report(bpy, obj, expected_paths, report, diffuse_only=False):
    materials = []
    linked_paths = set()
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        entry = {"name": mat.name, "use_nodes": mat.use_nodes, "images": []}
        materials.append(entry)
        if not mat.use_nodes or mat.node_tree is None:
            continue
        tree = mat.node_tree
        reachable = {node for node in tree.nodes
                     if node.type == "OUTPUT_MATERIAL" and node.is_active_output}
        pending = list(reachable)
        while pending:
            node = pending.pop()
            for link in tree.links:
                if link.is_valid and link.to_node == node and link.from_node not in reachable:
                    reachable.add(link.from_node)
                    pending.append(link.from_node)
        for node in tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            image = node.image
            if image is None:
                check(report, node not in reachable, f"{obj.name}: connected image node has no image")
                continue
            resolved = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
            packed = bool(image.packed_file or image.packed_files)
            exists = resolved.is_file() or packed or image.source == "GENERATED"
            linked = node in reachable
            sockets = sorted({f"{link.from_socket.name}->{link.to_node.type}.{link.to_socket.name}"
                              for link in tree.links
                              if link.is_valid and link.from_node == node and link.to_node in reachable})
            entry["images"].append({"name": image.name, "path": str(resolved),
                                    "exists_or_packed": exists, "packed": packed,
                                    "size": list(image.size), "connected_to_output": linked,
                                    "links": sockets})
            if linked and diffuse_only:
                # The option that skips the normal map is not trusted on its own:
                # what counts is which image actually feeds the surface.
                allowed = {Path(path).resolve() for path in expected_paths}
                check(report, resolved in allowed,
                      f"{obj.name}: a texture other than the diffuse map is connected: {resolved}")
                check(report, "Color->BSDF_PRINCIPLED.Base Color" in sockets,
                      f"{obj.name}: the diffuse map does not drive Base Color")
                check(report, set(sockets) <= {"Color->BSDF_PRINCIPLED.Base Color", "Alpha->BSDF_PRINCIPLED.Alpha"},
                      f"{obj.name}: unexpected diffuse connections {sockets}")
            check(report, exists, f"{obj.name}: missing image {resolved}")
            check(report, all(value > 0 for value in image.size),
                  f"{obj.name}: image could not be decoded: {resolved}")
            if linked:
                linked_paths.add(resolved)
    for expected in expected_paths:
        check(report, Path(expected).resolve() in linked_paths,
              f"{obj.name}: expected material image is not linked to output: {expected}")
    return materials


def frame_viewports(bpy, objects):
    """Save a useful viewport without moving or scaling any imported object."""
    from mathutils import Euler, Vector

    # Solid texture display uses the active image node, which would otherwise
    # become the normal map when optional normal-material setup runs last.
    for obj in objects:
        for material in obj.data.materials:
            if material and material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image and "-DiffuseMap." in node.image.filepath:
                        material.node_tree.nodes.active = node
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    if not corners:
        return {"viewports": 0}
    minimum = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
    center = (minimum + maximum) * 0.5
    diameter = max((maximum - minimum).length, 0.001)
    rotation = Euler((math.radians(90), 0.0, 0.0), "XYZ").to_quaternion()
    updated = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.lens = 50
            space.clip_start = max(diameter / 10000, 0.00001)
            space.clip_end = max(diameter * 100, 100)
            space.shading.type = "SOLID"
            space.shading.color_type = "TEXTURE"
            # A tall character also fits narrow viewports. Background screen
            # sizes are sometimes zero, in which case use a square viewport.
            aspect = area.width / area.height if area.width and area.height else 1.0
            distance = diameter * 2.2 / min(max(aspect, 0.1), 1.0)
            regions = list(space.region_quadviews) or [space.region_3d]
            for region in regions:
                if region is None:
                    continue
                region.view_location = center
                region.view_rotation = rotation
                region.view_distance = distance
                region.view_perspective = "PERSP"
                updated += 1
    return {"viewports": updated, "world_bounds_min": list(minimum),
            "world_bounds_max": list(maximum), "center": list(center),
            "bounding_diagonal": diameter, "shading": "SOLID/TEXTURE"}


def verify_object(bpy, obj, component, profile, json_path, report):
    from LoyalTools.common.mesh_create_helper import MeshCreateHelper

    mesh = obj.data
    bones = profile["bones_count"]
    cpu = component["cpu_posed"]
    expected_markers = {
        "LoyalTools:EFMIMergedSkeleton": True,
        "LoyalTools:EFMIComponentId": component["component_id"],
        "LoyalTools:EFMIVGOffset": component["vg_offset"],
        "LoyalTools:EFMIVGCount": component["vg_count"],
        "LoyalTools:EFMIBonesCount": bones,
        "LoyalTools:EFMIProfile": "EFMI_MergedSkeleton.json",
    }
    markers = {key: obj.get(key) for key in expected_markers}
    for key, value in expected_markers.items():
        check(report, markers[key] == value, f"{obj.name}: incorrect marker {key}")
    check(report, len(mesh.vertices) == component["vertex_count"],
          f"{obj.name}: vertex count differs from profile")
    check(report, len(mesh.polygons) * 3 == component["index_count"],
          f"{obj.name}: triangle count differs from profile")
    check(report, all(len(poly.vertices) == 3 for poly in mesh.polygons),
          f"{obj.name}: imported mesh contains non-triangle polygons")
    check(report, all(math.isfinite(value) for vertex in mesh.vertices for value in vertex.co),
          f"{obj.name}: non-finite vertex coordinates")

    names = {group.index: group.name for group in obj.vertex_groups}
    if not cpu:
        check(report, set(names.values()) == {str(i) for i in range(bones)},
              f"{obj.name}: global vertex groups do not match profile bones_count")
    allowed = set(component["vg_map"].values())
    used_vertices = {loop.vertex_index for loop in mesh.loops}
    bad_weights = bad_groups = unweighted_used = unweighted_unused = 0
    active_groups = set()
    positive_count = 0
    min_weight, max_weight = math.inf, -math.inf
    min_sum, max_sum = math.inf, -math.inf
    for vertex in mesh.vertices:
        total = 0.0
        for assignment in vertex.groups:
            weight = float(assignment.weight)
            if not math.isfinite(weight) or not 0.0 <= weight <= 1.000001:
                bad_weights += 1
                continue
            if weight <= 0:
                continue
            positive_count += 1
            total += weight
            min_weight, max_weight = min(min_weight, weight), max(max_weight, weight)
            name = names.get(assignment.group, "")
            if not name.isdecimal():
                bad_groups += 1
            else:
                group_id = int(name)
                active_groups.add(group_id)
                if not cpu and (not 0 <= group_id < bones or group_id not in allowed):
                    bad_groups += 1
        min_sum, max_sum = min(min_sum, total), max(max_sum, total)
        if total <= 0:
            if vertex.index in used_vertices:
                unweighted_used += 1
            else:
                unweighted_unused += 1
    check(report, bad_weights == 0, f"{obj.name}: {bad_weights} invalid weights")
    check(report, bad_groups == 0, f"{obj.name}: {bad_groups} invalid weighted group references")
    if not cpu:
        check(report, unweighted_used == 0,
              f"{obj.name}: {unweighted_used} referenced GPU vertices have no positive weight")
    diffuse_path, normal_path = MeshCreateHelper.get_import_texture_paths(obj.name, str(json_path.parent))
    diffuse_only = report["materials_mode"] == "diffuse"
    expected_textures = [path for path in ((diffuse_path,) if diffuse_only else (diffuse_path, normal_path)) if path]
    if not diffuse_path:
        report["warnings"].append(f"{obj.name}: workspace has no diffuse map alias; no texture is connected")
    return {
        "name": obj.name, "component_id": component["component_id"], "cpu_posed": cpu,
        "vertices": len(mesh.vertices), "triangles": len(mesh.polygons),
        "vertex_groups": len(names), "markers": markers,
        "weights": {"positive_assignments": positive_count, "invalid_weights": bad_weights,
                    "invalid_group_references": bad_groups,
                    "positive_weight_range": [min_weight, max_weight] if positive_count else None,
                    "vertex_weight_sum_range": [min_sum, max_sum] if mesh.vertices else None,
                    "active_group_ids": sorted(active_groups),
                    "unweighted_used_vertices": unweighted_used,
                    "unweighted_unused_vertices": unweighted_unused},
        "materials": material_report(bpy, obj, expected_textures, report, diffuse_only),
        # Where the connected diffuse map comes from, so a wrong guess by the
        # add-on's alias heuristic can be spotted and overridden.
        "diffuse_source": texture_source(Path(diffuse_path)) if diffuse_path else None,
    }


def run(args, report):
    import bpy

    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Run in background with --factory-startup, without opening an existing .blend")
    bpy.ops.preferences.addon_enable(module="LoyalTools")
    import LoyalTools
    from LoyalTools.common.efmi_merged_skeleton import load_profile, parse_submesh_metadata
    from LoyalTools.common.global_config import GlobalConfig
    from LoyalTools.common.logic_name import LogicName

    root = args.workspace.resolve()
    data_root = root / "workplace"
    profile = load_profile(str(data_root), required=True)
    profile_path = data_root / "EFMI_MergedSkeleton.json"
    profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    import_map = json.loads((data_root / "Import.json").read_text(encoding="utf-8-sig"))
    paths = {}
    for component in profile["components"]:
        name = component["unique_str"]
        if name not in import_map:
            raise ValueError(f"Profile component absent from Import.json: {name}")
        path = data_root / name / ("TYPE_" + import_map[name]) / (name + ".json")
        metadata = parse_submesh_metadata(json.loads(path.read_text(encoding="utf-8-sig")))
        if metadata is None:
            raise ValueError(f"Missing merged-skeleton submesh metadata: {path}")
        for key in ("component_id", "cpu_posed", "vg_offset", "vg_count", "vg_map"):
            if metadata[key] != component[key]:
                raise ValueError(f"Submesh/profile {key} mismatch: {name}")
        paths[name] = path
    props = bpy.context.scene.global_properties
    props.workspace_source_mode = "CUSTOM"
    props.custom_workspace_folder_path = str(root)
    props.force_standalone_preset = True
    props.standalone_game_preset = LogicName.EFMI
    props.use_normal_map = args.materials == "native"
    bpy.context.scene.loyal_extract_props.workflow_mode = "MERGED_SKELETON"
    GlobalConfig.read_from_main_json_ssmt4()
    if Path(GlobalConfig.path_workspace_folder()).resolve() != data_root:
        raise ValueError("LoyalTools resolved a different workspace data directory")
    # Remove factory defaults only from this unsaved background scene.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    before = set(bpy.data.node_groups)
    result = bpy.ops.loyal.import_workspace()
    check(report, result == {"FINISHED"}, f"Import operator returned {sorted(result)}")
    objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    by_component = {}
    for obj in objects:
        by_component.setdefault(obj.get("LoyalTools:EFMIComponentId"), []).append(obj)
    check(report, len(objects) == profile["component_count"], "Imported object count differs from profile")
    for component in profile["components"]:
        found = by_component.get(component["component_id"], [])
        check(report, len(found) == 1, f"Component {component['component_id']} has {len(found)} objects")
        if len(found) == 1:
            obj = found[0]
            check(report, obj.name == component["unique_str"], f"Component name mismatch: {obj.name}")
            report["objects"].append(verify_object(
                bpy, obj, component, profile, paths[component["unique_str"]], report))
    blueprints = [tree for tree in bpy.data.node_groups if tree not in before
                  and tree.bl_idname == "SSMTBlueprintTreeType"]
    check(report, len(blueprints) == 1, f"Expected one new blueprint; found {len(blueprints)}")
    for tree in blueprints:
        check(report, bool(tree.get("LoyalTools:EFMIMergedSkeleton")), "Blueprint missing merged marker")
        linked_objects = {node.object_name for node in tree.nodes
                          if node.bl_idname == "SSMTNode_Object_Info"}
        check(report, linked_objects == {obj.name for obj in objects}, "Blueprint object membership mismatch")
    check(report, hashlib.sha256(profile_path.read_bytes()).hexdigest() == profile_digest,
          "Profile changed during import")
    report.update(blender_version=bpy.app.version_string,
                  addon_path=LoyalTools.__file__, addon_version=LoyalTools.bl_info.get("version"),
                  profile_sha256=profile_digest, component_count=profile["component_count"],
                  bones_count=profile["bones_count"], blueprints=[tree.name for tree in blueprints],
                  total_vertices=sum(item["vertices"] for item in report["objects"]),
                  total_triangles=sum(item["triangles"] for item in report["objects"]))
    if report["errors"]:
        raise RuntimeError(f"Workspace validation failed with {len(report['errors'])} error(s)")
    report["viewport"] = frame_viewports(bpy, objects)
    # Recheck immediately before saving; never replace a user's existing project.
    if args.output.exists():
        raise FileExistsError(args.output)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=True)
    if result != {"FINISHED"} or not args.output.is_file():
        raise RuntimeError(f"Saving .blend failed: {result}")
    # Image paths relative to the .blend survive moving the project folder.
    bpy.ops.file.make_paths_relative()
    absolute = [image.filepath for image in bpy.data.images
                if image.filepath and not image.filepath.startswith("//")]
    if absolute:
        report["warnings"].append(f"{len(absolute)} image path(s) could not be made relative, e.g. {absolute[0]}")
    missing = [image.filepath for image in bpy.data.images if image.filepath
               and not Path(bpy.path.abspath(image.filepath)).is_file()]
    if missing:
        raise RuntimeError(f"Image paths no longer resolve after saving: {missing[:3]}")
    result = bpy.ops.wm.save_mainfile()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Saving .blend failed: {result}")
    report["image_paths_relative"] = not absolute
    report["saved_blend"] = str(args.output)
    report["blend_bytes"] = args.output.stat().st_size
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
    report = {"status": "failed", "workspace": str(args.workspace.resolve()),
              "output": str(args.output), "materials_mode": args.materials,
              "objects": [], "errors": [], "warnings": []}
    try:
        run(args, report)
    except Exception:
        report["exception"] = traceback.format_exc()
        raise
    finally:
        with report_path.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        print("MERGED_WORKSPACE_REPORT=" + str(report_path), flush=True)
        print("MERGED_WORKSPACE_STATUS=" + report["status"], flush=True)


if __name__ == "__main__":
    main()
