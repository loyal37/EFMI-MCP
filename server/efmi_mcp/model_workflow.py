"""Model workflow: live capture -> LoyalTools workspace -> Blender project -> native mod export.

A project is a LoyalTools custom workspace root, laid out the way the add-on
expects, with this tool's own files next to it:

  <project>/                      named after the mod: the add-on names the INI after this folder
    workplace/                    LoyalTools workspace (profile, components, textures)
    <project>.blend               editable project; <project>.verification.json is its import report
    source/sample-1..n/           the captured frames the workspace was built from
    source/lod-<frame>/           a captured LoD frame, when one was taken
    exports/<export_id>/mod/      output of the add-on's own export
    exports/<export_id>/export-manifest.json, export-report.json, blender.log
    verification/<run_id>/        deployment, reload and draw comparison of one export
    efmi-model-project.json       what was done, with which versions

Grouping, skeleton merging, LoD matching and the INI/buffer generation are
LoyalTools'. This module moves data between the steps and checks the results.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from . import blender_runner, loyal_bridge, model_capture

PROJECT_FILE = "efmi-model-project.json"
INDEX_FORMATS = {"dxgi_format_r32_uint": 4, "r32_uint": 4, "dxgi_format_r16_uint": 2, "r16_uint": 2}


class WorkflowError(ValueError):
    """A step cannot proceed; the message says what to change."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.workplace = self.root / "workplace"
        self.blend = self.root / (self.root.name + ".blend")
        self.import_report = self.blend.with_suffix(".verification.json")
        self.source = self.root / "source"
        self.exports = self.root / "exports"
        self.verification = self.root / "verification"
        self.record_path = self.root / PROJECT_FILE

    def record(self) -> dict:
        if self.record_path.is_file():
            return json.loads(self.record_path.read_text(encoding="utf-8"))
        return {"project": self.root.name, "history": []}

    def note(self, event: str, **fields) -> dict:
        record = self.record()
        record.setdefault("history", []).append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields})
        write_json(self.record_path, record)
        return record

    def require_workspace(self) -> None:
        if not (self.workplace / "EFMI_MergedSkeleton.json").is_file():
            raise WorkflowError(f"{self.root} is not a model project: workplace/EFMI_MergedSkeleton.json is missing")

    def full_source(self) -> Path:
        return self.source / "sample-1"


def guard_output(root: Path, protected: list[Path]) -> None:
    """Projects are never created inside the tools this workflow only reads."""
    for folder in protected:
        folder = Path(folder).resolve()
        if root == folder or folder in root.parents:
            raise WorkflowError(f"Choose a project directory outside {folder}")


# ---------------------------------------------------------------- extraction
def scan_models(client, out_dir: Path, loyaltools_root: Path, job=None) -> dict:
    """Capture one frame and list the characters LoyalTools recognises in it."""
    archive = Path(out_dir) / ("model-scan-" + time.strftime("%Y%m%d-%H%M%S"))
    model_capture.archive_sample(client, archive, deferred_skeleton=True,
                                 on_stage=(lambda step: job.step("scan: " + step)) if job else None)
    listing = loyal_bridge.list_candidates(archive, loyaltools_root)
    listing["note"] = ("One frame, not validated across samples. Names are what efmi_extract_model takes as "
                       "object_name; with a mod active on a character its original draws are replaced and it "
                       "is not listed.")
    return listing


def extract_model(client, root: str | Path, *, loyaltools_root: Path, object_name: str = "", samples: int = 3,
                  sample_interval_s: float = 0.5, blender_path: str = "", import_blender: bool = True,
                  materials: str = "diffuse", lod_json: str = "", protected: list[Path] = (), job=None) -> dict:
    """Sample the game, build the workspace through LoyalTools and import it in Blender."""
    project = Project(root)
    guard_output(project.root, [loyaltools_root, *protected])
    if samples < 3:
        raise WorkflowError("At least three samples are needed to validate a static extraction")
    if materials not in ("diffuse", "native"):
        raise WorkflowError("materials must be 'diffuse' or 'native'")
    if project.root.exists() and any(project.root.iterdir()):
        raise WorkflowError(f"{project.root} is not empty. Extraction always starts a new project folder; "
                            "an earlier project's LoD mapping can be carried over with lod_json.")
    blender = blender_runner.find_blender(blender_path) if import_blender else None
    project.root.mkdir(parents=True, exist_ok=True)
    try:
        return _extract(client, project, loyaltools_root, object_name, samples, sample_interval_s, blender,
                        import_blender, materials, lod_json, job)
    except BaseException as exc:
        # Keep what was collected for diagnosis, and leave the requested path
        # free so the extraction can simply be run again.
        kept = project.root.with_name(project.root.name + ".failed-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            if any(project.root.iterdir()):
                shutil.move(str(project.root), str(kept))
            else:
                project.root.rmdir()
                kept = None
        except OSError:
            kept = project.root
        if isinstance(exc, WorkflowError):
            exc.details["partial_output"] = str(kept) if kept else None
            raise
        if isinstance(exc, Exception):
            raise WorkflowError(f"{type(exc).__name__}: {exc}", {"partial_output": str(kept) if kept else None}) from exc
        raise


def _extract(client, project: "Project", loyaltools_root, object_name, samples, sample_interval_s, blender,
             import_blender, materials, lod_json, job) -> dict:
    step = job.step if job else (lambda text: None)
    result = {"project": str(project.root), "stages": {}}

    step("sampling the game")
    status = client.status()
    archives = model_capture.collect_samples(client, project.source, count=samples, interval_s=sample_interval_s,
                                             on_stage=step, cancelled=job.cancelled if job else None)
    frames = [json.loads((archive / "readback.json").read_text(encoding="utf-8"))["frame"] for archive in archives]
    result["stages"]["sampling"] = {"archives": [str(archive) for archive in archives], "frames": frames}

    step("identifying the character")
    listing = loyal_bridge.list_candidates(archives[0], loyaltools_root)
    names = [item["name"] for item in listing["objects"]]
    if object_name:
        if object_name not in names:
            raise WorkflowError(f"Object {object_name!r} is not in the captured frame", {"candidates": listing["objects"]})
        chosen = object_name
    elif len(names) == 1:
        chosen = names[0]
    else:
        # The largest object is LoyalTools' default, but with several characters
        # in view that is a guess the caller should make knowingly.
        raise WorkflowError("Several characters are in view; pass object_name", {"candidates": [
            {key: item[key] for key in ("name", "component_count", "vertices")} for item in listing["objects"]]})
    result["object_name"] = chosen
    result["stages"]["candidates"] = [{key: item[key] for key in ("name", "component_count", "vertices")}
                                      for item in listing["objects"]]

    step("building the workspace (LoyalTools)")
    bridge = loyal_bridge.export_merged_workspace(archives[0], project.workplace, loyaltools_root, chosen,
                                                  validation_archives=archives[1:])
    result["stages"]["workspace"] = {
        "workspace": str(project.workplace), "components": bridge["components"], "bones_count": bridge["bones_count"],
        "archive_verified": bridge["archive_verified"], "warnings": bridge["warnings"],
        "upstream_warnings": bridge["upstream_warnings"][:20],
        "missing_texture_resources": bridge["missing_texture_resources"],
        "validation": {key: bridge["validation"][key] for key in (
            "scope", "changed_used_bone_matrices", "pose_reconstruction", "pose_limitation", "volatile_vertex_data")},
        "report": str(project.workplace / "live-bridge-report.json"),
    }

    if lod_json:
        step("applying the stored LoD mapping (JSON match)")
        result["stages"]["lod"] = loyal_bridge.restore_lod(project.workplace, lod_json, loyaltools_root)

    if import_blender:
        step("importing in Blender")
        result["stages"]["blender"] = import_workspace(project, blender, materials, job)

    project.note("extracted", object_name=chosen, frames=frames, materials=materials if import_blender else None,
                 game={"pid": status.get("pid"), "exe": status.get("exe"), "probe": status.get("probe")},
                 loyaltools_source={"path": str(loyaltools_root), "version": loyal_bridge.loyaltools_version(loyaltools_root)},
                 blender=result["stages"].get("blender", {}).get("versions"),
                 lod_json=str(lod_json) if lod_json else None)
    result["blend"] = str(project.blend) if import_blender else None
    result["scope"] = ("Full-detail model as drawn in the sampled frames. LoD levels are only what the profile "
                       "maps (see stages.lod or efmi_map_lod); none are invented from the full-detail data.")
    return result


def import_workspace(project: Project, blender: Path, materials: str = "diffuse", job=None) -> dict:
    """Create <project>.blend from the workspace with the installed add-on."""
    project.require_workspace()
    if project.blend.exists() or project.import_report.exists():
        raise WorkflowError(f"{project.blend.name} already exists; the import never replaces a project file")
    log = project.root / "blender-import.log"
    run = blender_runner.run_script(
        blender, blender_runner.SCRIPTS / "verify_merged_workspace_blender.py",
        ["--workspace", project.root, "--output", project.blend, "--materials", materials], log,
        cancelled=job.cancelled if job else None, on_process=job.attach_process if job else None)
    if not project.import_report.is_file():
        raise WorkflowError("Blender did not produce an import report", {"log_tail": blender_runner.tail(log)})
    report = json.loads(project.import_report.read_text(encoding="utf-8"))
    if run["exit_code"] != 0 or report.get("status") != "passed":
        raise WorkflowError("The Blender import did not pass its checks", {
            "errors": report.get("errors", [])[:10], "exception": (report.get("exception") or "")[-800:],
            "report": str(project.import_report), "log_tail": blender_runner.tail(log, 12)})
    connected = sum(1 for obj in report["objects"]
                    if any(image["connected_to_output"] for material in obj["materials"] for image in material["images"]))
    return {
        "blend": str(project.blend), "report": str(project.import_report), "log": str(log),
        "versions": {"blender": report.get("blender_version"), "blender_path": str(blender),
                     "addon_version": report.get("addon_version"), "addon_path": report.get("addon_path")},
        "materials_mode": report.get("materials_mode"), "objects": len(report["objects"]),
        "objects_with_diffuse_connected": connected,
        "vertices": report.get("total_vertices"), "triangles": report.get("total_triangles"),
        "blueprints": report.get("blueprints"), "image_paths_relative": report.get("image_paths_relative"),
        "warnings": report.get("warnings", []), "seconds": run["seconds"],
    }


# ---------------------------------------------------------------- LoD mapping
def map_lod(root: str | Path, method: str, *, loyaltools_root: Path, client=None, source: str = "",
            full_source: str = "", job=None) -> dict:
    """Add or refresh the project's LoD mapping.

    json:   apply a mapping stored in a profile JSON (``source``: that JSON or
            its workspace; empty: this project's own profile or its backup).
    frame:  match against a frame showing the character at a distance
            (``source``: a probe archive or a FrameAnalysis dump folder).
    capture: take that frame from the running game now, then match.
    """
    project = Project(root)
    project.require_workspace()
    step = job.step if job else (lambda text: None)
    before = loyal_bridge.lod_status(project.workplace, loyaltools_root)
    if method == "json":
        step("applying a stored LoD mapping (JSON match)")
        report = loyal_bridge.restore_lod(project.workplace, source, loyaltools_root)
    elif method in ("frame", "capture"):
        full = Path(full_source) if full_source else project.full_source()
        if not full.is_dir():
            # Projects made by LoyalTools itself record their own full-detail frame.
            full = Path(before["source_frame_dump"]) if before["source_frame_dump"] else full
        if method == "capture":
            if client is None:
                raise WorkflowError("method 'capture' needs the running game")
            step("capturing the LoD frame")
            lod = project.source / ("lod-" + time.strftime("%Y%m%d-%H%M%S"))
            model_capture.archive_sample(client, lod, deferred_skeleton=True,
                                         on_stage=(lambda text: step("LoD frame: " + text)))
            if job:
                # The frame is on disk. Matching takes about a minute and needs no game:
                # mods that were switched off for the capture can be switched on meanwhile.
                job.probe_done()
        else:
            if not source:
                raise WorkflowError("method 'frame' needs source: a probe archive or a FrameAnalysis dump folder")
            lod = Path(source)
        step("matching full-detail and LoD components (LoyalTools)")
        report = loyal_bridge.map_lod(project.workplace, lod, full, loyaltools_root)
    else:
        raise WorkflowError("method must be 'json', 'frame' or 'capture'")
    report["lod_objects_before"] = before["lod_objects"]
    report["note"] = ("The .blend keeps one editable mesh per component; the add-on generates the LoD entry points, "
                      "vertex-layout variants and bone remaps from this profile when the mod is exported. "
                      "Components listed as fallback have no LoD mesh of their own.")
    project.note("lod_mapped", method=report["method"], lod_objects=list(report["lod_objects"]),
                 source=report.get("mapping_source") or report.get("lod_source"))
    return report


# ---------------------------------------------------------------- export
def read_ini_sections(path: Path) -> list[tuple[str, list[str]]]:
    sections, current = [], None
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = (line[1:-1].strip(), [])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return sections


def _assignments(lines: list[str]) -> dict[str, str]:
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values.setdefault(key.strip().lower(), value.strip())
    return values


def inspect_export(mod_dir: Path, profile: dict | None = None, draw_sections: tuple = ("commandlist",)) -> dict:
    """Checks an exported mod as files: references, sizes, index ranges, entry points.

    The INI and buffers are the add-on's; nothing is repaired here. A failure
    means the export must not be deployed. ``draw_sections``: the kinds of
    section whose draw commands are collected (EFMI's exports draw from command
    lists, ZZMI's from TextureOverride sections too).
    """
    mod_dir = Path(mod_dir)
    errors, warnings = [], []
    inis = sorted(mod_dir.rglob("*.ini"))
    if not inis:
        return {"errors": ["no INI file"], "warnings": [], "ini_files": []}
    resources, entry_points, draws, vertex_draws = {}, [], [], []
    referenced = set()
    for ini in inis:
        sections = read_ini_sections(ini)
        for name, lines in sections:
            values = _assignments(lines)
            if name.lower().startswith("resource") and "filename" in values:
                path = (ini.parent / values["filename"].replace("\\", "/")).resolve()
                entry = {"ini": ini.name, "file": path, "stride": int(values["stride"]) if values.get("stride", "").isdigit() else 0,
                         "format": values.get("format", "").lower(), "type": values.get("type", "").lower()}
                resources[name.lower()] = entry
                referenced.add(path)
                if not path.is_file():
                    errors.append(f"{ini.name} [{name}]: missing file {values['filename']}")
                    continue
                size = path.stat().st_size
                entry["bytes"] = size
                unit = entry["stride"] or INDEX_FORMATS.get(entry["format"], 0)
                if unit and size % unit:
                    errors.append(f"{ini.name} [{name}]: {size} bytes is not a multiple of {unit}")
                if entry["type"] == "buffer" and not size:
                    warnings.append(f"{ini.name} [{name}]: empty buffer")
            elif name.lower().startswith("textureoverride") and "hash" in values:
                entry_points.append({"ini": ini.name, "section": name, "hash": values["hash"].lower(),
                                     # whether the game's own draw is skipped where this section applies
                                     "skips": "skip" in values.get("handling", "").lower().split(),
                                     **{key: int(values[key]) for key in ("match_first_index", "match_index_count",
                                                                          "match_first_vertex") if values.get(key, "").lstrip("-").isdigit()}})
        for name, lines in sections:
            if not name.lower().startswith(draw_sections):
                continue
            bound = {}
            for line in lines:
                key, separator, value = (part.strip() for part in line.partition("="))
                key = key.lower()
                if separator and key in ("ib", "vb0", "vb1", "vb2"):
                    bound[key] = value.split()[-1].lower()   # "ref ResourceX" or "ResourceX"
                elif separator and key == "draw":
                    # A non-indexed draw of the mod's own vertices (ZZMI: the skinning draw that streams them out).
                    numbers = [part.strip() for part in value.split(",")]
                    if len(numbers) != 2 or not all(n.lstrip("-").isdigit() for n in numbers):
                        continue
                    count, first = map(int, numbers)
                    vb = resources.get(bound.get("vb0", ""))
                    if not vb or "bytes" not in vb or not vb.get("stride"):
                        continue
                    draw = {"ini": ini.name, "section": name, "vertex_count": count, "first_vertex": first,
                            "vb0": bound.get("vb0"), "vb0_file": vb["file"].relative_to(mod_dir.resolve()).as_posix()}
                    vertex_draws.append(draw)
                    if first + count > vb["bytes"] // vb["stride"]:
                        errors.append(f"{ini.name} [{name}]: draw {count}, {first} exceeds the "
                                      f"{vb['bytes'] // vb['stride']} vertices of {vb['file'].name}")
                elif separator and key == "drawindexed":
                    numbers = [part.strip() for part in value.split(",")]
                    if len(numbers) != 3 or not all(n.lstrip("-").isdigit() for n in numbers):
                        continue   # "auto" and expressions are resolved by XXMI at run time
                    count, first, base = map(int, numbers)
                    draw = {"ini": ini.name, "section": name, "index_count": count, "first_index": first,
                            "base_vertex": base, "ib": bound.get("ib"), "vb0": bound.get("vb0")}
                    draws.append(draw)
                    ib, vb = resources.get(bound.get("ib", "")), resources.get(bound.get("vb0", ""))
                    # Which exported files this draw reads, for the run-time comparison.
                    for key, resource in (("ib_file", ib), ("vb0_file", vb)):
                        if resource and resource["file"].is_file():
                            draw[key] = resource["file"].relative_to(mod_dir.resolve()).as_posix()
                    if not ib or "bytes" not in ib:
                        errors.append(f"{ini.name} [{name}]: drawindexed without a readable index buffer")
                        continue
                    width = INDEX_FORMATS.get(ib["format"], 4)
                    if (first + count) * width > ib["bytes"]:
                        errors.append(f"{ini.name} [{name}]: draw range {first}+{count} exceeds {ib['file'].name}")
                        continue
                    data = ib["file"].read_bytes()[first * width:(first + count) * width]
                    highest = max(memoryview(data).cast("I" if width == 4 else "H"), default=0)
                    draw["max_index"] = highest
                    if vb and vb.get("stride") and "bytes" in vb:
                        vertices = vb["bytes"] // vb["stride"]
                        draw["vertex_count"] = vertices
                        if highest + base >= vertices:
                            errors.append(f"{ini.name} [{name}]: index {highest}+{base} is outside "
                                          f"{vertices} vertices of {vb['file'].name}")
        # Which exported index files each entry point leads to. The INI says so
        # itself (run = X, ... = ref X); the exporter's section names are not parsed.
        lines_of = {name.lower(): lines for name, lines in sections}
        files_in = {}
        for draw in draws:
            if draw["ini"] == ini.name and draw.get("ib_file"):
                files_in.setdefault(draw["section"].lower(), set()).add(draw["ib_file"])
        for entry in entry_points:
            if entry["ini"] != ini.name:
                continue
            seen, todo = set(), [entry["section"].lower()]
            while todo:
                current = todo.pop()
                if current in seen:
                    continue
                seen.add(current)
                for line in lines_of.get(current, []):
                    _, separator, value = line.partition("=")
                    todo.extend(token.lower() for token in (value if separator else line).split()
                                if token.lower() in lines_of and token.lower() not in seen)
            entry["index_files"] = sorted(set().union(*(files_in.get(section, set()) for section in seen)))
    unreferenced = [path.relative_to(mod_dir).as_posix() for path in sorted(mod_dir.rglob("*"))
                    if path.is_file() and path.suffix.lower() != ".ini" and path.resolve() not in referenced]
    if unreferenced:
        warnings.append(f"{len(unreferenced)} file(s) are not referenced by an INI, e.g. {unreferenced[0]}")

    coverage = None
    if profile:
        seen = {(e["hash"], e.get("match_index_count"), e.get("match_first_index", 0)) for e in entry_points}
        coverage = {"full_detail": [], "lod": {}, "missing": []}
        for component in profile["components"]:
            key = (component["ib_hash"], component["index_count"], component["first_index"])
            (coverage["full_detail"] if key in seen else coverage["missing"]).append(component["unique_str"])
            for lod in component.get("lods", []):
                if lod.get("is_fallback"):
                    continue
                key = (lod["ib_hash"], lod["index_count"], lod.get("first_index", 0))
                if key in seen:
                    coverage["lod"].setdefault(lod["lod_object_name"], []).append(component["unique_str"])
                elif (lod["ib_hash"], lod["index_count"]) != (component["ib_hash"], component["index_count"]):
                    coverage["missing"].append(component["unique_str"] + " @ " + lod["lod_object_name"])
        if coverage["missing"]:
            # A component removed in the blueprint keeps its entry point; one that
            # has none at all would leave the game's original mesh on screen.
            warnings.append("Components without an entry point in the INI: " + ", ".join(coverage["missing"][:8]))
    return {"errors": errors, "warnings": warnings, "ini_files": [ini.relative_to(mod_dir).as_posix() for ini in inis],
            "resources": len(resources), "draws": draws, "vertex_draws": vertex_draws, "entry_points": entry_points,
            "entry_point_coverage": coverage}


def export_mod(root: str | Path, *, blueprint: str = "", blender_path: str = "", blend: str = "", job=None) -> dict:
    """Export the saved project with the add-on's own exporter into exports/<id>/mod."""
    project = Project(root)
    project.require_workspace()
    blend_path = Path(blend).resolve() if blend else project.blend
    if not blend_path.is_file():
        raise WorkflowError(f"No saved project at {blend_path}. Save the .blend in Blender first; "
                            "unsaved edits cannot be read.")
    blender = blender_runner.find_blender(blender_path)
    step = job.step if job else (lambda text: None)
    export_id = time.strftime("%Y%m%d-%H%M%S")
    export_dir = project.exports / export_id
    while export_dir.exists():
        export_id += "x"
        export_dir = project.exports / export_id
    export_dir.mkdir(parents=True)
    mod_dir, report_path = export_dir / "mod", export_dir / "export-report.json"

    step("exporting with the add-on (Blender)")
    run = blender_runner.run_script(
        blender, blender_runner.SCRIPTS / "export_mod_blender.py",
        ["--blend", blend_path, "--output", mod_dir, "--report", report_path, "--workspace", project.root,
         *(["--blueprint", blueprint] if blueprint else [])],
        export_dir / "blender.log", cancelled=job.cancelled if job else None,
        on_process=job.attach_process if job else None)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    passed = run["exit_code"] == 0 and report.get("status") == "passed"

    step("checking the exported files")
    profile = json.loads((project.workplace / "EFMI_MergedSkeleton.json").read_text(encoding="utf-8"))
    inspection = inspect_export(mod_dir, profile) if passed else {"errors": ["the export did not complete"], "warnings": []}
    files = [{"path": path.relative_to(mod_dir).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
             for path in sorted(mod_dir.rglob("*")) if path.is_file()] if mod_dir.is_dir() else []
    valid = passed and not inspection["errors"]
    manifest = {
        "export_id": export_id, "valid": valid, "project": str(project.root), "mod_dir": str(mod_dir),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blend": {"path": str(blend_path), "sha256": report.get("blend_sha256"), "modified": report.get("blend_modified")},
        "profile_sha256": report.get("profile_sha256"), "blueprint": report.get("blueprint"),
        "versions": {"blender": report.get("blender_version"), "blender_path": str(blender),
                     "addon_version": report.get("addon_version"), "addon_path": report.get("addon_path")},
        "ini_files": inspection.get("ini_files", []), "entry_points": inspection.get("entry_points", []),
        "draws": inspection.get("draws", []), "files": files,
    }
    write_json(export_dir / "export-manifest.json", manifest)
    result = {
        "export_id": export_id, "export_valid": valid, "export_dir": str(export_dir), "mod_dir": str(mod_dir),
        "manifest": str(export_dir / "export-manifest.json"), "report": str(report_path),
        "blender_log": run["log"], "addon_log": report.get("export_log"), "blueprint": report.get("blueprint"),
        "versions": manifest["versions"], "seconds": run["seconds"],
        "files": len(files), "bytes": sum(item["bytes"] for item in files), "ini_files": manifest["ini_files"],
        "objects": [{key: obj.get(key) for key in ("name", "vertices", "polygons", "unweighted_used_vertices")}
                    for obj in report.get("objects", [])],
        "profile": report.get("profile"), "entry_point_coverage": inspection.get("entry_point_coverage"),
        "errors": report.get("errors", []) + inspection["errors"],
        "warnings": report.get("warnings", []) + inspection["warnings"],
        "addon_warnings": report.get("log_warnings", [])[:20],
        "note": ("Produced by the add-on's Generate Mod operator from the .blend as saved on disk. "
                 "Components the add-on handles specially (for example CPU-posed ones, or ones removed in the "
                 "blueprint) keep the game's own draw; see the add-on log."),
    }
    if not valid:
        result["exception"] = (report.get("exception") or "")[-800:]
        result["log_tail"] = blender_runner.tail(run["log"], 12)
    project.note("exported", export_id=export_id, valid=valid, blueprint=report.get("blueprint"), files=len(files))
    return result


def latest_export(project: Project, export_id: str = "") -> tuple[Path, dict]:
    """The export directory and manifest to use: the named one, else the newest valid one."""
    if export_id and export_id != "latest":
        path = project.exports / export_id / "export-manifest.json"
        if not path.is_file():
            raise WorkflowError(f"Export {export_id!r} does not exist in {project.exports}")
        return path.parent, json.loads(path.read_text(encoding="utf-8"))
    manifests = sorted(project.exports.glob("*/export-manifest.json"), reverse=True) if project.exports.is_dir() else []
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("valid"):
            return path.parent, manifest
    raise WorkflowError("This project has no valid export yet; run efmi_export_mod first")


def describe_project(root: str | Path, loyaltools_root: Path) -> dict:
    """What a project folder currently holds."""
    project = Project(root)
    project.require_workspace()
    exports = []
    for path in sorted(project.exports.glob("*/export-manifest.json")) if project.exports.is_dir() else []:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        exports.append({"export_id": manifest["export_id"], "valid": manifest.get("valid"),
                        "created": manifest.get("created"), "files": len(manifest.get("files", []))})
    record = project.record()
    return {"project": str(project.root), "blend": str(project.blend) if project.blend.is_file() else None,
            "blend_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(project.blend.stat().st_mtime))
            if project.blend.is_file() else None,
            "lod": loyal_bridge.lod_status(project.workplace, loyaltools_root), "exports": exports,
            "samples": sorted(p.name for p in project.source.glob("*") if p.is_dir()) if project.source.is_dir() else [],
            "history": record.get("history", [])[-8:]}


_SAFE_NAME = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+$")


def safe_folder_name(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME.match(name)) and name.strip(". ") == name and name not in (".", "..")
