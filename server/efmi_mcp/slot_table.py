"""The slot table of one game version, built from the probe's slot cache (T-016).

A row is a mesh as mods name it: the index buffer region hash and the index count. For every pass
the game draws it in (one pixel shader each) the table keeps the texture slots that shader reads,
judged from the bytecode the probe saved, so that textures an earlier draw left bound are not
mistaken for this mesh's. A slot is

  material  a non-HDR block-compressed 2D texture, a material candidate only;
  global    a BC6H HDR texture, a global candidate only;
  engine    anything else: buffers, render targets, small lookup and volume textures.

Each pass also names the three slots mods bind: diffuse, normal map and light map. They come from
the installed mods where one binds resources so named (DiffuseMap, NormalMap, LightMap) for a mesh
drawn with that pixel shader and the game's textures in those slots are of the matching kind;
otherwise from an unambiguous consecutive material triple (sRGB diffuse, linear light map,
BC5 or linear normal). Current Endfield primary candidates must satisfy
10 < diffuse < light map < normal map. Auxiliary slots remain raw evidence; they are not a
fallback primary material. These are role hypotheses, not verified shader semantics.

Meshes are grouped by character where that is known: a mesh one of the installed mods names in a
TextureOverride is that mod's character. The rest is grouped, without certainty, by the looked-at
frame in which the probe first saw it (characters shown one after the other appear frame by
frame); what fits neither is listed on its own.

    python -m efmi_mcp.slot_table <slot-cache version folder> [--efmi-root PATH]
writes slot-table.json and slot-table.md into that folder.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

SCENE_MESHES = 40          # a frame that brought more new meshes than this showed a whole scene
GROUP_MIN = 3              # fewer new meshes than this in a frame are not taken for a character
ROLES = ("diffuse", "normal", "lightmap")


def load_records(folder: Path) -> list[dict]:
    """The records of the current format (probe 0.1.8 and later)."""
    out = []
    for line in (folder / "draws.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            if isinstance(record, dict) and record.get("fmt") == 2:
                out.append(record)
        except ValueError:
            continue
    return out


def shader_reader(folder: Path) -> Callable[[str], dict | None]:
    """slot -> access kinds for a pixel shader, from its saved bytecode; None when it cannot be told."""
    from .shader import disassemble, resource_usage

    cache: dict[str, dict | None] = {}

    def reads(ps: str) -> dict | None:
        if ps not in cache:
            path = folder / "shaders" / f"{ps}-ps.bin"
            try:
                usage = resource_usage(disassemble(path.read_bytes()))["resources"]
                cache[ps] = {slot: sorted({i["access"] for i in entry["instructions"]})
                             for slot, entry in usage.items() if entry["instructions"]}
            except (OSError, ValueError):
                cache[ps] = None
        return cache[ps]

    return reads


def _block_texture(view: dict) -> bool:
    return view.get("kind") == "tex2d" and str(view.get("view_format") or view.get("format", "")).upper().startswith("BC")


def _slot_entry(view: dict, access: list[str] | None) -> dict:
    fmt = view.get("view_format") or view.get("format", "")
    cls = ("global" if str(fmt).upper().startswith("BC6H") else "material") if _block_texture(view) else "engine"
    entry = {"slot": view["slot"], "class": cls, "kind": view.get("kind")}
    for key in ("format", "w", "h", "mips", "size", "stride", "hash"):
        if view.get(key) is not None:
            entry["texture" if key == "hash" else key] = view[key]
    if access is not None:
        entry["access"] = access
    entry["format"] = fmt
    entry["class_basis"] = "format heuristic; not a verified material role"
    return entry


def _mesh_passes(records: list[dict], reads: Callable[[str], dict | None]) -> list[dict]:
    """One entry per pixel shader the mesh is drawn with; records of the same pass are merged."""
    by_ps: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_ps[r["ps"] or "-"].append(r)
    passes = []
    for ps, recs in by_ps.items():
        read = reads(ps) if ps != "-" else None
        slots: dict[int, dict] = {}
        conflicts = set()
        for r in recs:
            for view in r.get("ps_srv", []):
                if read is not None and view["slot"] not in read:
                    continue
                entry = _slot_entry(view, read.get(view["slot"]) if read is not None else None)
                old = slots.get(view["slot"])
                if old and (old.get("format"), old.get("kind")) != (entry.get("format"), entry.get("kind")):
                    conflicts.add(view["slot"])
                slots.setdefault(view["slot"], entry)
        first = recs[0]
        item = {"ps": ps, "vs": first.get("vs"), "targets": first.get("rt", []), "depth": first.get("ds"),
                "reads_known": read is not None,
                "slots": [slots[s] for s in sorted(slots)]}
        item["observations"] = len(recs)
        item["pipeline_variants"] = list({json.dumps({"vs": r.get("vs"), "targets": r.get("rt", []),
                                                     "depth": r.get("ds")}, sort_keys=True):
                                           {"vs": r.get("vs"), "targets": r.get("rt", []), "depth": r.get("ds")}
                                           for r in recs}.values())
        if read is not None:
            item["read_but_unbound"] = sorted(s for s in read if s not in slots)
        if conflicts:
            item["differs_between_records"] = sorted(conflicts)
        passes.append(item)
    # The pass mods bind first: most material textures, then most slots read (the lit pass reads the
    # scene's lighting too, the G-buffer pass only the material).
    passes.sort(key=lambda p: (-sum(1 for s in p["slots"] if s["class"] == "material"), -len(p["slots"]), p["ps"]))
    return passes


# ---------------------------------------------------------------- what the installed mods say
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_SLOT = re.compile(r"^\s*ps-t(\d+)\s*=\s*(?:ref\s+|copy\s+)?(\S+)", re.I)
_RUN = re.compile(r"^\s*run\s*=\s*(\S+)", re.I)
_ENTRY = re.compile(r"^TextureOverride_EntryPoint_Component(\d+)", re.I)


def _role_of(resource: str) -> str | None:
    name = resource.lower()
    if "diffuse" in name or "albedo" in name or "basecolor" in name:
        return "diffuse"
    if "normal" in name:
        return "normal"
    if "light" in name:
        return "lightmap"
    return None


def _read_sections(path: Path) -> dict[str, list[str]]:
    raw = path.read_bytes()
    text = raw.decode("latin-1")
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    secs: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        line = line.split(";", 1)[0].rstrip()
        m = _SECTION.match(line)
        if m:
            cur = m[1].strip().lower()
            secs.setdefault(cur, [])
        elif cur is not None and line.strip():
            secs[cur].append(line)
    return secs


def _component_refs(efmi_root: Path):
    from .mods import scan_mods

    for ref in scan_mods(efmi_root).refs:
        if ref.kind != "texture" or len(ref.hash) != 8 or "match_index_count" not in ref.match:
            continue
        try:
            yield (ref.hash.lower(), int(ref.match["match_index_count"])), ref
        except ValueError:
            continue


def mod_names(efmi_root: Path | None) -> dict[tuple[str, int], list[tuple[str, str]]]:
    """(ib hash, index count) -> [(mod, section)] from the TextureOverride sections of the installed mods."""
    out: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
    if efmi_root:
        for key, ref in _component_refs(efmi_root):
            if (ref.mod, ref.section) not in out[key]:
                out[key].append((ref.mod, ref.section))
    return out


def mod_roles(efmi_root: Path | None) -> dict[tuple[str, int], dict[str, set[int]]]:
    """(ib hash, index count) -> role -> slots the mods bind resources of that name to, in any branch.

    Followed from the component's TextureOverride: run = CommandList... inside the mod, and the
    EFMIv1 convention EntryPoint_ComponentN -> CommandList_Draw_ComponentN.
    """
    if not efmi_root:
        return {}
    cache: dict[Path, dict[str, dict[str, list[str]]]] = {}

    def mod_inis(root: Path) -> dict[str, dict[str, list[str]]]:
        if root not in cache:
            found = {}
            for ini in sorted(root.rglob("*.ini")):
                if not any(part.upper().startswith("DISABLED") for part in ini.relative_to(root).parts):
                    try:
                        found[str(ini)] = _read_sections(ini)
                    except OSError:
                        pass
            cache[root] = found
        return cache[root]

    def lines_of(inis, name: str) -> list[str]:
        name = name.lower()
        short = name.split("\\")[-1]
        for secs in inis.values():
            for key in (name, short):
                if key in secs:
                    return secs[key]
        return []

    def walk(inis, name: str, seen: set, out: dict[str, set[int]]) -> None:
        name = name.lower()
        if name in seen:
            return
        seen.add(name)
        for line in lines_of(inis, name):
            m = _SLOT.match(line)
            if m:
                role = _role_of(m[2])
                if role:
                    out.setdefault(role, set()).add(int(m[1]))
                continue
            m = _RUN.match(line)
            if m and not m[1].lower().startswith(("commandlist\\efmiv1", "commandlist\\rabbitfx")):
                walk(inis, m[1], seen, out)

    out: dict[tuple[str, int], dict[str, set[int]]] = {}
    for key, ref in _component_refs(efmi_root):
        parts = Path(ref.ini).parts
        if ref.mod not in parts:
            continue
        inis = mod_inis(Path(efmi_root).joinpath(*parts[:parts.index(ref.mod) + 1]))
        roles: dict[str, set[int]] = {}
        walk(inis, ref.section, set(), roles)
        m = _ENTRY.match(ref.section)
        if m:
            walk(inis, f"CommandList_Draw_Component{m[1]}", set(), roles)
        for role, slots in roles.items():
            out.setdefault(key, {}).setdefault(role, set()).update(slots)
    return out


# ---------------------------------------------------------------- roles
def _kind_fits(role: str, fmt: str | None) -> bool:
    f = (fmt or "").upper()
    if role == "diffuse":
        return f.endswith("_SRGB")
    return bool(f) and not f.endswith("_SRGB") and "FLOAT" not in f and not f.startswith("BC6H")


def _roles_from_formats(slots: list[dict]) -> tuple[dict[str, int | None], str]:
    # Current Endfield primary-material convention, supplied by the user. Low-slot
    # auxiliary passes remain in raw slots, but must not become primary materials.
    material = {s["slot"]: s for s in slots if s["class"] == "material" and s["slot"] > 10}
    candidates = []
    for d, diffuse in material.items():
        light, normal = material.get(d + 1), material.get(d + 2)
        if (light and normal and _kind_fits("diffuse", diffuse.get("format"))
                and _kind_fits("lightmap", light.get("format"))
                and _kind_fits("normal", normal.get("format"))):
            candidates.append((d, str(normal.get("format", "")).upper().startswith("BC5")))
    # BC5 provides stronger normal-map evidence than two indistinguishable linear
    # formats. Multiple equally plausible triples remain unresolved.
    strong = [c for c in candidates if c[1]]
    candidates = strong or candidates
    if len(candidates) != 1:
        return dict.fromkeys(ROLES), "none"
    d, strong_normal = candidates[0]
    return {"diffuse": d, "lightmap": d + 1, "normal": d + 2}, "format" if strong_normal else "order"


def _primary_order(roles: dict) -> bool:
    d, l, n = (roles.get(r) for r in ("diffuse", "lightmap", "normal"))
    return all(s is not None for s in (d, l, n)) and 10 < d < l < n


def _assign_roles(passes_of: dict, roles: dict[tuple[str, int], dict[str, set[int]]]) -> None:
    # A mesh a mod names is drawn in several passes; the mod's bindings are meant for the pass whose
    # shader reads them, and count only where the game's texture there is of the named kind.
    votes: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for m, assigned in roles.items():
        if m not in passes_of:
            continue
        wanted = {s for slots in assigned.values() for s in slots}
        best = max(passes_of[m], key=lambda p: sum(1 for s in p["slots"] if s["slot"] in wanted), default=None)
        if best is None or not best["reads_known"]:
            continue
        by_slot = {s["slot"]: s for s in best["slots"]}
        for role, slots in assigned.items():
            for slot in slots:
                if slot > 10 and slot in by_slot and by_slot[slot]["class"] == "material" and _kind_fits(role, by_slot[slot].get("format")):
                    votes[best["ps"]][role][slot] += 1
    # A pixel shader reads the same slots for every mesh: where the formats are clear for some of its
    # meshes, the slots they give stand for its other meshes too, if the textures there fit.
    guesses = {}
    clean: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    for passes in passes_of.values():
        for p in passes:
            guess, basis = _roles_from_formats(p["slots"])
            guesses[id(p)] = (guess, basis)
            if basis == "format" and p["reads_known"]:
                clean[p["ps"]][tuple(guess[r] for r in ROLES)] += 1
    consensus = {ps: next(iter(c)) for ps, c in clean.items() if len(c) == 1}
    for passes in passes_of.values():
        for p in passes:
            if not p["reads_known"]:
                p["roles"] = dict.fromkeys(ROLES)
                p["roles_basis"] = "none"
                continue
            guess, basis = guesses[id(p)]
            shared = consensus.get(p["ps"])
            if shared and shared != tuple(guess[r] for r in ROLES):
                by_slot = {s["slot"]: s for s in p["slots"]}
                if all(slot is None or (slot in by_slot and by_slot[slot]["class"] == "material"
                                        and _kind_fits(role, by_slot[slot].get("format")))
                       for role, slot in zip(ROLES, shared)):
                    guess, basis = dict(zip(ROLES, shared)), "shader"
            known = votes.get(p["ps"], {})
            by_slot = {s["slot"]: s for s in p["slots"]}
            chosen = {}
            for role in ROLES:
                if known.get(role):
                    slot = max(known[role].items(), key=lambda kv: kv[1])[0]
                    chosen[role] = (slot if len(known[role]) == 1 and slot in by_slot
                                    and by_slot[slot]["class"] == "material"
                                    and _kind_fits(role, by_slot[slot].get("format")) else None)
                else:
                    chosen[role] = guess[role]
            for role, slot in list(chosen.items()):
                if slot in p.get("differs_between_records", []):
                    chosen[role] = None
            duplicates = {s for s in chosen.values() if s is not None and list(chosen.values()).count(s) > 1}
            chosen = {r: None if s in duplicates else s for r, s in chosen.items()}
            if all(s is not None for s in chosen.values()) and not _primary_order(chosen):
                p["roles_rejected"] = {"reason": "violates current Endfield primary order: 10 < diffuse < lightmap < normal",
                                       "candidates": chosen.copy()}
                chosen = dict.fromkeys(ROLES)
            p["roles"] = {role: (None if slot is None else
                                 {k: by_slot[slot][k] for k in ("slot", "format", "w", "h") if k in by_slot[slot]})
                          for role, slot in chosen.items()}
            if known:
                p["roles_basis"] = "mod" if all(known.get(r) for r in ROLES if chosen[r] is not None) else "mod+format"
            else:
                p["roles_basis"] = basis
            conflict = {role: dict(v) for role, v in known.items() if len(v) > 1}
            if conflict:
                p["roles_conflict"] = conflict


# ---------------------------------------------------------------- the table
def build(folder: Path, names: dict[tuple[str, int], list[tuple[str, str]]] | None = None,
          reads: Callable[[str], dict | None] | None = None,
          roles: dict[tuple[str, int], dict[str, set[int]]] | None = None) -> dict:
    folder = Path(folder)
    records = [r for r in load_records(folder) if r.get("origin") == "game" and r.get("ib_state") == "ok"]
    reads = reads or shader_reader(folder)
    names = names or {}

    meshes: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in records:
        if r.get("ib"):
            meshes[(r["ib"], r["index_count"])].append(r)
    first_frame = {m: min(r["frame"] for r in rs) for m, rs in meshes.items()}
    passes_of = {m: _mesh_passes(rs, reads) for m, rs in meshes.items()}
    _assign_roles(passes_of, roles or {})

    def row(m: tuple[str, int]) -> dict:
        out = {"ib": m[0], "index_count": m[1], "first_frame": first_frame[m], "passes": passes_of[m]}
        primary = main_pass(out)
        out["primary"] = ({"ps": primary["ps"], "roles": primary["roles"], "basis": primary["roles_basis"]}
                          if primary else None)
        out["primary_status"] = "candidate" if primary else "undetermined: missing or ambiguous primary evidence"
        if m in names:
            out["mod_sections"] = [s for _, s in names[m]]
        return out

    # Known: a mod names the mesh. A mesh two mods name is listed under both.
    known: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for m in meshes:
        for mod in sorted({mod for mod, _ in names.get(m, [])}):
            known[mod].append(m)
    named = {m for ms in known.values() for m in ms}

    # Not certain: the other meshes first seen in the same looked-at frame.
    by_frame: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for m, f in first_frame.items():
        by_frame[f].append(m)
    probable_members: dict[str, list[tuple[str, int]]] = defaultdict(list)
    groups, scenes, unassigned = [], [], []
    for f in sorted(by_frame):
        ms = by_frame[f]
        rest = [m for m in ms if m not in named]
        mods_here = {mod for m in ms if m in named for mod, _ in names[m]}
        if not rest:
            continue
        if len(mods_here) == 1 and len(ms) <= SCENE_MESHES:
            probable_members[next(iter(mods_here))].extend(rest)
        elif not mods_here and GROUP_MIN <= len(ms) <= SCENE_MESHES:
            groups.append({"first_frame": f, "meshes": [row(m) for m in sorted(rest)]})
        elif len(ms) > SCENE_MESHES or mods_here:
            scenes.append({"first_frame": f, "mods_in_frame": sorted(mods_here), "meshes": [row(m) for m in sorted(rest)]})
        else:
            unassigned.extend(rest)

    characters = []
    for mod in sorted(known, key=str.lower):
        characters.append({
            "name": mod,
            "meshes": [row(m) for m in sorted(known[mod], key=lambda m: (first_frame[m], m))],
            "probably_also": [row(m) for m in sorted(probable_members.get(mod, []))],
            "shared_with": sorted({x for m in known[mod] for x, _ in names[m]} - {mod}),
        })
    version = {}
    try:
        version = json.loads((folder / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {
        "version": version.get("key", folder.name), "label": version.get("label"),
        "generated_unix": int(time.time()),
        "records": len(records), "meshes": len(meshes),
        "rules": {"main_pass": "current Endfield convention: 10 < diffuse < lightmap < normal; complete unambiguous triple required; low-slot passes retained only as raw evidence",
                  "material_texture": "format heuristic only: non-HDR block-compressed 2D; BC6H is a global candidate; texture hashes are not used",
                  "roles": "mod resource names for the pixel shader where they fit the game's formats, else formats "
                           "(for this mesh or, where unclear, for other meshes of the same shader), else order",
                  "probable_group": f"first seen in the same looked-at frame, {GROUP_MIN} to {SCENE_MESHES} new meshes"},
        "characters": characters,
        "probable_groups": groups,
        "scenes": scenes,
        "unassigned": [row(m) for m in sorted(unassigned, key=lambda m: (first_frame[m], m))],
    }


# ---------------------------------------------------------------- Markdown
BASIS = {"mod": "mod names", "mod+format": "mod names + formats", "format": "formats",
         "shader": "formats of the same shader on other meshes",
         "order": "uncertain (slot order or slots apart)", "none": "-"}


def _role_text(p: dict, role: str) -> str:
    r = p["roles"].get(role)
    if not r:
        return "-"
    size = f" {r['w']}x{r['h']}" if r.get("w") and r.get("h") else ""
    return f"t{r['slot']} {r.get('format', '')}{size}".rstrip()


def main_pass(row: dict) -> dict | None:
    """A primary candidate under the current Endfield convention, never an auxiliary fallback."""
    candidates = [p for p in row["passes"] if p["reads_known"] and not p.get("roles_conflict")
                  and _primary_order({r: (v or {}).get("slot") for r, v in p["roles"].items()})]
    named = [p for p in candidates if p["roles_basis"].startswith("mod")]
    candidates = named or candidates
    triples = {tuple(p["roles"][r]["slot"] for r in ROLES) for p in candidates}
    if len(triples) != 1:
        return None
    return max(candidates, key=lambda p: sum(bool(set(s.get("access", [])) & {"sample", "load"}) for s in p["slots"]))


def _mesh_rows(rows: list[dict], lines: list[str]) -> None:
    lines.append("| IB | indices | pixel shader | diffuse | light map | normal map | known from |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        p = main_pass(r)
        if p is None:
            lines.append(f"| `{r['ib']}` | {r['index_count']} | primary undetermined | | | | |")
            continue
        basis = BASIS[p["roles_basis"]] + (" (mods disagree)" if p.get("roles_conflict") else "")
        lines.append(f"| `{r['ib']}` | {r['index_count']} | `{p['ps']}` | {_role_text(p, 'diffuse')} | "
                     f"{_role_text(p, 'lightmap')} | {_role_text(p, 'normal')} | {basis} |")


def to_markdown(table: dict) -> str:
    lines = [f"# Slot table {table['version']}", "",
             f"{table['meshes']} meshes from {table['records']} records of the probe's slot cache. Per mesh the pass "
             "with an unambiguous primary-material candidate and its diffuse, light map and normal map slots "
             "(current Endfield convention: 10 < diffuse < light map < normal map), "
             "known from the mods' resource names for that pixel shader or from the formats of the game's textures "
             "there. Roles and material/global classes are suggestions, not verified semantics or migration rules. "
             "Unknown shader usage and conflicting formats do not receive roles. "
             "Formats and sizes are the game's own textures as first recorded; the game streams textures in, "
             "so a size below the one seen up close is normal. Every pass and every slot read is in slot-table.json.", ""]
    lines.append("## Characters (named by a mod's INI)")
    for c in table["characters"]:
        lines += ["", f"### {c['name']}" + (f" (meshes also named by {', '.join(c['shared_with'])})" if c["shared_with"] else ""), ""]
        _mesh_rows(c["meshes"], lines)
        if c["probably_also"]:
            lines += ["", "Probably the same character too (first seen in the same frame, not named by the mod):", ""]
            _mesh_rows(c["probably_also"], lines)
    lines += ["", "## Not certain: groups first seen in the same frame (probably one character each)"]
    for g in table["probable_groups"]:
        lines += ["", f"### Frame {g['first_frame']} ({len(g['meshes'])} meshes)", ""]
        _mesh_rows(g["meshes"], lines)
    lines += ["", "## Not certain: whole scenes (many meshes at once, or next to a modded character)"]
    for g in table["scenes"]:
        extra = f", with {', '.join(g['mods_in_frame'])}" if g["mods_in_frame"] else ""
        lines += ["", f"### Frame {g['first_frame']} ({len(g['meshes'])} meshes{extra})", ""]
        _mesh_rows(g["meshes"], lines)
    lines += ["", "## Unassigned", ""]
    _mesh_rows(table["unassigned"], lines)
    return "\n".join(lines) + "\n"


class TableCache:
    """One version in memory. Rebuild on cache changes; refresh also rescans mod names."""

    def __init__(self):
        self.signature = None
        self.table = None

    def get(self, folder: Path, root: Path | None = None, refresh: bool = False) -> dict:
        folder = folder.resolve()
        paths = [folder / "draws.jsonl", folder / "version.json", *sorted((folder / "shaders").glob("*-ps.bin"))]
        signature = (str(folder), str(root), tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                                 for p in paths if p.exists()))
        if refresh or signature != self.signature:
            table = build(folder, mod_names(root), roles=mod_roles(root))
            self.table, self.signature = table, signature
        return self.table


def query(table: dict, ib_hash: str = "", character: str = "", offset: int = 0, limit: int = 40) -> dict:
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 200")
    if ib_hash and not re.fullmatch(r"[0-9a-fA-F]{8}", ib_hash):
        raise ValueError("ib_hash must be eight hexadecimal digits")
    rows = {}
    for c in table["characters"]:
        for m in c["meshes"]:
            key = (m["ib"], m["index_count"])
            rows.setdefault(key, {**m, "characters": []})["characters"].append(c["name"])
    other = [m for c in table["characters"] for m in c["probably_also"]]
    other += [m for g in table["probable_groups"] + table["scenes"] for m in g["meshes"]] + table["unassigned"]
    for m in other:
        rows.setdefault((m["ib"], m["index_count"]), {**m, "characters": []})
    selected = [r for _, r in sorted(rows.items()) if (not ib_hash or r["ib"] == ib_hash.lower())
                and (not character or any(character.lower() in n.lower() for n in r["characters"]))]
    end = offset + limit
    return {"version": table["version"], "matching_meshes": len(selected), "rows": selected[offset:end],
            "next_offset": end if end < len(selected) else None, "rules": table["rules"],
            "note": "Offline cached observations, not a live frame. Roles are hypotheses, not migration rules. "
                    "Frame-based groups can mix characters or sessions and are not used as character filters. "
                    "No files written. Use refresh after changing installed mod INIs."}


def register(server, session):
    cache = TableCache()

    @server.tool("efmi_get_slot_table",
                 "Read a version's persistent slot cache without exporting files or contacting the game. "
                 "Filter by IB region hash or installed mod name; returns all shader passes and role hypotheses. "
                 "Reuses the table in memory until the cache changes; refresh after editing mod INIs.",
                 {"folder": {"type": "string", "description": "Absolute slot-cache version folder containing draws.jsonl."},
                  "ib_hash": {"type": "string", "default": ""},
                  "character": {"type": "string", "default": ""},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 40},
                  "refresh": {"type": "boolean", "default": False}}, required=["folder"])
    def efmi_get_slot_table(folder: str, ib_hash: str = "", character: str = "", offset: int = 0,
                            limit: int = 40, refresh: bool = False):
        return query(cache.get(Path(folder), session.efmi_root, refresh), ib_hash, character, offset, limit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="efmi_mcp.slot_table", description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="a version folder of the slot cache (dist\\slot-cache\\<version>)")
    ap.add_argument("--efmi-root", default="",
                    help="the EFMI folder, read to name meshes and slots after the installed mods ('' for none)")
    args = ap.parse_args(argv)
    folder = Path(args.folder)
    root = Path(args.efmi_root) if args.efmi_root else None
    table = build(folder, mod_names(root), roles=mod_roles(root))
    (folder / "slot-table.json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    (folder / "slot-table.md").write_text(to_markdown(table), encoding="utf-8")
    print(f"{table['meshes']} meshes: {len(table['characters'])} characters named by mods, "
          f"{len(table['probable_groups'])} probable groups, {len(table['scenes'])} scenes, "
          f"{len(table['unassigned'])} unassigned -> {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
