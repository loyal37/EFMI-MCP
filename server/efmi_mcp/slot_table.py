"""Evidence-first per-IB slot tables; native analysis precedes optional mod comparison.

No texture hash, register number/order, format or mod declaration assigns a role.
MCP queries reuse memory; only the CLI explicitly exports files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from .shader import disassemble
from .shader_semantics import analyze

ROLES = ("diffuse", "lightmap", "normal")
RULES = {
    "analysis": "Per IB/count and VS/PS/output pass, from game-origin bindings and static component provenance only.",
    "roles": "Candidates: diffuse = UV RGB tint/transfer; normal = signed XY and sqrt(1-dot(XY,XY)); "
             "lightmap = independently used UV channels entering normal/shadow-dependent arithmetic. "
             "Observed uses do not prove authored texture names or runtime paths.",
    "primary": "Primary candidates require bound comparison-shadow/cube sampling plus texture-normal or packed-lighting evidence. "
               "Color-only lighting passes are insufficient to identify a main surface-material pass. "
               "No selection by slot number, count, format or mod agreement. Conflicting pass mappings remain unresolved.",
    "mods": "INI labels and bindings are compared only after native analysis and cannot fill or change its roles.",
    "limits": "Game-origin draws may use patched shaders. Conditional replacement slots remain explicit. "
              "Loops/unsupported operations limit dataflow; only unaffected local role evidence is used.",
}


def load_records(folder):
    records = []
    with (Path(folder) / "draws.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            try:
                r = json.loads(line)
            except ValueError:
                continue  # The probe may be appending its final line.
            if isinstance(r, dict) and r.get("fmt") == 2:
                records.append(r)
    return records


def shader_reader(folder):
    cache = {}

    def read(ps):
        path = Path(folder) / "shaders" / f"{ps}-ps.bin"
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            signature = None
        if ps not in cache or cache[ps][0] != signature:
            try:
                if not re.fullmatch(r"[0-9a-fA-F]{16}", ps):
                    raise ValueError("missing or invalid pixel shader hash")
                result = analyze(disassemble(path.read_bytes()))
            except (OSError, ValueError) as e:
                result = {"resources": {}, "error": str(e), "limitations": ["shader analysis unavailable"]}
            cache[ps] = signature, result
        return cache[ps][1]
    return read


def _shape(view):
    return {k: view[k] for k in ("kind", "format", "view_format", "view_dim", "w", "h", "mips", "array", "stride") if k in view}


def _pass(records, analysis):
    first, resources = records[0], analysis["resources"]
    known = "error" not in analysis
    bound = defaultdict(list)
    for r in records:
        for v in r.get("ps_srv", []):
            bound[v["slot"]].append(v)
    slots = []
    for slot in sorted(set(bound) | set(resources)):
        info = resources.get(slot)
        if known and info is None:
            continue
        views = bound.get(slot, [])
        shapes = {json.dumps(_shape(v), sort_keys=True): _shape(v) for v in views}
        slots.append({"slot": slot, "bound_records": len(views), "observed_views": list(shapes.values()),
                      "texture_hashes": sorted({v["hash"] for v in views if v.get("hash")}),
                      "reference": "static" if info else "unknown",
                      "role_candidates": info["role_candidates"] if info else [],
                      "evidence_lines": sorted({n for paths in (info or {}).get("evidence", {}).values()
                                                 for path in paths for n in path["lines"]}),
                      "direct_2d_inputs": (info or {}).get("direct_2d_inputs", [])})
    by_slot = {s["slot"]: s for s in slots}
    roles, candidates = {}, {}
    for role in ROLES:
        eligible = [s for s in slots if role in s["role_candidates"] and s["bound_records"]]
        candidates[role] = [s["slot"] for s in eligible]
        only = eligible[0] if len(eligible) == 1 else None
        stable = only and only["bound_records"] == len(records) and len({
            (v.get("kind"), v.get("view_dim"), v.get("view_format") or v.get("format"))
            for v in only["observed_views"]}) == 1
        roles[role] = {"slot": only["slot"], "basis": "shader_dataflow_candidate"} if stable else None
    lighting = [s for s, info in resources.items() if by_slot.get(s, {}).get("bound_records")
                and any(q["comparison"] or q["dimension"] == "texturecube" for q in info["samples"])]
    outputs = sorted({o for i in resources.values() for o in i["output_components"]})
    category = ("analysis_unknown" if not known else "depth_or_discard" if not first.get("rt") else
                "lighting_candidate" if lighting and (candidates["diffuse"] or candidates["normal"]) else
                "material_outputs" if candidates["normal"] else "color_or_effect" if candidates["diffuse"] else "unresolved")
    return {"ps": first.get("ps"), "vs": first.get("vs"), "targets": first.get("rt", []), "depth": first.get("ds"),
            "observations": len(records), "reads_known": known, "category": category, "slots": slots,
            "roles": roles, "role_candidates": candidates, "lighting_evidence_slots": lighting,
            "output_components": outputs, "limitations": analysis.get("limitations", []),
            "unbound_references": [s["slot"] for s in slots if not s["bound_records"]]}


def primary_of(passes):
    candidates = [p for p in passes if p["category"] == "lighting_candidate"
                  and (p["role_candidates"]["normal"] or p["role_candidates"]["lightmap"])]
    signatures = {tuple((p["roles"][r] or {}).get("slot") for r in ROLES) for p in candidates}
    if len(signatures) != 1 or not any(s is not None for s in next(iter(signatures), ())):
        return None
    if any(len(p["role_candidates"][r]) > 1 for p in candidates for r in ROLES):
        return None
    return {"roles": candidates[0]["roles"], "pixel_shaders": sorted({p["ps"] for p in candidates}),
            "basis": "consistent dataflow candidates across observed lighting passes"}


def build(folder, reader=None):
    """Native-only computation: deliberately accepts no mod root, names or role map."""
    folder = Path(folder)
    records = [r for r in load_records(folder) if r.get("origin") == "game" and r.get("ib_state") == "ok" and r.get("ib")]
    reader = reader or shader_reader(folder)
    analyses = {ps: reader(ps) for ps in sorted({r.get("ps") or "-" for r in records})}
    meshes = defaultdict(list)
    for r in records:
        meshes[(r["ib"], r["index_count"])].append(r)
    rows = []
    for (ib, count), observations in sorted(meshes.items()):
        groups = defaultdict(list)
        for r in observations:
            groups[(r.get("ps") or "-", r.get("vs"), tuple(r.get("rt", [])), r.get("ds"))].append(r)
        passes = [_pass(rs, analyses[k[0]]) for k, rs in sorted(groups.items(), key=lambda kv: str(kv[0]))]
        primary = primary_of(passes)
        rows.append({"ib": ib, "index_count": count, "records": len(observations), "passes": passes,
                     "primary": primary, "primary_status": "candidate" if primary else "unresolved",
                     "first_observed_frame": min(r["frame"] for r in observations)})
    try:
        version = json.loads((folder / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        version = {}
    digest = hashlib.sha256(json.dumps({"rows": rows, "shaders": analyses}, sort_keys=True).encode()).hexdigest()
    return {"schema": 2, "version": version.get("key", folder.name), "generated_unix": int(time.time()),
            "records": len(records), "meshes": len(rows), "rows": rows, "shaders": analyses,
            "native_analysis_sha256": digest, "rules": RULES,
            "mod_comparison": {"performed": False, "meshes": {}, "unseen_components": []}}


def compare_mods(table, root):
    """Return a separate comparison, without changing native rows/shaders/digest."""
    if root is None:
        return table
    from .slot_table_mods import mod_names, mod_roles
    names, assigned = mod_names(root), mod_roles(root)
    comparison, seen = {}, set()
    for row in table["rows"]:
        key = row["ib"], row["index_count"]
        seen.add(key)
        if key not in names:
            continue
        checks = []
        native = (row["primary"] or {}).get("roles", {})
        for role, bound in assigned.get(key, {}).items():
            expected = (native.get(role) or {}).get("slot")
            checks.append({"role": role, "ini_slots": sorted(bound), "native_candidate": expected,
                           "status": "unresolved_native" if expected is None else
                                     "matches_candidate" if bound == {expected} else "differs_from_candidate"})
        comparison[f"{key[0]}/{key[1]}"] = {"mod_sections": [{"mod": n, "section": s} for n, s in names[key]], "checks": checks}
    return {**table, "mod_comparison": {"performed": True, "meshes": comparison,
             "unseen_components": [{"ib": k[0], "index_count": k[1], "mods": sorted({n for n, _ in names[k]})}
                                   for k in sorted(names.keys() - seen)],
             "note": "INI resource names describe author intent, not shader evidence. Conditions/namespaces are not fully evaluated. "
                     "Agreement is a cross-check, not proof; no INI changes were made."}}


def _labels(table, row):
    m = table["mod_comparison"]["meshes"].get(f"{row['ib']}/{row['index_count']}", {})
    return sorted({s["mod"] for s in m.get("mod_sections", [])})


def material_row(table, row):
    return {"ib": row["ib"], "index_count": row["index_count"], "primary": row["primary"],
            "primary_status": row["primary_status"], "labels": _labels(table, row)}


def query(table, ib_hash="", character="", offset=0, limit=40, include_evidence=False, include_passes=False):
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 200")
    if ib_hash and not re.fullmatch(r"[0-9a-fA-F]{8}", ib_hash):
        raise ValueError("ib_hash must be eight hexadecimal digits")
    selected = [r for r in table["rows"] if (not ib_hash or r["ib"] == ib_hash.lower())
                and (not character or any(character.lower() in n.lower() for n in _labels(table, r)))]
    page = selected[offset:offset + limit]
    comparisons = {f"{r['ib']}/{r['index_count']}": table["mod_comparison"]["meshes"][f"{r['ib']}/{r['index_count']}"]
                   for r in page if f"{r['ib']}/{r['index_count']}" in table["mod_comparison"]["meshes"]}
    shaders = {p["ps"] or "-" for r in page for p in r["passes"]}
    evidence = {}
    if include_evidence:
        for s in sorted(shaders):
            analysis = table["shaders"][s]
            evidence[s] = {**analysis, "resources": {slot: {
                **info, "reference_count": len(info["references"]), "references": info["references"][:3],
                "sample_count": len(info["samples"]), "samples": info["samples"][:3],
                "evidence": {k: paths[:2] for k, paths in info["evidence"].items()}}
                for slot, info in analysis["resources"].items()},
                "paging_note": "First three references/samples and two paths per evidence kind; full data in explicit CLI export."}
    return {"schema": 2, "version": table["version"], "native_analysis_sha256": table["native_analysis_sha256"],
            "matching_meshes": len(selected), "rows": page if include_passes else [material_row(table, r) for r in page],
            "mod_comparison": comparisons,
            "shader_evidence": evidence,
            "next_offset": offset + limit if offset + limit < len(selected) else None, "rules": table["rules"],
            "note": "Offline static evidence, no live visual/branch verification. No files written; refresh after INI edits."}


def _role_text(primary, role):
    item = (primary or {}).get("roles", {}).get(role)
    if item:
        return f"t{item['slot']} (candidate)"
    return "unresolved"


def to_markdown(table):
    primary_count = sum(r["primary"] is not None for r in table["rows"])
    lines = [f"# Slot evidence table {table['version']}", "",
             f"{table['meshes']} IB/count meshes, {table['records']} game-origin records. "
             "Every pass is analyzed independently before reading mods. Roles are dataflow candidates; "
             "no register threshold/order, format, texture hash or mod binding assigns them. "
             "Game-origin draws may use patched shaders. Missing roles are not filled from INIs.", "",
             f"{primary_count} primary candidates are listed below; {table['meshes'] - primary_count} unresolved meshes "
             "remain explicitly marked in slot-table.json and slot-evidence.json.", "",
             "Normal means signed-XY hemisphere reconstruction; diffuse means UV RGB tint/transfer reaching color output; "
             "light map means a candidate packed lighting-parameter texture, not proof of its authored name. "
             "Loop limitations and conditional alternatives are retained in JSON. "
             "This table contains primary slots only. slot-evidence.json separately preserves every pass, "
             "instruction line, sample coordinate, channel use and output for audit.", ""]
    named, unnamed = [], []
    for row in table["rows"]:
        if row["primary"] is None:
            continue
        (named if _labels(table, row) else unnamed).append(row)
    for title, rows in (("IBs named by mods (labels attached after analysis)", named), ("Other observed IBs", unnamed)):
        lines += [f"## {title}", "", "| IB / indices | label | primary pixel shaders | diffuse | light map | normal | mod comparison |",
                  "|---|---|---|---|---|---|---|"]
        for row in rows:
            labels = ", ".join(_labels(table, row)).replace("|", "\\|")
            comparison = table["mod_comparison"]["meshes"].get(f"{row['ib']}/{row['index_count']}", {})
            checks = "; ".join(f"{c['role']}: {c['status']}" for c in comparison.get("checks", []))
            p = row["primary"]
            shaders = ", ".join(f"`{s}`" for s in p["pixel_shaders"]) if p else "unresolved"
            lines.append(f"| `{row['ib']}` / {row['index_count']} | {labels} | {shaders} | "
                         f"{_role_text(p, 'diffuse')} | {_role_text(p, 'lightmap')} | {_role_text(p, 'normal')} | {checks} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def export(table, folder):
    folder = Path(folder)
    material = {k: table[k] for k in ("schema", "version", "records", "meshes", "native_analysis_sha256", "rules", "mod_comparison")}
    material["rows"] = [material_row(table, r) for r in table["rows"]]
    (folder / "slot-table.json").write_text(json.dumps(material, indent=1), encoding="utf-8")
    (folder / "slot-table.md").write_text(to_markdown(table), encoding="utf-8")
    (folder / "slot-evidence.json").write_text(json.dumps(table, indent=1), encoding="utf-8")


class TableCache:
    def __init__(self):
        self.signature = None
        self.table = None
        self.folder = None
        self.reader = None

    def get(self, folder, root=None, refresh=False):
        folder = Path(folder).resolve()
        if folder != self.folder:
            self.folder, self.reader = folder, shader_reader(folder)
        paths = [folder / "draws.jsonl", folder / "version.json", *sorted((folder / "shaders").glob("*-ps.bin"))]
        signature = (str(folder), str(root), tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()))
        if refresh or signature != self.signature:
            native = build(folder, reader=self.reader)
            table = compare_mods(native, root)
            self.table, self.signature = table, signature
        return self.table


def register(server, session):
    cache = TableCache()

    @server.tool("efmi_get_slot_table",
                 "Read per-IB/per-pass shader dataflow evidence without contacting the game or writing files. "
                 "Native analysis is independent of mods; INI bindings are compared afterwards. Roles remain candidates. "
                 "Returns primary slots only by default; include_evidence/include_passes expose the audit. Refresh after INI edits.",
                 {"folder": {"type": "string", "description": "Absolute version folder containing draws.jsonl and shaders."},
                  "ib_hash": {"type": "string", "default": ""}, "character": {"type": "string", "default": ""},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 40},
                  "include_evidence": {"type": "boolean", "default": False},
                  "include_passes": {"type": "boolean", "default": False},
                  "refresh": {"type": "boolean", "default": False}}, required=["folder"])
    def efmi_get_slot_table(folder, ib_hash="", character="", offset=0, limit=40, include_evidence=False, include_passes=False, refresh=False):
        return query(cache.get(Path(folder), session.efmi_root, refresh), ib_hash, character, offset, limit, include_evidence, include_passes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder")
    parser.add_argument("--efmi-root", default="", help="Optional read-only mod comparison AFTER native analysis")
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    native = build(folder)
    table = compare_mods(native, Path(args.efmi_root) if args.efmi_root else None)
    export(table, folder)
    print(f"{table['meshes']} meshes, {len(table['shaders'])} shaders; native digest {table['native_analysis_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
