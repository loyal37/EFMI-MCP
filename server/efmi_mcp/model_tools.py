"""MCP tools for the model workflow: extract, map LoD, export, deploy and verify.

Long steps run as jobs (see model_jobs): the tool waits a while for the job and
otherwise returns its ID, which efmi_model_job follows or cancels.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import loyal_bridge, mod_runtime, model_capture, model_workflow
from .blender_runner import BlenderError
from .capture_model import Capture
from .loyal_bridge import BridgeError
from .mcp_stdio import ToolError
from .model_workflow import Project, WorkflowError
from .probe_client import ProbeError
from .reload_workflow import reload_and_capture

WAIT = {"type": "integer", "minimum": 0, "maximum": 300, "default": 50,
        "description": "Seconds to wait for the job before returning its ID instead of the result."}
PROJECT = {"type": "string", "description": "Project folder (the LoyalTools workspace root that contains workplace/)."}


def _fail(exc: Exception) -> ToolError:
    details = getattr(exc, "details", None)
    text = str(exc)
    if details:
        text += "\n" + json.dumps(details, ensure_ascii=False, indent=1)
    return ToolError(text)


def register(srv, s) -> None:
    """Adds the model tools to ``srv``; ``s`` is the tools.Session."""

    def start(kind: str, summary: str, function, wait_s: int, uses_probe: bool, loyaltools: dict | None = None):
        def guarded(job):
            try:
                result = function(job)
                if isinstance(result, dict) and loyaltools:
                    result["loyaltools"] = loyaltools   # which copy did the work
                return result
            except (WorkflowError, BridgeError, BlenderError, ProbeError) as exc:
                # Shown to the agent as the job's error, with any details attached.
                raise RuntimeError(str(_fail(exc))) from exc

        try:
            job = s.jobs.start(kind, summary, guarded, uses_probe=uses_probe)
        except RuntimeError as exc:
            raise ToolError(str(exc))
        job.thread.join(timeout=max(0, wait_s))
        return job.describe()

    def root_of(project_dir: str) -> Path:
        if not project_dir or not Path(project_dir).is_absolute():
            raise ToolError("project_dir must be an absolute path")
        return Path(project_dir)

    # ================================================================ scan
    @srv.tool("efmi_list_models",
              "Captures one frame and lists the characters LoyalTools recognises in it: the object names that "
              "efmi_extract_model accepts, their components (index-buffer hash, index and vertex counts) and a "
              "screenshot of the frame. A character whose mesh a mod currently replaces is not listed, because its "
              "original draws are not issued; disable that mod and reload first.",
              read_only=False)
    def efmi_list_models():
        try:
            listing = model_workflow.scan_models(s.probe(), s.out_dir, s.loyaltools_root)
        except (WorkflowError, BridgeError, ProbeError, ValueError) as exc:
            raise _fail(exc)
        for obj in listing["objects"]:
            for component in obj["components"]:
                note = s.mod_note(component["ib_hash"])
                if note:
                    component["installed_mods"] = note.strip()
        listing["loyaltools"] = s.loyaltools()
        return listing

    # ================================================================ extract
    @srv.tool("efmi_extract_model",
              "Extracts the full-detail model of a character on screen into a new project folder: samples the game "
              "three times, builds the LoyalTools merged-skeleton workspace with LoyalTools' own algorithm (the samples "
              "must agree on geometry and bone-group sources), optionally applies a stored LoD mapping (lod_json, "
              "LoyalTools' JSON match), and imports the result in background Blender as <project>.blend with only the "
              "diffuse map connected to the material. The other game textures stay in the workspace for the export. "
              "Runs as a job. The character must be drawn by the game itself: with a mod active on it, disable the "
              "mod and reload first.",
              {"output_dir": {"type": "string", "description": "New (or empty) project folder; its name becomes the mod's name."},
               "object_name": {"type": "string", "default": "", "description": "Name from efmi_list_models; needed when several characters are in view."},
               "samples": {"type": "integer", "minimum": 3, "maximum": 6, "default": 3},
               "lod_json": {"type": "string", "default": "",
                            "description": "A profile JSON, or the workspace/project, of the same character that already holds a LoD mapping."},
               "import_blender": {"type": "boolean", "default": True},
               "materials": {"type": "string", "enum": ["diffuse", "native"], "default": "diffuse",
                             "description": "diffuse: only the diffuse map is connected; native: the add-on's setup with normal map."},
               "blender_path": {"type": "string", "default": ""},
               "wait_s": WAIT},
              ["output_dir"], read_only=False)
    def efmi_extract_model(output_dir, object_name="", samples=3, lod_json="", import_blender=True,
                           materials="diffuse", blender_path="", wait_s=50):
        root = root_of(output_dir)
        blender = blender_path or s.blender_path
        # The add-on of the Blender that imports the result also builds the workspace.
        used = s.loyaltools(blender)

        def run(job):
            return model_workflow.extract_model(
                s.probe(), root, loyaltools_root=Path(used["path"]), object_name=object_name, samples=samples,
                blender_path=blender, import_blender=import_blender, materials=materials,
                lod_json=lod_json, protected=[s.efmi_root], job=job)

        return start("extract", f"extract {object_name or 'the character in view'} to {root}", run, wait_s, True, used)

    # ================================================================ LoD
    @srv.tool("efmi_map_lod",
              "Adds or refreshes a project's LoD mapping with LoyalTools' own matcher. method='json' applies a mapping "
              "that is already stored as JSON (source: a profile JSON or the workspace of the same character; empty: "
              "this project's own profile or the copy kept before re-extraction) and needs no capture. method='frame' "
              "matches the project's full-detail frame against a frame that shows the character at LoD distance "
              "(source: a FrameAnalysis dump folder or a probe archive). method='capture' takes that LoD frame from "
              "the running game now, so the character must currently be drawn at LoD distance. The mapping is stored "
              "in workplace/EFMI_MergedSkeleton.json; the .blend keeps one mesh per component and the add-on generates "
              "the LoD entry points and buffer variants at export. Components without a LoD mesh are reported as "
              "fallback; nothing is invented for a LoD that was not captured.",
              {"project_dir": PROJECT,
               "method": {"type": "string", "enum": ["json", "frame", "capture"]},
               "source": {"type": "string", "default": ""},
               "full_source": {"type": "string", "default": "",
                               "description": "Full-detail frame for 'frame'/'capture' (default: the project's source/sample-1)."},
               "wait_s": WAIT},
              ["project_dir", "method"], read_only=False)
    def efmi_map_lod(project_dir, method, source="", full_source="", wait_s=50):
        root = root_of(project_dir)
        used = s.loyaltools()

        def run(job):
            return model_workflow.map_lod(root, method, loyaltools_root=Path(used["path"]),
                                          client=s.probe() if method == "capture" else None,
                                          source=source, full_source=full_source, job=job)

        return start("lod", f"{method} LoD mapping for {root.name}", run, wait_s, method == "capture", used)

    # ================================================================ export
    @srv.tool("efmi_export_mod",
              "Exports the project's saved .blend as a game mod (INI, buffers, textures) with the LoyalTools add-on's own "
              "Generate Mod operator in background Blender, into exports/<export_id>/mod inside the project. Reads the "
              ".blend as saved on disk and never saves it; unsaved edits in an open Blender are not seen. The result is "
              "checked as files (every referenced file exists, sizes fit their stride, draw ranges and indices are in "
              "bounds, every component has an entry point) and gets a manifest with SHA256 of each file. "
              "export_valid=false means it must not be deployed. Runs as a job.",
              {"project_dir": PROJECT,
               "blueprint": {"type": "string", "default": "", "description": "Blueprint to export when the file has several."},
               "blend": {"type": "string", "default": "", "description": "Another saved .blend of this project (default: <project>.blend)."},
               "blender_path": {"type": "string", "default": ""},
               "wait_s": WAIT},
              ["project_dir"], read_only=False)
    def efmi_export_mod(project_dir, blueprint="", blend="", blender_path="", wait_s=50):
        root = root_of(project_dir)

        def run(job):
            return model_workflow.export_mod(root, blueprint=blueprint, blender_path=blender_path or s.blender_path,
                                             blend=blend, job=job)

        return start("export", f"export {root.name}", run, wait_s, False)

    # ================================================================ deploy + verify
    def verify(job, project: Project, manifest: dict, action: str, target: Path, settle_ms: int, check_textures: bool):
        run_dir = project.verification / time.strftime("%Y%m%d-%H%M%S")
        report = {"export_id": manifest["export_id"], "export_valid": bool(manifest.get("valid")),
                  "deployment_verified": None, "reload": None, "fresh_capture": None,
                  "target_mod_status": "not_run", "run_dir": str(run_dir), "target": str(target)}
        client = s.probe()
        baseline = None
        if action == "verify":
            job.step("baseline frame before deployment")
            doc, blob = client.capture(detail="full", cb_data=False, timeout_ms=8000)
            before = Capture(doc, blob)
            baseline = mod_runtime.entry_draw_counts(before, manifest.get("entry_points", []))
            model_capture.save_screenshot(client, before, run_dir / "screenshots" / "before.png")
            report["baseline"] = {"frame": before.frame, "game_draws_per_entry_point": baseline,
                                  "character_in_view": any(baseline.values())}
            job.step("deploying the export")
            report["conflicts"] = mod_runtime.find_conflicts(manifest, s.mods.get(), target, s.efmi_root)
            deployment = mod_runtime.deploy_export(manifest, target, run_dir / "previous-deployment")
            model_workflow.write_json(run_dir / "deployment.json", deployment)
            report["deployment"] = deployment
            report["deployment_verified"] = deployment["verified_identical"]
            s.mods.invalidate()
            job.step("reloading (F10 inside the game) and capturing")
            try:
                reload = reload_and_capture(s, detail="full", cb_data=False, settle_ms=settle_ms)
            except ToolError as exc:
                reload = json.loads(str(exc))
            model_workflow.write_json(run_dir / "reload.json", reload)
            report["reload"] = {key: reload.get(key) for key in (
                "status", "phase", "reason", "input_error_code", "sender", "reload_requested", "reload_confirmed",
                "confirmation_note", "reload_evidence")}
            if reload.get("status") != "captured_after_reload_request":
                # The files are in place; whether XXMI loaded them is unknown. No second F10.
                report["fresh_capture"] = False
                report["reasons"] = ["the reload or the capture after it failed; see reload"]
                return finish(project, run_dir, report)
            cap = s.capture("latest")
        else:
            status = mod_runtime.deployment_status(target)
            report["deployment"] = status
            report["deployment_verified"] = (status.get("managed") and status.get("export_id") == manifest["export_id"]
                                             and not status.get("changed_or_missing"))
            job.step("capturing a frame")
            doc, blob = client.capture(detail="full", cb_data=False, timeout_ms=8000)
            cap = Capture(doc, blob)
            s.add_capture(cap)
        report["fresh_capture"] = True
        job.step("comparing the drawn buffers with the exported files")
        checked = mod_runtime.verify_capture(cap, client, manifest, baseline)
        if check_textures:
            job.step("comparing bound textures with the exported files")
            checked["textures"] = mod_runtime.verify_textures(cap, client, manifest)
        model_capture.save_screenshot(client, cap, run_dir / "screenshots" / "after.png")
        model_workflow.write_json(run_dir / "draw-verification.json", checked)
        report.update(target_mod_status=checked["target_mod_status"], reasons=checked["reasons"],
                      frame=checked["frame"], covered=checked["covered"], not_covered=checked["not_covered"],
                      draws_of_this_mod=checked["draws_of_this_mod"],
                      exported_buffers_seen=len(checked["exported_buffers_seen"]),
                      exported_buffers_identical=sum(1 for info in checked["exported_buffers_seen"].values()
                                                     if info["whole_buffer"] == "identical"),
                      exported_buffers_not_seen=checked["exported_buffers_not_seen"][:20],
                      textures=checked.get("textures", {}).get("textures_identical") if check_textures else None,
                      screenshots=[str(p) for p in sorted((run_dir / "screenshots").glob("*.png"))],
                      details=str(run_dir / "draw-verification.json"))
        return finish(project, run_dir, report)

    def finish(project: Project, run_dir: Path, report: dict) -> dict:
        report["scope"] = ("Covers the components and LoD level that were on screen in the captured frame. "
                           "reload_confirmed only says that XXMI reloaded; target_mod_status is the check of this export. "
                           "Matching buffers do not prove that the result looks right: see the screenshots.")
        model_workflow.write_json(run_dir / "report.json", report)
        lines = [f"# Verification of export {report['export_id']}", "",
                 f"- export valid: {report['export_valid']}", f"- deployment identical to export: {report['deployment_verified']}",
                 f"- reload: {json.dumps(report.get('reload'), ensure_ascii=False)}",
                 f"- fresh capture: {report['fresh_capture']}", f"- target mod status: **{report['target_mod_status']}**",
                 f"- reasons: {report.get('reasons')}", f"- covered: {report.get('covered')}",
                 f"- not covered: {report.get('not_covered')}", "", report["scope"], ""]
        (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
        project.note("verified", export_id=report["export_id"], target_mod_status=report["target_mod_status"],
                     run_dir=str(run_dir))
        return report

    @srv.tool("efmi_verify_exported_mod",
              "Deploys one valid export as a test mod and checks what the game then draws. action='verify' (default): "
              "take a baseline frame, copy the export into its own folder under <EFMI>/Mods (default "
              "Mods/EFMI-MCP/<project>; only a folder this tool created and marked is ever written or updated, and the "
              "copies are compared by SHA256 with the export), reload once with F10 inside the game, capture a fresh "
              "frame, then compare every buffer XXMI draws with the exported files by content and check per component "
              "whether the game's own mesh was replaced. action='check' compares without deploying or reloading. "
              "action='status' shows what is deployed; action='remove' deletes the deployed files and reloads. "
              "Results are reported separately: export_valid, deployment_verified, reload (requested / confirmed), "
              "fresh_capture and target_mod_status (passed, failed, inconclusive, not_run) with what was and was not "
              "covered. Other installed mods are never moved or disabled; ones that refer to the same hashes are listed "
              "as possible conflicts. The character has to be on screen. Runs as a job.",
              {"project_dir": PROJECT,
               "export_id": {"type": "string", "default": "latest"},
               "action": {"type": "string", "enum": ["verify", "check", "status", "remove"], "default": "verify"},
               "deploy_dir": {"type": "string", "default": "",
                              "description": "Folder under <EFMI>/Mods, absolute or relative to it (default EFMI-MCP/<project>)."},
               "settle_ms": {"type": "integer", "minimum": 250, "maximum": 10000, "default": 2000},
               "check_textures": {"type": "boolean", "default": True},
               "wait_s": WAIT},
              ["project_dir"], read_only=False, annotations={"destructiveHint": False, "idempotentHint": False})
    def efmi_verify_exported_mod(project_dir, export_id="latest", action="verify", deploy_dir="", settle_ms=2000,
                                 check_textures=True, wait_s=50):
        root = root_of(project_dir)
        project = Project(root)
        try:
            project.require_workspace()
            target = mod_runtime.resolve_target(s.efmi_root, deploy_dir, root.name)
            if action == "status":
                return mod_runtime.deployment_status(target)
            if action == "remove":
                def run(job):
                    job.step("removing the deployed files")
                    removed = mod_runtime.remove_deployment(target)
                    s.mods.invalidate()
                    job.step("reloading (F10 inside the game)")
                    try:
                        reload = reload_and_capture(s, detail="light", cb_data=False, settle_ms=settle_ms)
                    except ToolError as exc:
                        reload = json.loads(str(exc))
                    removed["reload"] = {key: reload.get(key) for key in ("status", "reason", "reload_requested", "reload_confirmed")}
                    project.note("deployment_removed", target=str(target))
                    return removed
                return start("remove", f"remove deployment of {root.name}", run, wait_s, True)
            _, manifest = model_workflow.latest_export(project, export_id)
        except WorkflowError as exc:
            raise _fail(exc)
        return start("verify", f"{action} export {manifest['export_id']} of {root.name}",
                     lambda job: verify(job, project, manifest, action, target, settle_ms, check_textures), wait_s, True)

    # ================================================================ project + jobs
    @srv.tool("efmi_model_project",
              "What a model project folder holds: the .blend and when it was saved, the LoD levels its profile maps, "
              "its exports and the recent history. Works without the game.",
              {"project_dir": PROJECT}, ["project_dir"])
    def efmi_model_project(project_dir):
        try:
            described = model_workflow.describe_project(root_of(project_dir), s.loyaltools_root)
        except (WorkflowError, BridgeError, ValueError) as exc:
            raise _fail(exc)
        described["loyaltools"] = s.loyaltools()
        return described

    @srv.tool("efmi_model_job",
              "Follows or cancels a model job started by efmi_extract_model, efmi_map_lod, efmi_export_mod or "
              "efmi_verify_exported_mod. action='status' returns the stage and, once finished, the result or the "
              "error; 'wait' waits up to wait_s for it to finish; 'cancel' stops it between steps and ends a running "
              "Blender (partial files stay on disk for inspection). Without job_id the most recent job is used.",
              {"job_id": {"type": "string", "default": ""},
               "action": {"type": "string", "enum": ["status", "wait", "cancel"], "default": "status"},
               "wait_s": WAIT},
              read_only=False)
    def efmi_model_job(job_id="", action="status", wait_s=50):
        job = s.jobs.get(job_id) if job_id else s.jobs.latest()
        if job is None:
            raise ToolError("No such job." if job_id else "No model job has been started in this session.")
        if action == "cancel":
            job.cancel()
            job.thread.join(timeout=15)
        elif action == "wait":
            job.thread.join(timeout=max(0, wait_s))
        return job.describe(messages=20)
