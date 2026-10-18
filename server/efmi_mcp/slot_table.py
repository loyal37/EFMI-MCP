"""The slot table of one game version, built from the probe's slot cache (T-016).

A row is a mesh as mods name it, the index buffer region hash and the index count, with the three
slots a mod binds for it: diffuse, light map and normal map. They are those of the mesh's main
pass: the forward pass whose pixel shader samples shadows or an environment cube (the lit pass),
else another forward pass with the most material textures; between equal passes the one whose slots
come first, as LoyalTools' texture marking prefers. Depth passes and G-buffer passes
(deferred shading, three targets or more) are never the main pass. A pass whose shader is
another variant of the main one (XXMI's patched copy, for instance) and reads the same three slots
is folded into the row.

Every slot is decided from the game's own data, so that the table of a new version does not lean
on mods written for the old one:

  dataflow           the saved bytecode's arithmetic (shader_semantics): signed XY with the
                     hemisphere reconstruction for a normal map, an interpolated RGB sample tinted
                     on its way to the output for a diffuse, independent channels feeding the
                     lighting for a light map; only slots the shader reads and that hold a
                     material texture count
  dataflow+position  the dataflow names several slots for a role: the one nearest to the slots of
                     the other roles
  dataflow+format    several slots, equally near: the one the formats name
  sequence           LoyalTools' material sequence next to roles already found: the diffuse right
                     before the light map (or the normal map), the normal map right after the light map
  format             the formats of the material textures: diffuse the first sRGB one, normal map
                     the BC5 one, light map the linear one after the diffuse, all side by side
  shader             the same pixel shader resolved that role on other meshes
  order              only the order of the slots says it (uncertain)

The installed mods are compared afterwards: whether the slots a mod binds resources named
DiffuseMap, LightMap or NormalMap to agree with the table. They never change a slot.

A material texture is a block-compressed 2D texture bound together with few other material
textures (a character's own); one bound together with the materials of many characters is global
(ramps, noise, lookup textures) and never one of the three.

Meshes are grouped by character where a mod names them; the rest, without certainty, by the
looked-at frame in which the probe first saw them; what fits neither is listed on its own.

    python -m efmi_mcp.slot_table <slot-cache version folder> [--efmi-root PATH]
writes slot-table.json and slot-table.md into that folder.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

ROLES = ("diffuse", "lightmap", "normal")
SCHEMA = 4
GLOBAL_COOCCURRENCE = 60   # a texture bound together with more material textures than this is global
SCENE_MESHES = 40          # a frame that brought more new meshes than this showed a whole scene
GROUP_MIN = 3              # fewer new meshes than this in a frame are not taken for a character
DEFAULT_EFMI = r"D:\XXMI-Launcher-Portable-v2.1.6\EFMI"
SURE = ("dataflow", "dataflow+position", "dataflow+format", "sequence", "format")


# ---------------------------------------------------------------- input
def load_records(folder: Path) -> list[dict]:
    """The records of the current format; a line the probe is still writing is skipped."""
    out = []
    with (Path(folder) / "draws.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.startswith('{"fmt":2,'):
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def shader_reader(folder: Path) -> Callable[[str], dict]:
    """The dataflow analysis of a pixel shader's saved bytecode, cached; {"error": ...} when there is none."""
    from .shader import disassemble
    from .shader_semantics import analyze

    cache: dict[str, tuple] = {}

    def read(ps: str) -> dict:
        path = Path(folder) / "shaders" / f"{ps}-ps.bin"
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            signature = None
        if ps not in cache or cache[ps][0] != signature:
            try:
                if not re.fullmatch(r"[0-9a-f]{16}", ps or ""):
                    raise ValueError("no pixel shader hash")
                result = analyze(disassemble(path.read_bytes()))
            except (OSError, ValueError) as e:
                result = {"resources": {}, "error": str(e)}
            cache[ps] = signature, result
        return cache[ps][1]

    return read


def _block_texture(view: dict) -> bool:
    return view.get("kind") == "tex2d" and str(view.get("format", "")).upper().startswith("BC") and bool(view.get("hash"))


def texture_classes(records: list[dict]) -> dict[str, str]:
    """material / global for every block-compressed texture the game bound."""
    seen_with: dict[str, set] = defaultdict(set)
    for r in records:
        hashes = {v["hash"] for v in r.get("ps_srv", []) if _block_texture(v)}
        for h in hashes:
            seen_with[h] |= hashes - {h}
    return {h: ("global" if len(s) > GLOBAL_COOCCURRENCE else "material") for h, s in seen_with.items()}


# ---------------------------------------------------------------- passes
def _slot_entry(view: dict, classes: dict[str, str], info: dict | None) -> dict:
    entry = {"slot": view["slot"], "class": classes.get(view.get("hash"), "engine") if _block_texture(view) else "engine",
             "kind": view.get("kind")}
    for key in ("format", "w", "h", "mips", "size", "stride", "hash"):
        if view.get(key) is not None:
            entry["texture" if key == "hash" else key] = view[key]
    if info is not None:
        entry["candidates"] = list(info.get("role_candidates", []))
    return entry


def _passes(records: list[dict], classes: dict[str, str], reader: Callable[[str], dict]) -> list[dict]:
    """One per pixel shader the mesh is drawn with; the slots it reads that something is bound to."""
    by_ps: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_ps[r.get("ps") or "-"].append(r)
    passes = []
    for ps, recs in sorted(by_ps.items()):
        analysis = reader(ps) if ps != "-" else {"resources": {}, "error": "no pixel shader"}
        known = "error" not in analysis
        resources = analysis.get("resources", {})
        slots: dict[int, dict] = {}
        differs = set()
        for r in recs:
            for view in r.get("ps_srv", []):
                if known and view["slot"] not in resources:
                    continue                 # bound, but this shader does not read it (left by an earlier draw)
                entry = _slot_entry(view, classes, resources.get(view["slot"]))
                old = slots.get(view["slot"])
                if old and (old.get("format"), old.get("kind")) != (entry.get("format"), entry.get("kind")):
                    differs.add(view["slot"])
                slots.setdefault(view["slot"], entry)
        lit = any(q.get("comparison") or q.get("dimension") == "texturecube"
                  for s, info in resources.items() if s in slots for q in info.get("samples", []))
        first = recs[0]
        item = {"ps": ps, "vs": first.get("vs"), "targets": first.get("rt", []), "depth": first.get("ds"),
                "reads_known": known, "lit": lit, "slots": [slots[s] for s in sorted(slots)]}
        if known:
            item["read_but_unbound"] = sorted(s for s in resources if s not in slots)
        if differs:
            item["differs_between_records"] = sorted(differs)
        passes.append(item)
    return passes


# ---------------------------------------------------------------- roles, from the game's own data
def _fits(role: str, fmt: str | None) -> bool:
    f = (fmt or "").upper()
    if role == "diffuse":
        return f.endswith("_SRGB")
    return bool(f) and not f.endswith("_SRGB") and "FLOAT" not in f and not f.startswith("BC6H")


def _roles_from_formats(slots: list[dict]) -> tuple[dict[str, int | None], str]:
    material = [s for s in slots if s["class"] == "material"]
    diffuse = next((s["slot"] for s in material if str(s.get("format", "")).upper().endswith("_SRGB")), None)
    after = [s for s in material if diffuse is None or s["slot"] > diffuse]
    normal = next((s["slot"] for s in after if str(s.get("format", "")).upper().startswith("BC5")), None)
    linear = [s["slot"] for s in after if s["slot"] != normal and _fits("lightmap", s.get("format"))]
    lightmap = linear[0] if linear else None
    basis = "format"
    if normal is None and len(linear) >= 2:
        lightmap, normal = linear[0], linear[1]
        basis = "order"
    found = [x for x in (diffuse, lightmap, normal) if x is not None]
    if found and max(found) - min(found) > len(found) - 1:
        basis = "order"                          # not side by side: a shader with more material textures
    return {"diffuse": diffuse, "lightmap": lightmap, "normal": normal}, basis


def _native_roles(p: dict) -> tuple[dict[str, int | None], dict[str, str]]:
    material = [s for s in p["slots"] if s["class"] == "material"]
    chosen: dict[str, int] = {}
    basis: dict[str, str] = {}
    candidates = {r: [s["slot"] for s in material if r in s.get("candidates", [])] for r in ROLES}
    for role in ROLES:
        if len(candidates[role]) == 1:
            chosen[role], basis[role] = candidates[role][0], "dataflow"
    for role in ROLES:
        if role in chosen or len(candidates[role]) < 2 or not chosen:
            continue
        free = [c for c in candidates[role] if c not in chosen.values()]
        distance = {c: min(abs(c - a) for a in chosen.values()) for c in free}
        if distance:
            best = min(distance.values())
            nearest = [c for c, d in distance.items() if d == best]
            if len(nearest) == 1:
                chosen[role], basis[role] = nearest[0], "dataflow+position"
    # LoyalTools' material sequence fills a gap next to roles already found: the diffuse is the sRGB
    # material texture right before the light map (or before the normal map when there is no light
    # map), and the normal map the linear one right after the light map.
    by_slot = {s["slot"]: s for s in material}
    srgb = lambda s: str(by_slot[s].get("format", "")).upper().endswith("_SRGB")  # noqa: E731
    if "diffuse" not in chosen:
        after = chosen.get("lightmap", chosen.get("normal"))
        if after is not None and after - 1 in by_slot and srgb(after - 1) and after - 1 not in chosen.values():
            chosen["diffuse"], basis["diffuse"] = after - 1, "sequence"
    if "normal" not in chosen and "diffuse" in chosen and chosen.get("lightmap") == chosen["diffuse"] + 1:
        n = chosen["lightmap"] + 1
        if n in by_slot and _fits("normal", by_slot[n].get("format")) and n not in chosen.values():
            chosen["normal"], basis["normal"] = n, "sequence"
    guess, how = _roles_from_formats(p["slots"])
    for role in ROLES:
        g = guess[role]
        if role in chosen or g is None or g in chosen.values():
            continue
        if not candidates[role]:
            chosen[role], basis[role] = g, how
        elif g in candidates[role] and how == "format":
            chosen[role], basis[role] = g, "dataflow+format"   # the formats settle a tie of the dataflow
    return {r: chosen.get(r) for r in ROLES}, basis


def _assign_roles(passes_of: dict[tuple, list[dict]]) -> None:
    every = [p for passes in passes_of.values() for p in passes]
    for p in every:
        p["_slots"], p["_basis"] = _native_roles(p)
    # A pixel shader reads the same slots for every mesh: what its clear cases give stands for the rest.
    clear: dict[str, Counter] = defaultdict(Counter)
    for p in every:
        if all(p["_basis"].get(r) in SURE for r in ROLES if p["_slots"][r] is not None) and any(p["_slots"].values()):
            clear[p["ps"]][tuple(p["_slots"][r] for r in ROLES)] += 1
    for p in every:
        if p["ps"] in clear:
            shared = dict(zip(ROLES, clear[p["ps"]].most_common(1)[0][0]))
            by_slot = {s["slot"]: s for s in p["slots"]}
            for role in ROLES:
                slot = shared[role]
                if slot is None or p["_basis"].get(role) in SURE:
                    continue
                if slot in by_slot and by_slot[slot]["class"] == "material" and _fits(role, by_slot[slot].get("format")) \
                        and slot not in [p["_slots"][r] for r in ROLES if r != role]:
                    p["_slots"][role], p["_basis"][role] = slot, "shader"
        by_slot = {s["slot"]: s for s in p["slots"]}
        p["roles"] = {}
        for role in ROLES:
            slot = p["_slots"][role]
            p["roles"][role] = None if slot is None else {
                **{k: by_slot[slot][k] for k in ("slot", "format", "w", "h") if k in by_slot[slot]},
                "basis": p["_basis"][role]}
        del p["_slots"], p["_basis"]


def _triple(p: dict) -> tuple:
    return tuple((p["roles"][r] or {}).get("slot") for r in ROLES)


def _main(passes: list[dict], users: Counter) -> dict | None:
    """The main pass, the variants that read the same three slots, and other lit passes that read
    at least two of the three elsewhere (a mesh can be drawn twice in a frame by lit shaders whose
    slots differ; one ps-tN binding cannot suit both)."""
    # Depth passes and G-buffer passes (deferred shading writes three targets or more) read textures a
    # mod for a forward-shaded character does not bind: diffuse at t0 for alpha testing, or the
    # material at t0/t1. They are never the main pass.
    with_roles = [p for p in passes if any(p["roles"].values()) and 1 <= len(p["targets"]) < 3]
    if not with_roles:
        return None
    # Between equal passes the one whose diffuse comes first, as LoyalTools' texture marking prefers
    # (the mods it writes bind that pass's slots).
    first = lambda p: -min((v["slot"] for v in p["roles"].values() if v), default=999)  # noqa: E731
    main = max(with_roles, key=lambda p: (p["lit"], sum(1 for s in p["slots"] if s["class"] == "material"),
                                           sum(1 for v in p["roles"].values() if v), first(p), users[p["ps"]],
                                           len(p["slots"]), p["ps"]))
    others: dict[tuple, dict] = {}
    also = []
    for p in with_roles:
        if p is main or p["lit"] != main["lit"]:
            continue
        if _triple(p) == _triple(main):
            also.append(p["ps"])
        elif sum(1 for v in p["roles"].values() if v) >= 2:
            entry = others.setdefault(_triple(p), {"ps": p["ps"], "also_ps": [], "roles": p["roles"]})
            if p["ps"] != entry["ps"]:
                entry["also_ps"].append(p["ps"])
    return {"ps": main["ps"], "also_ps": sorted(also), "targets": main["targets"], "lit": main["lit"],
            "roles": main["roles"], "other_passes": [others[k] for k in sorted(others, key=str)]}


# ---------------------------------------------------------------- the table
def build(folder: Path, names: dict | None = None, reader: Callable[[str], dict] | None = None,
          declared: dict | None = None) -> dict:
    """names: (ib, count) -> [(mod, section)]; declared: (ib, count) -> role -> slots a mod binds.
    Neither changes a slot: names group the rows, declared is compared afterwards."""
    folder = Path(folder)
    records = [r for r in load_records(folder) if r.get("origin") == "game" and r.get("ib")]
    reader = reader or shader_reader(folder)
    names, declared = names or {}, declared or {}
    classes = texture_classes(records)

    meshes: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        meshes[(r["ib"], r["index_count"])].append(r)
    first_frame = {m: min(r["frame"] for r in rs) for m, rs in meshes.items()}
    passes_of = {m: _passes(rs, classes, reader) for m, rs in meshes.items()}
    _assign_roles(passes_of)
    users = Counter(p["ps"] for passes in passes_of.values() for p in passes)

    def row(m: tuple) -> dict:
        main = _main(passes_of[m], users)
        out = {"ib": m[0], "index_count": m[1], "first_frame": first_frame[m], "main": main}
        if m in names:
            out["mod_sections"] = sorted({f"{mod}: {section}" for mod, section in names[m]})
        if m in declared and main:
            out["mod_check"] = {}
            for role, slots in sorted(declared[m].items()):
                mine = (main["roles"].get(role) or {}).get("slot")
                other = [o["ps"] for o in main["other_passes"]
                         if (o["roles"].get(role) or {}).get("slot") is not None and set(slots) != {o["roles"][role]["slot"]}]
                out["mod_check"][role] = {"mod": sorted(slots), "table": mine,
                                          "agrees": mine is not None and set(slots) == {mine},
                                          "wrong_for_other_passes": other}
        if main is None:
            p = passes_of[m]
            drawn = [q for q in p if q["targets"]]
            out["why_none"] = ("no pixel shader analysis" if not any(q["reads_known"] for q in p) else
                               "depth passes only" if not drawn else
                               "G-buffer pass only (deferred shading, material at t0/t1)"
                               if all(len(q["targets"]) >= 3 for q in drawn) else
                               "no material texture read")
        out["passes"] = passes_of[m]
        return out

    known: dict[str, list[tuple]] = defaultdict(list)
    for m in meshes:
        for mod in sorted({mod for mod, _ in names.get(m, [])}):
            known[mod].append(m)
    named = {m for ms in known.values() for m in ms}

    by_frame: dict[int, list[tuple]] = defaultdict(list)
    for m, f in first_frame.items():
        by_frame[f].append(m)
    probable: dict[str, list[tuple]] = defaultdict(list)
    groups, scenes, unassigned = [], [], []
    for f in sorted(by_frame):
        ms = by_frame[f]
        rest = sorted(m for m in ms if m not in named)
        mods_here = {mod for m in ms if m in named for mod, _ in names[m]}
        if not rest:
            continue
        if len(mods_here) == 1 and len(ms) <= SCENE_MESHES:
            probable[next(iter(mods_here))].extend(rest)
        elif not mods_here and GROUP_MIN <= len(ms) <= SCENE_MESHES:
            groups.append({"first_frame": f, "meshes": [row(m) for m in rest]})
        elif len(ms) > SCENE_MESHES or mods_here:
            scenes.append({"first_frame": f, "mods_in_frame": sorted(mods_here), "meshes": [row(m) for m in rest]})
        else:
            unassigned.extend(rest)

    characters = [{"name": mod,
                   "meshes": [row(m) for m in sorted(known[mod], key=lambda m: (first_frame[m], m))],
                   "probably_also": [row(m) for m in sorted(probable.get(mod, []))]}
                  for mod in sorted(known, key=str.lower)]
    try:
        version = json.loads((folder / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        version = {}
    table = {"schema": SCHEMA, "version": version.get("key", folder.name), "label": version.get("label"),
             "generated_unix": int(time.time()), "records": len(records), "meshes": len(meshes),
             "rules": {"main_pass": "samples shadows or an environment cube, else most material textures",
                       "slots": "dataflow > dataflow+position > dataflow+format > sequence (LoyalTools) > format > shader > order (uncertain); mods only compared",
                       "material_texture": f"block-compressed 2D, bound with at most {GLOBAL_COOCCURRENCE} other material textures",
                       "probable_group": f"first seen in the same looked-at frame, {GROUP_MIN} to {SCENE_MESHES} new meshes"},
             "characters": characters, "probable_groups": groups, "scenes": scenes,
             "unassigned": [row(m) for m in sorted(unassigned, key=lambda m: (first_frame[m], m))]}
    table["counts"] = counts(table)
    return table


def all_rows(table: dict):
    """(group label, row) for every mesh of the table."""
    for c in table["characters"]:
        for r in c["meshes"]:
            yield c["name"], r
        for r in c["probably_also"]:
            yield f"{c['name']} (probably)", r
    for g in table["probable_groups"]:
        for r in g["meshes"]:
            yield f"frame {g['first_frame']} (probably one character)", r
    for g in table["scenes"]:
        for r in g["meshes"]:
            yield f"frame {g['first_frame']} (scene)", r
    for r in table["unassigned"]:
        yield "unassigned", r


def counts(table: dict) -> dict:
    rows = [r for _, r in all_rows(table)]
    basis = Counter(b["basis"] for r in rows if r["main"] for b in r["main"]["roles"].values() if b)
    checks = [c for r in rows for c in r.get("mod_check", {}).values()]
    return {"meshes": len(rows), "with_slots": sum(r["main"] is not None for r in rows),
            "with_other_lit_passes": sum(bool(r["main"] and r["main"]["other_passes"]) for r in rows),
            "named_by_mods": sum(len(c["meshes"]) for c in table["characters"]),
            "slot_basis": dict(sorted(basis.items())),
            "mod_checks": {"agree": sum(c["agrees"] for c in checks), "differ": sum(not c["agrees"] for c in checks),
                           "wrong_for_another_lit_pass": sum(bool(c["wrong_for_other_passes"]) for c in checks)}}


# ---------------------------------------------------------------- Markdown
BASIS = {"dataflow": "dataflow", "dataflow+position": "dataflow+position", "dataflow+format": "dataflow+formats",
         "sequence": "sequence (LoyalTools)", "format": "formats",
         "shader": "same shader", "order": "order (uncertain)"}


def _role_text(main: dict, role: str) -> str:
    r = main["roles"].get(role)
    if not r:
        return "-"
    size = f" {r['w']}x{r['h']}" if r.get("w") and r.get("h") else ""
    return f"t{r['slot']} {r.get('format', '')}{size}".rstrip()


def _basis_text(main: dict) -> str:
    seen = []
    for role in ROLES:
        b = (main["roles"].get(role) or {}).get("basis")
        if b and BASIS[b] not in seen:
            seen.append(BASIS[b])
    return ", ".join(seen) or "-"


def _check_text(row: dict) -> str:
    check = row.get("mod_check")
    if not check:
        return ""
    wrong = [f"{role} mod t{','.join(map(str, c['mod']))}" for role, c in check.items() if not c["agrees"]]
    if wrong:
        return "differs: " + "; ".join(wrong)
    if any(c["wrong_for_other_passes"] for c in check.values()):
        return "agrees; wrong for the other lit pass"
    return "agrees"


def _lit(r: dict) -> bool:
    return bool(r["main"] and r["main"]["lit"])


def _rows(rows: list[dict], lines: list[str]) -> None:
    lines.append("| IB | indices | pixel shader | diffuse | light map | normal map | known from | mod check |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        m = r["main"]
        if not _lit(r):
            continue                         # listed in the section of passes that are not lit
        ps = f"`{m['ps']}`" + (f" (+{len(m['also_ps'])})" if m["also_ps"] else "")
        lines.append(f"| `{r['ib']}` | {r['index_count']} | {ps} | {_role_text(m, 'diffuse')} | "
                     f"{_role_text(m, 'lightmap')} | {_role_text(m, 'normal')} | {_basis_text(m)} | {_check_text(r)} |")
        for o in m["other_passes"]:
            ps = f"`{o['ps']}`" + (f" (+{len(o['also_ps'])})" if o["also_ps"] else "")
            lines.append(f"| | | {ps} (other lit pass) | {_role_text(o, 'diffuse')} | {_role_text(o, 'lightmap')} | "
                         f"{_role_text(o, 'normal')} | {_basis_text(o)} | |")


def to_markdown(table: dict) -> str:
    c = table["counts"]
    lines = [f"# Slot table {table['version']}", "",
             f"{c['meshes']} meshes from {table['records']} records of the probe's slot cache; {c['with_slots']} with "
             "at least one of the three slots. Per mesh the main pass (the lit one) and its diffuse, light map and normal "
             "map slots, decided from the game's own data: the shader's dataflow, else the formats of the material "
             "textures, else the same shader on other meshes. The mod check compares the installed mods afterwards; "
             "it never changes a slot. Sizes are the game's textures as first recorded (they stream in). "
             "(+n) after a pixel shader: n variants with the same slots. Meshes whose forward passes sample no shadows "
             "(effects, glowing and translucent parts) are listed in a section of their own near the end. Depth and "
             "G-buffer passes are never the main pass: meshes drawn only by them (the open world's deferred meshes) "
             "are listed at the end. An 'other lit pass' row: the mesh is also "
             "drawn by a lit shader that reads these textures at other slots, which one ps-tN binding cannot suit. "
             "Everything per pass is in slot-table.json.", ""]
    lines.append("## Characters (named by a mod's INI)")
    for ch in table["characters"]:
        lines += ["", f"### {ch['name']}", ""]
        _rows(ch["meshes"], lines)
        if any(_lit(r) for r in ch["probably_also"]):
            lines += ["", "Probably the same character too (first seen in the same frame, not named by the mod):", ""]
            _rows(ch["probably_also"], lines)
    lines += ["", "## Not certain: groups first seen in the same frame (probably one character each)"]
    for g in table["probable_groups"]:
        if any(_lit(r) for r in g["meshes"]):
            lines += ["", f"### Frame {g['first_frame']} ({len(g['meshes'])} meshes)", ""]
            _rows(g["meshes"], lines)
    lines += ["", "## Not certain: whole scenes (many meshes at once, or next to a modded character)"]
    for g in table["scenes"]:
        if any(_lit(r) for r in g["meshes"]):
            extra = f", with {', '.join(g['mods_in_frame'])}" if g["mods_in_frame"] else ""
            lines += ["", f"### Frame {g['first_frame']} ({len(g['meshes'])} meshes{extra})", ""]
            _rows(g["meshes"], lines)
    if any(_lit(r) for r in table["unassigned"]):
        lines += ["", "## Unassigned", ""]
        _rows(table["unassigned"], lines)
    unlit = [(label, r) for label, r in all_rows(table) if r["main"] and not r["main"]["lit"]]
    if unlit:
        lines += ["", f"## Forward passes that do not sample shadows ({len(unlit)}: effects, glowing and translucent parts)", "",
                  "Their shaders read small effect textures, often at t0 to t4; mods rarely bind slots for them.", "",
                  "| IB | indices | group | pixel shader | diffuse | light map | normal map | known from | mod check |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for label, r in unlit:
            m = r["main"]
            lines.append(f"| `{r['ib']}` | {r['index_count']} | {label} | `{m['ps']}` | {_role_text(m, 'diffuse')} | "
                         f"{_role_text(m, 'lightmap')} | {_role_text(m, 'normal')} | {_basis_text(m)} | {_check_text(r)} |")
    none = [(label, r) for label, r in all_rows(table) if r["main"] is None]
    if none:
        lines += ["", f"## Meshes without these slots ({len(none)})", "", "| IB | indices | group | why |", "|---|---|---|---|"]
        for label, r in none:
            lines.append(f"| `{r['ib']}` | {r['index_count']} | {label} | {r['why_none']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- MCP and command line
def query(table: dict, ib_hash: str = "", character: str = "", offset: int = 0, limit: int = 40,
          include_passes: bool = False, reader: Callable[[str], dict] | None = None,
          include_evidence: bool = False) -> dict:
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be 0 or more and limit between 1 and 200")
    ib_hash = (ib_hash or "").strip().lower()
    if ib_hash and not re.fullmatch(r"[0-9a-f]{8}", ib_hash):
        raise ValueError("ib_hash must be 8 hex digits")
    selected = [(label, r) for label, r in all_rows(table)
                if (not ib_hash or r["ib"] == ib_hash) and (not character or character.lower() in label.lower())]
    page = []
    for label, r in selected[offset:offset + limit]:
        item = {k: v for k, v in r.items() if k != "passes" or include_passes}
        item["group"] = label
        if include_evidence and reader and r["main"]:
            analysis = reader(r["main"]["ps"])
            item["evidence"] = {role: {k: v for k, v in analysis.get("resources", {}).get(slot["slot"], {}).items()
                                       if k in ("role_candidates", "lighting_channels", "evidence", "output_components")}
                                for role, slot in r["main"]["roles"].items() if slot}
        page.append(item)
    return {"schema": SCHEMA, "version": table["version"], "counts": table["counts"], "matching": len(selected),
            "rows": page, "next_offset": offset + limit if offset + limit < len(selected) else None,
            "rules": table["rules"]}


class TableCache:
    """One version folder's table, built again when its records, shaders or the mods' INIs change."""

    def __init__(self):
        self.key = None
        self.table = None
        self.reader = None
        self.folder = None

    def get(self, folder: Path, efmi_root: Path | None, refresh: bool = False) -> dict:
        folder = Path(folder).resolve()
        if folder != self.folder:
            self.folder, self.reader = folder, shader_reader(folder)
        files = [folder / "draws.jsonl", folder / "version.json", *sorted((folder / "shaders").glob("*-ps.bin"))]
        inis = sorted(Path(efmi_root).rglob("*.ini")) if efmi_root and Path(efmi_root).is_dir() else []
        key = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files + inis if p.exists())
        if refresh or key != self.key or self.table is None:
            from .slot_table_mods import mod_names, mod_roles
            self.table = build(folder, mod_names(efmi_root), self.reader, mod_roles(efmi_root))
            self.key = key
        return self.table


def register(server, session) -> None:
    cache = TableCache()

    @server.tool("efmi_get_slot_table",
                 "The slot table of a game version from the probe's slot cache, without the game and without writing "
                 "files: per mesh (IB hash and index count) the diffuse, light map and normal map slots of its main pass, "
                 "decided from the game's own shader dataflow and texture formats, with what decided each and whether "
                 "the installed mods agree. Filter by ib_hash or by group (mod name, 'probably', 'frame', 'unassigned'); "
                 "include_passes adds every pass and slot read, include_evidence the dataflow behind the three slots.",
                 {"folder": {"type": "string", "description": "the version folder: <probe dir>\\slot-cache\\<version>"},
                  "ib_hash": {"type": "string", "default": ""},
                  "character": {"type": "string", "default": "", "description": "part of the group label"},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 40},
                  "include_passes": {"type": "boolean", "default": False},
                  "include_evidence": {"type": "boolean", "default": False},
                  "refresh": {"type": "boolean", "default": False}}, required=["folder"])
    def efmi_get_slot_table(folder, ib_hash="", character="", offset=0, limit=40, include_passes=False,
                            include_evidence=False, refresh=False):
        table = cache.get(Path(folder), session.efmi_root, refresh)
        return query(table, ib_hash, character, offset, limit, include_passes, cache.reader, include_evidence)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="efmi_mcp.slot_table", description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="a version folder of the slot cache")
    ap.add_argument("--efmi-root", default=DEFAULT_EFMI,
                    help="the EFMI folder, read to group meshes by mod and to compare the mods' slots ('' for none)")
    args = ap.parse_args(argv)
    from .slot_table_mods import mod_names, mod_roles
    folder = Path(args.folder)
    root = Path(args.efmi_root) if args.efmi_root else None
    table = build(folder, mod_names(root), declared=mod_roles(root))
    (folder / "slot-table.json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    (folder / "slot-table.md").write_text(to_markdown(table), encoding="utf-8")
    c = table["counts"]
    print(f"{c['meshes']} meshes, {c['with_slots']} with slots; {len(table['characters'])} characters named by mods; "
          f"slots from {c['slot_basis']}; mod checks {c['mod_checks']} -> {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
