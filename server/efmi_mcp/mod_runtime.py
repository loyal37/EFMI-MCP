"""Deploying one export as a test mod and checking what the game then draws.

Deployment only ever writes inside one directory under <EFMI>/Mods that this
tool created and marked; everything else in the EFMI folder stays read-only.
Verification compares what XXMI actually draws after a reload with the bytes
of the exported files. A reload acknowledgement is not a verification of this
mod, and neither is the reverse.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .capture_model import Capture
from .hashes import crc32c, hex32
from .model_workflow import WorkflowError, sha256_file, write_json

MARKER = "efmi-mcp-deployment.json"
READ_BUDGET = 512 << 20          # bytes of whole buffers read back for one verification
INDEXED = ("DrawIndexed", "DrawIndexedInstanced")


# ---------------------------------------------------------------- deployment
def resolve_target(efmi_root: Path, target: str | Path, project_name: str) -> Path:
    """The directory to deploy to: inside <EFMI>/Mods, never the Mods folder itself."""
    mods = (Path(efmi_root) / "Mods").resolve()
    path = Path(target) if target else mods / "EFMI-MCP" / project_name
    if not path.is_absolute():
        path = mods / path
    path = path.resolve()
    if path == mods or mods not in path.parents:
        raise WorkflowError(f"The deployment directory must be a folder inside {mods}")
    for part in path.relative_to(mods).parts:
        if part.upper().startswith("DISABLED"):
            raise WorkflowError(f"XXMI skips folders named DISABLED*: {path}")
    return path


def _marker(target: Path) -> dict | None:
    path = target / MARKER
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def deployment_status(target: Path) -> dict:
    """What is deployed in ``target`` and whether its files still match the record."""
    marker = _marker(target)
    if marker is None:
        return {"target": str(target), "managed": False, "exists": target.exists(),
                "other_files": sorted(p.name for p in target.iterdir())[:10] if target.is_dir() else []}
    changed = [name for name, digest in marker["files"].items()
               if not (target / name).is_file() or sha256_file(target / name) != digest]
    known = set(marker["files"]) | {MARKER}
    foreign = [p.relative_to(target).as_posix() for p in sorted(target.rglob("*"))
               if p.is_file() and p.relative_to(target).as_posix() not in known]
    return {"target": str(target), "managed": True, "export_id": marker.get("export_id"),
            "project": marker.get("project"), "deployed": marker.get("deployed"), "files": len(marker["files"]),
            "changed_or_missing": changed, "files_not_from_this_tool": foreign}


def deploy_export(manifest: dict, target: Path, backup_dir: Path) -> dict:
    """Copy a valid export into ``target`` and prove the copies are identical.

    A new target is created and marked. An existing one is only touched when it
    carries this tool's marker: its recorded files are replaced or removed
    (moved to ``backup_dir``, outside XXMI's Mods folder), nothing else.
    """
    if not manifest.get("valid"):
        raise WorkflowError(f"Export {manifest.get('export_id')} did not pass its checks and is not deployed")
    source = Path(manifest["mod_dir"])
    wanted = {item["path"]: item for item in manifest["files"]}
    for item in manifest["files"]:
        if sha256_file(source / item["path"]) != item["sha256"]:
            raise WorkflowError(f"Exported file changed since the export was checked: {item['path']}")
    previous = _marker(target)
    if target.exists() and any(target.iterdir()) and previous is None:
        raise WorkflowError(f"{target} exists and was not created by this tool; choose another directory")
    replaced, removed = [], []
    if previous:
        known = set(previous["files"]) | {MARKER}
        foreign = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - known
        clash = sorted(foreign & set(wanted))
        if clash:
            raise WorkflowError(f"{target} holds files this tool did not deploy that the export would overwrite: {clash[:5]}")
        for name in previous["files"]:
            current = target / name
            if not current.is_file():
                continue
            if name in wanted and sha256_file(current) == wanted[name]["sha256"]:
                continue   # already the right bytes
            backup = backup_dir / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(backup))
            (replaced if name in wanted else removed).append(name)
        (target / MARKER).unlink()
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name, item in wanted.items():
        destination = target / name
        if destination.is_file() and sha256_file(destination) == item["sha256"]:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        copied += 1
    mismatched = [name for name, item in wanted.items() if sha256_file(target / name) != item["sha256"]]
    if mismatched:
        raise WorkflowError(f"Deployed files differ from the export: {mismatched[:5]}")
    record = {"tool": "efmi-mcp", "project": manifest["project"], "export_id": manifest["export_id"],
              "deployed": time.strftime("%Y-%m-%d %H:%M:%S"),
              "files": {name: item["sha256"] for name, item in wanted.items()}}
    # Written last: a target without it is incomplete and is not reported as deployed.
    write_json(target / MARKER, record)
    return {"target": str(target), "export_id": manifest["export_id"], "files": len(wanted), "copied": copied,
            "bytes": sum(item["bytes"] for item in manifest["files"]), "replaced_previous": replaced,
            "removed_previous": removed, "backup": str(backup_dir) if (replaced or removed) else None,
            "verified_identical": True, "deployed": record["deployed"]}


def remove_deployment(target: Path) -> dict:
    """Delete the files this tool deployed, and the folder when nothing else is in it."""
    marker = _marker(target)
    if marker is None:
        raise WorkflowError(f"{target} was not deployed by this tool; nothing was removed")
    removed = 0
    for name in marker["files"]:
        path = target / name
        if path.is_file():
            path.unlink()
            removed += 1
    (target / MARKER).unlink()
    for folder in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    left = [p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()]
    if not left:
        target.rmdir()
        # The EFMI-MCP container folder is this tool's as well.
        if target.parent.name == "EFMI-MCP" and not any(target.parent.iterdir()):
            target.parent.rmdir()
    return {"target": str(target), "removed_files": removed, "left_in_place": left,
            "note": "XXMI keeps the mod loaded until the next reload."}


def find_conflicts(manifest: dict, mod_index, target: Path, efmi_root: Path) -> list[dict]:
    """Other active mod INIs that refer to the same entry hashes.

    The same hash is not a conflict by itself: match conditions decide. These
    are listed so a result is not mistaken for this export's when another mod
    answers for the same mesh.
    """
    try:
        inside = target.resolve().relative_to(Path(efmi_root).resolve()).as_posix().lower() + "/"
    except ValueError:
        inside = None
    conflicts = []
    for entry in manifest.get("entry_points", []):
        for ref in mod_index.by_hash.get(entry["hash"], []):
            if inside and ref.ini.replace("\\", "/").lower().startswith(inside):
                continue
            conflicts.append({"hash": entry["hash"], "our_section": entry["section"], "other_mod": ref.mod,
                              "other_ini": ref.ini, "other_section": ref.section, "other_match": ref.match})
    return conflicts


# ---------------------------------------------------------------- verification
def verify_when_drawn(first: Capture, another, client, manifest: dict, baseline_counts: dict[str, int] | None = None,
                      looks: int = 6, pause_s: float = 0.5, sleep=time.sleep) -> tuple[Capture, dict]:
    """verify_capture, looking at later frames while nothing of the mod is drawn yet.

    Right after a reload a mod may not draw for some frames, whether or not it
    already skips the game's mesh: its buffers and textures are loaded on first
    use, and a merged skeleton is built first. ``another()`` returns the next
    capture. Returns the capture that was judged and its result.
    """
    cap, checked, count = first, verify_capture(first, client, manifest, baseline_counts), 1
    while not checked["draws_of_this_mod"] and count < looks:
        sleep(pause_s)
        cap = another()
        checked = verify_capture(cap, client, manifest, baseline_counts)
        count += 1
    checked["frames_looked_at"] = count
    return cap, checked


def _matches(draw: dict, entry: dict) -> bool:
    ib, args = draw.get("ib") or {}, draw["args"]
    return (ib.get("hash") == entry["hash"]
            and ("match_index_count" not in entry or args.get("index_count") == entry["match_index_count"])
            and ("match_first_index" not in entry or args.get("first_index", 0) == entry["match_first_index"]))


def entry_draw_counts(cap: Capture, entries: list[dict]) -> dict[str, int]:
    """How often the game itself draws each entry-point mesh in a frame."""
    counts = {entry["section"]: 0 for entry in entries}
    for draw in cap.draws():
        if draw["origin"] == "xxmi" or draw["call"] not in INDEXED:
            continue
        for entry in entries:
            if _matches(draw, entry):
                counts[entry["section"]] += 1
    return counts


def verify_capture(cap: Capture, client, manifest: dict, baseline_counts: dict[str, int] | None = None) -> dict:
    """Compare what XXMI draws in ``cap`` with the exported files.

    Every indexed draw issued by XXMI is tried against the exported buffers:
    a bound buffer belongs to an exported file when it has the file's size and
    the bytes the draw reads have the same CRC as that range of the file. The
    whole buffers behind matched draws are then read back and compared by
    SHA256. Draw arguments are checked against the INI's draw commands.

    A vertex buffer of such a draw that has an exported file's size but was
    recorded with another hash is decided by reading it: the hash a draw is
    recorded with can be older than the buffer, its bytes cannot.
    """
    mod_dir = Path(manifest["mod_dir"])
    digests = {item["path"]: item for item in manifest["files"]}
    by_size: dict[int, list[str]] = {}
    for item in manifest["files"]:
        if not item["path"].lower().endswith((".ini", ".dds", ".md", ".txt", ".png", ".jpg")):
            by_size.setdefault(item["bytes"], []).append(item["path"])
    file_bytes: dict[str, bytes] = {}

    def data(name):
        if name not in file_bytes:
            file_bytes[name] = (mod_dir / name).read_bytes()
        return file_bytes[name]

    def match_region(binding):
        """The exported file a bound buffer range is identical to, with what was compared."""
        region = (binding or {}).get("region")
        resource = cap.resource(binding["res"]) if binding else None
        if not region or region["state"] != "ok" or not region["hash"] or not resource:
            return None, "no_region_hash", None
        candidates = by_size.get(resource.get("size"), [])
        for name in candidates:
            chunk = data(name)[region["offset"]:region["offset"] + region["size"]]
            if len(chunk) == region["size"] and hex32(crc32c(chunk)) == region["hash"]:
                return name, "identical", region
        return None, ("same_size_different_bytes" if candidates else "not_an_exported_buffer"), region

    ini_draws = {}   # exported index file -> the INI's draw commands that read it
    for draw in manifest.get("draws", []):
        if draw.get("ib_file"):
            ini_draws.setdefault(draw["ib_file"], []).append(draw)
    used_files: dict[str, dict] = {}
    mod_draws, foreign_draws, stale = [], 0, []
    resources_to_read: dict[int, str] = {}
    undecided = []      # vertex bindings of this mod's draws: an exported file's size, another recorded hash
    xxmi_indexed = 0
    for draw in cap.draws():
        if draw["origin"] != "xxmi" or draw["call"] not in INDEXED or "ib" not in draw:
            continue
        xxmi_indexed += 1
        name, state, region = match_region(draw["ib"])
        if name is None:
            if state == "same_size_different_bytes":
                stale.append({"draw": draw["index"], "ib_bytes": cap.resource(draw["ib"]["res"]).get("size"),
                              "index_count": draw["args"].get("index_count")})
            else:
                foreign_draws += 1
            continue
        args = draw["args"]
        record = {"draw": draw["index"], "index_file": name, "index_count": args.get("index_count"),
                  "first_index": args.get("first_index", 0), "base_vertex": args.get("first_vertex", 0),
                  "instances": args.get("instance_count", 1), "vertex_buffers": {}}
        used_files.setdefault(name, {"draws": 0, "resource": draw["ib"]["res"]})["draws"] += 1
        resources_to_read[draw["ib"]["res"]] = name
        for slot, binding in sorted(draw.get("vb", {}).items()):
            vb_name, vb_state, _ = match_region(binding)
            record["vertex_buffers"][f"vb{slot}"] = vb_name or vb_state
            if vb_name:
                used_files.setdefault(vb_name, {"draws": 0, "resource": binding["res"]})["draws"] += 1
                resources_to_read[binding["res"]] = vb_name
            elif vb_state == "same_size_different_bytes":
                undecided.append((record, f"vb{slot}", binding))
        mod_draws.append(record)

    # Whole buffers: the draw ranges above need not cover a file completely.
    budget, whole = READ_BUDGET, {}
    read: dict[int, tuple[str | None, str]] = {}

    def digest_now(rid):
        """SHA256 of a whole buffer as it is now, or why it was not read."""
        nonlocal budget
        if rid not in read:
            size = cap.resource(rid)["size"]
            if size > budget:
                read[rid] = (None, "not_read_budget")
            else:
                budget -= size
                try:
                    _, payload = client.read_buffer(cap.frame, rid, size=size)
                    read[rid] = (hashlib.sha256(payload).hexdigest(), "read")
                except Exception as exc:   # the capture may have been released meanwhile
                    read[rid] = (None, f"read_failed: {exc}")
        return read[rid]

    for rid, name in sorted(resources_to_read.items()):
        digest, state = digest_now(rid)
        whole[name] = state if digest is None else "identical" if digest == digests[name]["sha256"] else "different"

    hash_disagreements, other_bytes = [], []
    for record, slot, binding in undecided:
        rid = binding["res"]
        digest, _ = digest_now(rid)
        name = next((n for n in by_size.get(cap.resource(rid)["size"], []) if digests[n]["sha256"] == digest), None)
        if name:
            record["vertex_buffers"][slot] = name
            used_files.setdefault(name, {"draws": 0, "resource": rid})["draws"] += 1
            whole[name] = "identical"
            if not any(item["resource"] == rid for item in hash_disagreements):
                hash_disagreements.append({"resource": rid, "file": name, "recorded_hash": binding["region"]["hash"]})
        elif digest:
            other_bytes.append({"draw": record["draw"], "slot": slot, "bytes": cap.resource(rid)["size"]})

    # Draw arguments against the INI's draw commands for the same index file.
    argument_mismatches = []
    for record in mod_draws:
        expected = ini_draws.get(record["index_file"], [])
        if expected and not any(d["index_count"] == record["index_count"] and d["first_index"] == record["first_index"]
                                and d["base_vertex"] == record["base_vertex"] for d in expected):
            argument_mismatches.append({"draw": record["draw"], "index_file": record["index_file"],
                                        "drawn": [record["index_count"], record["first_index"], record["base_vertex"]],
                                        "ini": [[d["index_count"], d["first_index"], d["base_vertex"]] for d in expected]})

    # Is the game's own mesh still drawn, and is ours drawn instead. Entry points
    # that lead to the same exported files (the full-detail entry of a component
    # and its LoD entries) are one group: they share one edited mesh.
    after_counts = entry_draw_counts(cap, manifest.get("entry_points", []))
    components = {}
    for entry in manifest.get("entry_points", []):
        files = tuple(entry.get("index_files") or ())
        state = components.setdefault(files or ("", entry["section"]), {
            "name": entry["section"], "index_files": list(files), "entry_points": []})
        state["entry_points"].append({"section": entry["section"], "entry_hash": entry["hash"],
                                      "game_draws_before": (baseline_counts or {}).get(entry["section"]),
                                      "game_draws_after": after_counts[entry["section"]]})
    for state in components.values():
        names = set(state["index_files"])
        drawn = [r for r in mod_draws if r["index_file"] in names]
        state["mod_draws"] = len(drawn)
        state["exports_geometry"] = bool(names)
        still_original = any(level["game_draws_after"] for level in state["entry_points"])
        seen_before = any(level["game_draws_before"] for level in state["entry_points"])
        if drawn and not still_original:
            state["status"] = "replaced"
        elif still_original:
            state["status"] = "original_still_drawn"
        elif not names and (seen_before or baseline_counts is None):
            # The add-on kept an entry point without a draw: the mesh was removed
            # in the blueprint or is left to the game by design.
            state["status"] = "hidden_or_left_to_game_by_export"
        else:
            state["status"] = "not_observed"

    statuses = [state["status"] for state in components.values()]
    problems = []
    if stale:
        problems.append(f"{len(stale)} XXMI draw(s) use a buffer with an exported file's size but other bytes: "
                        "an older deployment or another mod is being drawn")
    if any(value == "different" for value in whole.values()):
        problems.append("a buffer bound by a matched draw differs from its exported file outside the drawn range")
    if argument_mismatches:
        problems.append(f"{len(argument_mismatches)} draw(s) use other arguments than the INI's draw commands")
    # vb0 is the stream the INI binds from a file; other slots may hold what the game left bound.
    wrong_positions = [item for item in other_bytes if item["slot"] == "vb0"]
    if wrong_positions:
        problems.append(f"{len(wrong_positions)} draw(s) of this mod read a vb0 with an exported file's size but other bytes")
    if "original_still_drawn" in statuses:
        problems.append("the game's own mesh is still drawn for: " + ", ".join(
            s["name"] for s in components.values() if s["status"] == "original_still_drawn"))
    if problems:
        target = "failed"
    elif "replaced" in statuses:
        target = "passed"
    else:
        target = "inconclusive"
        problems.append("no component of this mod was seen being drawn" if xxmi_indexed else
                        "XXMI issued no indexed draws in this frame; the character may not be in view")
    geometry_files = [item["path"] for item in manifest["files"] if item["bytes"] in by_size and
                      item["path"] in by_size[item["bytes"]]]
    return {
        "target_mod_status": target, "reasons": problems, "frame": cap.frame,
        "xxmi_indexed_draws": xxmi_indexed, "draws_of_this_mod": len(mod_draws),
        "xxmi_draws_of_other_content": foreign_draws, "stale_or_foreign_same_size": stale[:10],
        "components": list(components.values()),
        "covered": sorted(s["name"] for s in components.values() if s["status"] == "replaced"),
        "not_covered": sorted(f"{s['name']} ({s['status']})" for s in components.values() if s["status"] != "replaced"),
        "exported_buffers_seen": {name: {**info, "whole_buffer": whole.get(name, "not_read")}
                                  for name, info in sorted(used_files.items())},
        "exported_buffers_not_seen": sorted(set(geometry_files) - set(used_files)),
        "argument_mismatches": argument_mismatches[:10], "mod_draws": mod_draws[:200],
        # Buffers whose bytes are an exported file's although the draw was recorded with another hash.
        "recorded_hash_disagreements": hash_disagreements,
        "vertex_bindings_with_other_bytes": other_bytes[:20],
        "scope": ("Geometry buffers by content. Buffers the mod computes at run time (skinned positions, bone data) "
                  "and render-state are not compared; LoD levels and branches that were not on screen are not covered."),
    }


def verify_textures(cap: Capture, client, manifest: dict, limit: int = 24) -> dict:
    """Compare textures bound by XXMI-created draws with the exported DDS files, mip 0."""
    mod_dir = Path(manifest["mod_dir"])
    files = {}
    for item in manifest["files"]:
        if not item["path"].lower().endswith(".dds"):
            continue
        raw = (mod_dir / item["path"]).read_bytes()
        if raw[:4] != b"DDS " or len(raw) < 128:
            continue
        height, width = int.from_bytes(raw[12:16], "little"), int.from_bytes(raw[16:20], "little")
        header = 148 if raw[84:88] == b"DX10" else 128
        files[item["path"]] = (width, height, raw[header:])
    bound = {}
    for draw in cap.draws():
        if draw["origin"] != "xxmi":
            continue
        for slot, binding in draw.get("tex", {}).items():
            resource = cap.resource(binding["res"])
            if resource and resource.get("kind") == "tex2d" and resource.get("origin") == "xxmi":
                bound.setdefault(binding["res"], set()).add(slot)
    results, unmatched = {}, 0
    for rid, slots in sorted(bound.items())[:limit]:
        resource = cap.resource(rid)
        candidates = [name for name, (w, h, _) in files.items() if (w, h) == (resource.get("w"), resource.get("h"))]
        if not candidates:
            unmatched += 1
            continue
        try:
            _, payload = client.read_texture(cap.frame, rid)
        except Exception as exc:
            results[f"res{rid}"] = {"slots": sorted(slots), "state": f"read_failed: {exc}"}
            continue
        match = next((name for name in candidates if files[name][2][:len(payload)] == payload), None)
        results[match or f"res{rid}"] = {"slots": sorted(slots), "width": resource.get("w"), "height": resource.get("h"),
                                         "state": "identical_mip0" if match else "no_exported_file_has_these_pixels"}
    seen = {name for name, info in results.items() if info["state"] == "identical_mip0"}
    return {"textures_identical": sorted(seen), "exported_textures_not_seen": sorted(set(files) - seen),
            "other": {name: info for name, info in results.items() if info["state"] != "identical_mip0"},
            "bound_textures_of_other_sizes": unmatched,
            "scope": "Top mip of textures XXMI created and bound in its draws; file container versus GPU pixel bytes."}
