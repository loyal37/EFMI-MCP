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
from collections import Counter, defaultdict
from pathlib import Path

from .shader import disassemble
from .shader_semantics import analyze

ROLES = ("diffuse", "lightmap", "normal")
SCHEMA = 3
RULES = {
    "analysis": "Per IB/count and VS/PS/output pass, from game-origin bindings and static component provenance only.",
    "roles": "Candidates: diffuse = UV RGB tint/transfer; normal = signed XY and sqrt(1-dot(XY,XY)); "
             "lightmap = independently used UV channels entering normal/shadow-dependent arithmetic. "
             "Observed uses do not prove authored texture names or runtime paths.",
    "primary": "Primary candidates require bound comparison-shadow/cube sampling plus texture-normal or packed-lighting evidence. "
               "Color-only lighting passes are insufficient to identify a main surface-material pass. "
               "No selection by slot number, count, format or mod agreement. Keep each primary pipeline variant; "
               "ambiguity withholds only the affected role. A shared mapping requires agreement across variants.",
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
    """Only roles independently resolved to the same slot in every primary variant."""
    candidates = _primary_passes(passes)
    roles = {}
    for role in ROLES:
        slots = {(p["roles"][role] or {}).get("slot") for p in candidates}
        roles[role] = candidates[0]["roles"][role] if len(slots) == 1 and None not in slots else None
    if not any(roles.values()):
        return None
    return {"roles": roles, "pixel_shaders": sorted({p["ps"] for p in candidates}),
            "basis": "per-role agreement across observed primary pipeline candidates"}


def _primary_passes(passes):
    return [p for p in passes if p["category"] == "lighting_candidate"
            and (p["role_candidates"]["normal"] or p["role_candidates"]["lightmap"])]


def primary_variants_of(passes):
    """Compact primary-only evidence; never replace an ambiguous role with an INI guess."""
    variants = []
    for p in _primary_passes(passes):
        status = {r: "candidate" if p["roles"][r] else
                  "ambiguous" if len(p["role_candidates"][r]) > 1 else
                  "unstable_binding" if p["role_candidates"][r] else "no_role_evidence" for r in ROLES}
        variants.append({**{k: p[k] for k in ("ps", "vs", "targets", "depth", "roles", "role_candidates")},
                         "role_status": status})
    return variants


def primary_state(passes, variants):
    if not variants:
        reason = ("shader_analysis_unavailable" if any(not p["reads_known"] for p in passes) else
                  "only_depth_passes_observed" if all(not p["targets"] for p in passes) else
                  "color_only_lighting_evidence" if any(p["category"] == "lighting_candidate" for p in passes) else
                  "no_primary_lighting_evidence" if any(p["category"] == "material_outputs" for p in passes) else
                  "no_primary_material_evidence")
        return "unresolved", reason
    signatures = {tuple((tuple(v["role_candidates"][r]), (v["roles"][r] or {}).get("slot"))
                        for r in ROLES) for v in variants}
    if len(signatures) > 1:
        return "shader_variants", "per_pipeline_mappings"
    issues = {s for v in variants for s in v["role_status"].values()} & {"ambiguous", "unstable_binding"}
    if issues:
        status = "partial" if any(any(v["roles"].values()) for v in variants) else "ambiguous"
        return status, "ambiguous_roles" if "ambiguous" in issues else "unstable_bindings"
    return "candidate", "dataflow_candidates"


def coverage(rows):
    return {"observed_meshes": len(rows),
            "primary_evidence_meshes": sum(bool(r["primary_variants"]) for r in rows),
            "primary_variant_rows": sum(len(r["primary_variants"]) for r in rows),
            "shared_mapping_meshes": sum(r["primary"] is not None for r in rows),
            "status_counts": dict(sorted(Counter(r["primary_status"] for r in rows).items())),
            "reason_counts": dict(sorted(Counter(r["primary_reason"] for r in rows).items())),
            "note": "Counts are observed IB/count keys, not character counts or a complete roster."}


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
        variants = primary_variants_of(passes)
        status, reason = primary_state(passes, variants)
        rows.append({"ib": ib, "index_count": count, "records": len(observations), "passes": passes,
                     "primary": primary, "primary_variants": variants, "primary_status": status, "primary_reason": reason,
                     "first_observed_frame": min(r["frame"] for r in observations)})
    try:
        version = json.loads((folder / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        version = {}
    digest = hashlib.sha256(json.dumps({"rows": rows, "shaders": analyses}, sort_keys=True).encode()).hexdigest()
    return {"schema": SCHEMA, "version": version.get("key", folder.name), "generated_unix": int(time.time()),
            "records": len(records), "meshes": len(rows), "rows": rows, "shaders": analyses,
            "native_analysis_sha256": digest, "rules": RULES, "coverage": coverage(rows),
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
            variants = [{"ps": v["ps"], "vs": v["vs"], "targets": v["targets"], "depth": v["depth"],
                         "slot": (v["roles"][role] or {}).get("slot"), "status": v["role_status"][role]}
                        for v in row["primary_variants"]]
            differing = len({v["slot"] for v in variants if v["slot"] is not None}) > 1
            checks.append({"role": role, "ini_slots": sorted(bound), "native_candidate": expected,
                           "native_variants": variants,
                           "status": "requires_shader_context" if differing else "unresolved_native" if expected is None else
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
            "primary_variants": row["primary_variants"], "primary_status": row["primary_status"],
            "primary_reason": row["primary_reason"], "labels": _labels(table, row)}


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
    return {"schema": SCHEMA, "version": table["version"], "native_analysis_sha256": table["native_analysis_sha256"],
            "matching_meshes": len(selected), "rows": page if include_passes else [material_row(table, r) for r in page],
            "coverage": table["coverage"], "matching_coverage": coverage(selected),
            "mod_comparison": comparisons,
            "shader_evidence": evidence,
            "next_offset": offset + limit if offset + limit < len(selected) else None, "rules": table["rules"],
            "note": "Offline static evidence, no live visual/branch verification. No files written; refresh after INI edits."}


def _role_text(primary, role):
    item = (primary or {}).get("roles", {}).get(role)
    if item:
        return f"t{item['slot']} (candidate)"
    candidates = (primary or {}).get("role_candidates", {}).get(role, [])
    if candidates:
        slots = ", ".join(f"t{s}" for s in candidates)
        status = primary["role_status"][role].replace("_", " ")
        return f"unresolved: {slots} ({status})"
    return "unresolved"


def to_markdown(table):
    counts = table["coverage"]
    lines = [f"# Slot evidence table {table['version']}", "",
             f"{table['meshes']} IB/count meshes, {table['records']} game-origin records. "
             "Every pass is analyzed independently before reading mods. Roles are dataflow candidates; "
             "no register threshold/order, format, texture hash or mod binding assigns them. "
             "Game-origin draws may use patched shaders. Missing roles are not filled from INIs.", "",
             f"{counts['primary_evidence_meshes']} IB/count keys have primary-pass candidates "
             f"({counts['primary_variant_rows']} pipeline variants). "
             f"{table['meshes'] - counts['primary_evidence_meshes']} have unresolved primary-pass identity. "
             "These are capture/analysis counts, not character counts. Every captured key appears below, "
             "including unresolved entries. Unlabelled entries without primary evidence are in the expandable inventory.", "",
             "Normal means signed-XY hemisphere reconstruction; diffuse means UV RGB tint/transfer reaching color output; "
             "light map means a candidate packed lighting-parameter texture, not proof of its authored name. "
             "Loop limitations and conditional alternatives are retained in JSON. "
             "This table contains primary slots only. slot-evidence.json separately preserves every pass, "
             "instruction line, sample coordinate, channel use and output for audit.", ""]
    named, unnamed, unresolved = [], [], []
    for row in table["rows"]:
        (named if _labels(table, row) else unnamed if row["primary_variants"] else unresolved).append(row)
    comparison = table["mod_comparison"]
    if comparison["performed"]:
        lines += ["## Capture coverage by label", "",
                  "Names are attached only after native analysis. Unseen INI keys may be unobserved LODs, "
                  "outdated hashes or other draws; they do not establish a missing character. "
                  "A candidate is not live verification.", "",
                  "| label | captured IB/count keys | primary evidence | primary unresolved | INI keys not observed |",
                  "|---|---|---|---|---|"]
        labels = sorted({n for r in named for n in _labels(table, r)} |
                        {n for u in comparison["unseen_components"] for n in u["mods"]})
        for label in labels:
            members = [r for r in named if label in _labels(table, r)]
            count = sum(bool(r["primary_variants"]) for r in members)
            unseen = sum(label in u["mods"] for u in comparison["unseen_components"])
            lines.append(f"| {_cell(label)} | {len(members)} | {count} | {len(members) - count} | {unseen} |")
        lines.append("")
    for title, rows in (("IBs named by mods (labels attached after analysis)", named), ("Other observed IBs", unnamed)):
        lines += [f"## {title}", "", "Each line is one primary pipeline candidate. Distinct shader mappings are retained; "
                  "an ambiguous role does not hide other roles or the IB. No auxiliary-pass slots are listed.", "",
                  "| IB / indices | label | primary PS / VS | diffuse | light map | normal | status / reason |",
                  "|---|---|---|---|---|---|---|"]
        for row in rows:
            labels = _cell(", ".join(_labels(table, row)))
            for p in row["primary_variants"] or [None]:
                shaders = f"`{p['ps']}` / `{p['vs']}`" if p else "unresolved"
                reason = row["primary_reason"].replace("_", " ")
                lines.append(f"| `{row['ib']}` / {row['index_count']} | {labels} | {shaders} | "
                             f"{_role_text(p, 'diffuse')} | {_role_text(p, 'lightmap')} | {_role_text(p, 'normal')} | "
                             f"{row['primary_status']}: {reason} |")
        lines.append("")
    if unresolved:
        lines += ["## Captured but not identified as primary materials", "",
                  "These unlabelled IBs are retained without assigning character identity or primary slots. "
                  "Repeated dynamic geometry can create many distinct IB hashes. Full bindings remain in slot-evidence.json.", "",
                  f"<details><summary>{len(unresolved)} captured IB/count keys with unresolved primary identity</summary>", "",
                  "| IB / indices | reason |", "|---|---|"]
        for row in unresolved:
            lines.append(f"| `{row['ib']}` / {row['index_count']} | {row['primary_reason'].replace('_', ' ')} |")
        lines += ["", "</details>", ""]
    return "\n".join(lines) + "\n"


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def export(table, folder):
    folder = Path(folder)
    material = {k: table[k] for k in ("schema", "version", "records", "meshes", "native_analysis_sha256", "rules", "mod_comparison", "coverage")}
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
                 "Returns primary slots per pipeline variant, unresolved reasons and capture coverage by default; "
                 "include_evidence/include_passes expose the audit. Refresh after INI edits.",
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
