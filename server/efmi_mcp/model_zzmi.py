"""Model workflow for Zenless Zone Zero (ZZMI): frame analysis folder -> LoyalTools -> Blender -> mod.

LoyalTools reads Zenless Zone Zero from an XXMI frame analysis folder (its
``extract/zzmi_dump_extractor.py``), so that is the hand-over: either a folder
the user made with XXMI's F8, or one this server writes from a live capture with
contents, in XXMI's own log syntax (``contents.dump_frame(log='xxmi')``).
Extraction, the merged skeleton, import and export are the add-on's own entry
points, run in background Blender with the ZZMI preset; this module moves data
between them and checks the results as files and against XXMI's INI syntax.

The project folder is laid out as for Endfield (see model_workflow), with the
add-on's ZZMI workspace in workplace/ and the frame it came from in source/
(or only named in the project record when the user's own folder was used).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sys
import time
import types
from pathlib import Path

from . import blender_runner, contents
from .capture_model import Capture
from .model_workflow import Project, WorkflowError, guard_output, inspect_export, sha256_file, write_json

GAME = "ZZMI"
CONTENTS = {"inputs": ["ib", "vb", "cb", "buf", "tex"], "events": True, "budget_mb": 2048}


def project_game(root: Path) -> str | None:
    """Which game a project folder was made for: 'EFMI', 'ZZMI', or None when it is not a model project."""
    workplace = Path(root) / "workplace"
    if (workplace / "EFMI_MergedSkeleton.json").is_file():
        return "EFMI"
    if (workplace / "ZZMI_MergedSkeleton.json").is_file():
        return GAME
    record = Path(root) / "efmi-model-project.json"
    if (workplace / "Import.json").is_file() and record.is_file():
        if json.loads(record.read_text(encoding="utf-8")).get("game") == GAME:
            return GAME
    return None


def require_workspace(project: Project) -> None:
    if project_game(project.root) != GAME:
        raise WorkflowError(f"{project.root} is not a Zenless Zone Zero model project (no ZZMI workspace in workplace/)")


# ---------------------------------------------------------------- LoyalTools outside Blender
def load_extractor(loyaltools_root: Path):
    """LoyalTools' ZZMI extractor module, imported without bpy and without writing bytecode into the add-on."""
    root = Path(loyaltools_root).resolve()
    if not (root / "extract" / "zzmi_dump_extractor.py").is_file():
        raise WorkflowError(f"This LoyalTools ({root}) has no Zenless Zone Zero extractor; update the add-on")
    name = "_efmi_loyal_zzmi_" + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(root)]
        sys.modules[name] = package
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name + ".extract.zzmi_dump_extractor")
    finally:
        sys.dont_write_bytecode = keep


def require_dump(folder: str | Path) -> Path:
    folder = Path(folder)
    if not folder.is_absolute() or not (folder / "log.txt").is_file():
        raise WorkflowError(f"{folder} is not a frame analysis folder (an absolute path with log.txt)")
    return folder.resolve()


def list_models(dump: Path, loyaltools_root: Path) -> dict:
    """The meshes LoyalTools finds in a frame, skinned (character) ones first."""
    module = load_extractor(loyaltools_root)
    try:
        extractor = module.ZZMIDumpExtractor(str(dump))
        summaries = extractor.list_draw_ibs()
    except module.ExtractError as exc:
        raise WorkflowError(f"LoyalTools could not read the frame: {exc}")
    meshes = [{"ib_hash": item.ib_hash, "draws": item.draw_call_count, "indices": item.total_index_count,
               "skinned": item.has_blend, "textures": item.texture_count} for item in summaries]
    return {"game": GAME, "source": str(dump), "meshes": meshes,
            "skinned": [item["ib_hash"] for item in meshes if item["skinned"]],
            "note": ("Skinned meshes are characters' parts: Zenless Zone Zero poses them with a stream-output draw. "
                     "efmi_extract_model mode='merged' (LoyalTools' merged skeleton) picks the character that the "
                     "largest skinned mesh belongs to by the bones its parts share, with its face parts kept as the "
                     "game draws them; mode='skinned' extracts every skinned mesh as it is. A mesh a mod replaces is "
                     "not drawn by the game: switch mods off with efmi_toggle_mods first.")}


# ---------------------------------------------------------------- a frame from the running game
def skinned_calls(cap: Capture) -> set[int]:
    """The calls LoyalTools needs files of: skinning draws (stream output) and the draws that read what they wrote."""
    writers: dict[int, list[int]] = {}
    for d in cap.draws():
        # By the call: an index buffer left bound from an earlier draw is recorded for a Draw too.
        if d["call"] in ("Draw", "DrawInstanced"):
            for entry in d.get("so", {}).values():
                writers.setdefault(entry["res"], []).append(d["index"])
    chosen: set[int] = set()
    for d in cap.draws():
        vb0 = (d.get("vb") or {}).get(0)
        if d["call"].startswith("DrawIndexed") and vb0 and vb0["res"] in writers:
            chosen.add(d["index"])
            chosen.update(writers[vb0["res"]])
    return chosen


def live_frame(session, folder: Path, on_stage=None) -> dict:
    """Capture the next frame with contents and write it as a frame analysis folder LoyalTools reads."""
    stage = on_stage or (lambda text: None)
    stage("capturing a frame with contents")
    doc, blob = session.call(lambda c: c.capture(detail="full", cb_data=False, timeout_ms=15000, contents=CONTENTS))
    cap = Capture(doc, blob)
    session.add_capture(cap)
    chosen = skinned_calls(cap)
    if not chosen:
        raise WorkflowError("The frame has no skinned character draws (no stream-output skinning draw is read by an "
                            "indexed draw). Is a character on screen, drawn by the game (mods off), and is the probe "
                            "0.1.12 or later?", {"frame": cap.frame, "draws": len(cap)})
    stage(f"writing {len(chosen)} calls as a frame analysis folder")
    written = contents.dump_frame(session, cap, folder=str(folder), log="xxmi", files_for=chosen,
                                  max_seconds=600, max_mb=8192)
    if written.get("stopped") or written.get("skipped", {}).get("not_exact") or written.get("skipped", {}).get("failed"):
        raise WorkflowError("The frame could not be written completely", {"dump": written})
    return {"frame": cap.frame, "folder": str(folder), "calls_with_files": len(chosen), "files": written["files"],
            "megabytes": written["megabytes"], "contents": cap.stats.get("ct_bytes"),
            "lost": cap.stats.get("ct_lost", 0)}


# ---------------------------------------------------------------- extraction
def extract_model(session, root: str | Path, *, loyaltools_root: Path, frame_dump: str = "", mode: str = "merged",
                  blender_path: str = "", materials: str = "diffuse", protected: list[Path] = (), job=None) -> dict:
    """A frame (the user's folder, or one captured now) -> LoyalTools' ZZMI workspace -> <project>.blend."""
    project = Project(root)
    guard_output(project.root, [loyaltools_root, *protected])
    if mode not in ("merged", "skinned"):
        raise WorkflowError("mode must be 'merged' or 'skinned'")
    if materials not in ("diffuse", "native"):
        raise WorkflowError("materials must be 'diffuse' or 'native'")
    if project.root.exists() and any(project.root.iterdir()):
        raise WorkflowError(f"{project.root} is not empty. Extraction always starts a new project folder.")
    dump = require_dump(frame_dump) if frame_dump else None
    if dump is not None:
        guard_output(project.root, [dump])
    blender = blender_runner.find_blender(blender_path)
    step = job.step if job else (lambda text: None)
    project.root.mkdir(parents=True, exist_ok=True)
    result = {"project": str(project.root), "game": GAME, "mode": mode, "stages": {}}
    try:
        if dump is None:
            folder = project.source / time.strftime("frame-%Y%m%d-%H%M%S")
            result["stages"]["frame"] = live_frame(session, folder, on_stage=step)
            if job:
                job.probe_done()        # the frame is on disk; extraction needs no game
            dump = folder
        else:
            result["stages"]["frame"] = {"folder": str(dump), "from": "the user's frame analysis folder (only read)"}
        step("extracting and importing with LoyalTools (Blender)")
        log = project.root / "blender-import.log"
        run = blender_runner.run_script(
            blender, blender_runner.SCRIPTS / "zzmi_extract_blender.py",
            ["--dump", dump, "--workspace", project.root, "--output", project.blend, "--mode", mode,
             "--materials", materials], log,
            cancelled=job.cancelled if job else None, on_process=job.attach_process if job else None)
        report = json.loads(project.import_report.read_text(encoding="utf-8")) if project.import_report.is_file() else {}
        if run["exit_code"] != 0 or report.get("status") != "passed":
            raise WorkflowError("The LoyalTools extraction in Blender did not pass its checks", {
                "errors": report.get("errors", [])[:10], "exception": (report.get("exception") or "")[-800:],
                "addon_report": report.get("addon_report"), "report": str(project.import_report),
                "log_tail": blender_runner.tail(log, 12)})
    except BaseException as exc:
        # Keep what was made for diagnosis, and leave the requested path free for another try.
        kept = project.root.with_name(project.root.name + ".failed-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.move(str(project.root), str(kept))
        except OSError:
            kept = project.root
        if isinstance(exc, WorkflowError):
            exc.details["partial_output"] = str(kept)
            raise
        if isinstance(exc, Exception):
            raise WorkflowError(f"{type(exc).__name__}: {exc}", {"partial_output": str(kept)}) from exc
        raise
    result["stages"]["blender"] = {
        "blend": str(project.blend), "report": str(project.import_report), "log": run["log"],
        "versions": {"blender": report.get("blender_version"), "blender_path": str(blender),
                     "addon_version": report.get("addon_version"), "addon_path": report.get("addon_path")},
        "submeshes": report.get("submeshes"), "vertices": report.get("total_vertices"),
        "triangles": report.get("total_triangles"), "blueprints": report.get("blueprints"),
        "profile": report.get("profile"), "warnings": report.get("warnings", []), "seconds": run["seconds"],
        "objects": [{key: obj.get(key) for key in ("name", "role", "vertices", "triangles", "vertex_groups")}
                    for obj in report.get("objects", [])]}
    status = session.call(lambda c: c.status()) if not frame_dump else {}
    record = project.record()
    record["game"] = GAME
    write_json(project.record_path, record)
    project.note("extracted", game=GAME, mode=mode, source=str(dump), materials=materials,
                 game_process={"pid": status.get("pid"), "exe": status.get("exe"), "probe": status.get("probe")},
                 blender=result["stages"]["blender"]["versions"])
    result["blend"] = str(project.blend)
    result["scope"] = ("The model as LoyalTools extracts it from this one frame. Faces that the game drives by shape keys "
                       "keep the game's mesh in the export (textures only); parts without shared bones (bangboo, "
                       "weapons, other characters) are left out by the merged skeleton.")
    return result


# ---------------------------------------------------------------- export
def coverage(project: Project, entry_points: list[dict]) -> dict:
    """Every extracted part has an entry point (its index buffer, first index and count) in the INI."""
    seen = {(e["hash"], e.get("match_first_index"), e.get("match_index_count")) for e in entry_points}
    import_map = json.loads((project.workplace / "Import.json").read_text(encoding="utf-8-sig"))
    covered, missing = [], []
    for unique_str, gametype in sorted(import_map.items()):
        path = project.workplace / unique_str / ("TYPE_" + gametype) / (unique_str + ".json")
        frame = json.loads(path.read_text(encoding="utf-8-sig")).get("ZZMIFrameExtract", {}) if path.is_file() else {}
        key = (str(frame.get("IB", "")).lower(), frame.get("FirstIndex"), frame.get("IndexCount"))
        (covered if key in seen else missing).append(unique_str)
    return {"parts": covered, "missing": missing}


def export_mod(root: str | Path, *, blueprint: str = "", blender_path: str = "", blend: str = "", job=None) -> dict:
    """Export the saved project with the add-on's own Generate Mod (ZZMI preset) into exports/<id>/mod."""
    project = Project(root)
    require_workspace(project)
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
        blender, blender_runner.SCRIPTS / "zzmi_export_blender.py",
        ["--blend", blend_path, "--output", mod_dir, "--report", report_path, "--workspace", project.root,
         *(["--blueprint", blueprint] if blueprint else [])],
        export_dir / "blender.log", cancelled=job.cancelled if job else None,
        on_process=job.attach_process if job else None)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    passed = run["exit_code"] == 0 and report.get("status") == "passed"

    step("checking the exported files")
    if passed:
        inspection = inspect_export(mod_dir, draw_sections=("commandlist", "textureoverride"))
        inspection["entry_point_coverage"] = coverage(project, inspection["entry_points"])
        if inspection["entry_point_coverage"]["missing"]:
            inspection["warnings"].append("Parts without an entry point in the INI: "
                                          + ", ".join(inspection["entry_point_coverage"]["missing"][:8]))
    else:
        inspection = {"errors": ["the export did not complete"], "warnings": []}
    files = [{"path": path.relative_to(mod_dir).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
             for path in sorted(mod_dir.rglob("*")) if path.is_file()] if mod_dir.is_dir() else []
    valid = passed and not inspection["errors"]
    manifest = {
        "export_id": export_id, "game": GAME, "valid": valid, "project": str(project.root), "mod_dir": str(mod_dir),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blend": {"path": str(blend_path), "sha256": report.get("blend_sha256"), "modified": report.get("blend_modified")},
        "profile_sha256": report.get("profile_sha256"), "blueprint": report.get("blueprint"),
        "versions": {"blender": report.get("blender_version"), "blender_path": str(blender),
                     "addon_version": report.get("addon_version"), "addon_path": report.get("addon_path")},
        "ini_files": inspection.get("ini_files", []), "entry_points": inspection.get("entry_points", []),
        "draws": inspection.get("draws", []), "vertex_draws": inspection.get("vertex_draws", []), "files": files,
    }
    write_json(export_dir / "export-manifest.json", manifest)
    result = {
        "export_id": export_id, "game": GAME, "export_valid": valid, "export_dir": str(export_dir), "mod_dir": str(mod_dir),
        "manifest": str(export_dir / "export-manifest.json"), "report": str(report_path),
        "blender_log": run["log"], "addon_log": report.get("export_log"), "blueprint": report.get("blueprint"),
        "versions": manifest["versions"], "seconds": run["seconds"],
        "files": len(files), "bytes": sum(item["bytes"] for item in files), "ini_files": manifest["ini_files"],
        "objects": report.get("objects", []), "entry_point_coverage": inspection.get("entry_point_coverage"),
        "draws": len(manifest["draws"]), "skinning_draws": len(manifest["vertex_draws"]),
        "errors": report.get("errors", []) + inspection["errors"],
        "warnings": report.get("warnings", []) + inspection["warnings"],
        "note": ("Produced by the add-on's Generate Mod operator from the .blend as saved on disk, with the ZZMI preset. "
                 "Faces driven by shape keys keep the game's mesh (their entry points only set textures)."),
    }
    if not valid:
        result["exception"] = (report.get("exception") or "")[-800:]
        result["log_tail"] = blender_runner.tail(run["log"], 12)
    project.note("exported", export_id=export_id, valid=valid, blueprint=report.get("blueprint"), files=len(files))
    return result


def describe_project(root: str | Path) -> dict:
    project = Project(root)
    require_workspace(project)
    exports = []
    for path in sorted(project.exports.glob("*/export-manifest.json")) if project.exports.is_dir() else []:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        exports.append({"export_id": manifest["export_id"], "valid": manifest.get("valid"),
                        "created": manifest.get("created"), "files": len(manifest.get("files", []))})
    record = project.record()
    profile = project.workplace / "ZZMI_MergedSkeleton.json"
    parts = json.loads((project.workplace / "Import.json").read_text(encoding="utf-8-sig"))
    return {"project": str(project.root), "game": GAME,
            "blend": str(project.blend) if project.blend.is_file() else None,
            "blend_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(project.blend.stat().st_mtime))
            if project.blend.is_file() else None,
            "parts": sorted(parts), "merged_skeleton": profile.is_file(), "exports": exports,
            "frames": sorted(p.name for p in project.source.glob("*") if p.is_dir()) if project.source.is_dir() else [],
            "lod": "Zenless Zone Zero has one level of detail: no LoD mapping.",
            "history": record.get("history", [])[-8:]}
