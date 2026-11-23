"""Portable EFMI INI texture conversion, preview and byte-exact restoration.

Only the Python standard library is required. build_texture_repair.py embeds a
catalog derived from a native slot table into a distributable copy of this file.
"""
from __future__ import annotations

import argparse
import base64
import codecs
import hashlib
import json
import os
import re
import sys
import tempfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"
CATALOG_B85 = ""
MARKER = "; EFMI texture repair 1.0.0"
BACKUP = ".efmi-texture-repair.bak"
RECEIPT = ".efmi-texture-repair.json"
ROLES = {"diffuse": "Diffuse", "lightmap": "Light", "normal": "Normal"}
RESET = "CommandList_EFMIRepair_ResetMatches"
POOL = "Pool_EFMIRepair_SavedTextures"
INTERFACE_ROLES = {"diffuse": "diffuse", "lightmap": "lightmap", "normalmap": "normal"}
HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$")
BASIC = re.compile(r"Resource\\(RabbitFX|EFMITTL)\\(Diffuse|LightMap|NormalMap)\b", re.I)
SET = re.compile(r"CommandList\\(RabbitFX|EFMITTL)\\SetTextures$", re.I)
SLOT = re.compile(r"ps-t(\d+)$", re.I)
RESOURCE = re.compile(r"(?:ref\s+)?(Resource[\w.\\-]+|null)$", re.I)
SURE = {"dataflow", "dataflow+position", "dataflow+format", "sequence", "format", "other pass"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def assignment(line):
    code = line.split(";", 1)[0].strip()
    if "=" not in code:
        return None
    key, value = code.split("=", 1)
    return key.strip().lower(), value.strip()


def table_rows(table):
    for group in table.get("characters", []):
        yield from group.get("meshes", [])
        yield from group.get("probably_also", [])
    for kind in ("probable_groups", "scenes"):
        for group in table.get(kind, []):
            yield from group.get("meshes", [])
    yield from table.get("unassigned", [])


def make_catalog(table):
    """No mod inventory or character-name whitelist participates in eligibility."""
    meshes = {}
    for row in table_rows(table):
        main = row.get("main") or {}
        roles = {}
        for role, entry in main.get("roles", {}).items():
            if role not in ROLES or not entry or entry.get("basis") not in SURE:
                continue
            samples = entry.get("dhashes", [])
            values = {(v.get("view_format") or v.get("format"), v.get("dhash")) for v in samples}
            if len(values) != 1:
                continue
            fmt, dhash = next(iter(values))
            if not re.fullmatch(r"[0-9a-fA-F]{16}", dhash or ""):
                continue
            if not re.fullmatch(r"BC[1-7][A-Z0-9_]*", fmt or ""):
                continue
            roles[role] = dict(slot=entry["slot"], format=fmt, dhash=dhash.lower())
        key = row["ib"].lower() + ":" + str(row["index_count"])
        signatures = [(v["format"], v["dhash"]) for v in roles.values()]
        roles = {r: v for r, v in roles.items() if signatures.count((v["format"], v["dhash"])) == 1}
        if key in meshes:
            roles = {r: v for r, v in roles.items() if meshes[key].get(r) == v}
        meshes[key] = roles
    return dict(schema=1, game_version=table.get("version", "unknown"),
                selection="All native table groups; confident role and one format/dHash only",
                meshes=meshes)


def load_catalog(path=None):
    if path:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if "characters" in doc:
            doc = make_catalog(doc)
    elif CATALOG_B85:
        doc = json.loads(zlib.decompress(base64.b85decode(CATALOG_B85)))
    else:
        raise ValueError("Supply --catalog slot-table.json, or use the packaged script with its embedded catalog.")
    if doc.get("schema") != 1 or not isinstance(doc.get("meshes"), dict):
        raise ValueError("Unsupported catalog")
    for key, roles in doc["meshes"].items():
        if not re.fullmatch(r"[0-9a-f]{8}:[1-9][0-9]*", key) or not isinstance(roles, dict):
            raise ValueError("Invalid catalog mesh: " + str(key))
        for role, entry in roles.items():
            if (role not in ROLES or not isinstance(entry, dict)
                    or type(entry.get("slot")) is not int or not 0 <= entry["slot"] < 128
                    or not re.fullmatch(r"BC[1-7][A-Z0-9_]*", str(entry.get("format", "")))
                    or not re.fullmatch(r"[0-9a-f]{16}", str(entry.get("dhash", "")))):
                raise ValueError("Invalid catalog role: " + key + "/" + role)
    if not isinstance(doc.get("game_version"), str) or any(c in doc["game_version"] for c in "\r\n"):
        raise ValueError("Invalid catalog game version")
    return doc


def regular(path):
    """Never traverse links or Windows junctions into a different tree."""
    return not path.is_symlink() and not (path.lstat().st_file_attributes & 0x400 if os.name == "nt" else False)


def walk_files(root, suffix):
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"}
                         and regular(Path(folder) / d))
        for name in sorted(files):
            path = Path(folder) / name
            if name.lower().endswith(suffix) and regular(path):
                yield path


@dataclass(eq=False)
class Section:
    doc: "Document"
    name: str
    start: int
    end: int = 0

    def commands(self):
        for i in range(self.start + 1, self.end):
            item = assignment(self.doc.lines[i])
            if item:
                yield i, *item

    def values(self):
        return {k: v for _, k, v in self.commands()}


@dataclass(eq=False)
class Document:
    path: Path
    raw: bytes
    text: str = ""
    encoding: str = ""
    bom: bytes = b""
    lines: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    namespace: str = ""
    error: str = ""

    def __post_init__(self):
        data = self.raw
        for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                         (codecs.BOM_UTF16_BE, "utf-16-be")):
            if data.startswith(bom):
                self.bom, self.encoding, data = bom, enc, data[len(bom):]
                break
        for enc in ([self.encoding] if self.encoding else ["utf-8", "gb18030", "latin-1"]):
            try:
                self.text, self.encoding = data.decode(enc), enc
                break
            except UnicodeDecodeError:
                pass
        self.lines = self.text.splitlines(keepends=True)
        names = set()
        depth = 0
        for i, line in enumerate(self.lines):
            m = HEADER.match(line.strip())
            if m:
                if depth:
                    self.error = "Unbalanced control flow"
                if self.sections:
                    self.sections[-1].end = i
                name = m[1].strip()
                if name.lower() in names and name.lower() not in {"present", "constants"}:
                    self.error = "Duplicate section: " + name
                names.add(name.lower())
                self.sections.append(Section(self, name, i))
            elif not self.sections:
                a = assignment(line)
                if a and a[0] == "namespace":
                    self.namespace = a[1].lower()
            code = line.split(";", 1)[0].strip().lower()
            if re.match(r"if\s", code):
                depth += 1
            elif code == "endif":
                depth -= 1
            if depth < 0:
                self.error = "Unbalanced control flow"
        if depth:
            self.error = "Unbalanced control flow"
        if self.sections:
            self.sections[-1].end = len(self.lines)

    @property
    def scope(self):
        return self.namespace or str(self.path).lower()

    @property
    def nl(self):
        return "\r\n" if "\r\n" in self.text else "\n"

    def encode(self, text):
        return self.bom + text.encode(self.encoding)


def resolve(source, target, index):
    target = target.lower()
    if "\\" in target:
        kind, rest = target.split("\\", 1)
        if "\\" not in rest:
            return None
        namespace, short = rest.rsplit("\\", 1)
        key = (namespace, kind + short)
    else:
        key = (source.doc.scope, target)
    found = index.get(key, [])
    return found[0] if len(found) == 1 else None


def reference(key, value):
    if key in {"run", "post run"}:
        return value
    if key.startswith("commandlist") and value.lower().startswith("ref "):
        return value[4:].strip()
    return None


def semantic_role(value):
    name = value.lower()
    if any(s in name for s in ("diffuse", "albedo", "basecolor")):
        return "diffuse"
    if "normal" in name:
        return "normal"
    if "lightmap" in name:
        return "lightmap"
    return None


def pending_interface(sec, interface, index, pending=None, active=None):
    """Conservative branch merge: can this helper leave basic inputs unconsumed?"""
    state, active, stack = set(pending or ()), set(active or ()), []
    if sec in active:
        return set(INTERFACE_ROLES)
    active.add(sec)
    for line in sec.doc.lines[sec.start + 1:sec.end]:
        code = line.split(";", 1)[0].strip().lower()
        if code.startswith("if "):
            stack.append([set(state), set(), False])
        elif code.startswith("elif ") or code == "else":
            if not stack:
                return set(INTERFACE_ROLES)
            stack[-1][1].update(state)
            stack[-1][2] |= code == "else"
            state = set(stack[-1][0])
        elif code == "endif":
            if not stack:
                return set(INTERFACE_ROLES)
            entry, exits, has_else = stack.pop()
            state |= exits | (set() if has_else else entry)
        item = assignment(line)
        if not item:
            continue
        key, value = item
        m = BASIC.fullmatch(key)
        if m and m[1].lower() == interface:
            if value.lower() == "null":
                state.discard(m[2].lower())
            else:
                state.add(m[2].lower())
        if key == "run":
            call = SET.fullmatch(value)
            if call and call[1].lower() == interface:
                state.clear()
            else:
                target = resolve(sec, value, index)
                if target:
                    state = pending_interface(target, interface, index, state, active)
    return state | (set(INTERFACE_ROLES) if stack else set())


def plan(documents, catalog):
    """Follow real run/ref edges; specialize only unambiguous material contexts.

    Control flow and draw commands are never rewritten. Interface inputs become
    local staging resources and are consumed at the original SetTextures call.
    This preserves independent materials for each draw within the same IB.
    """
    index = defaultdict(list)
    for doc in documents:
        for sec in doc.sections:
            index[doc.scope, sec.name.lower()].append(sec)
    sections = [s for d in documents for s in d.sections]
    edges, missing = {}, defaultdict(list)
    for sec in sections:
        edges[sec] = []
        for _, key, value in sec.commands():
            target = reference(key, value)
            if not target:
                continue
            # API implementations are consumers, never user material contexts.
            if target.lower().startswith(("commandlist\\efmiv1\\", "commandlist\\rabbitfx\\",
                                          "commandlist\\efmittl\\")) or target.lower() in {
                                              "snapshotinitialtextures", "restoreinitialtextures"}:
                continue
            other = resolve(sec, target, index)
            if other:
                edges[sec].append(other)
            else:
                missing[sec].append(target)

    def closure(sec):
        seen, active = set(), set()
        cyclic = False

        def visit(s):
            nonlocal cyclic
            if s in active:
                cyclic = True
                return
            if s in seen:
                return
            seen.add(s)
            active.add(s)
            for child in edges[s]:
                visit(child)
            active.remove(s)
        visit(sec)
        return seen, cyclic

    meshes = catalog["meshes"]
    roots = [s for s in sections if s.name.lower().startswith(("textureoverride", "shaderoverride", "key"))
             or s.name.lower() in {"present", "clearingsettings"}]
    identities = {}
    for root in roots:
        v = root.values()
        if not root.name.lower().startswith("textureoverride"):
            identities[root] = None
            continue
        ib = v.get("hash", "").lower().removeprefix("0x")
        candidates = [k for k in meshes if k.startswith(ib + ":")] if re.fullmatch(r"[0-9a-f]{8}", ib) else []
        if "match_index_count" in v:
            candidates = [k for k in candidates if k.split(":")[1] == v["match_index_count"]]
        identities[root] = (root.doc.scope, candidates[0]) if len(candidates) == 1 else None

    # Explicit main/LOD callbacks establish their shared material context.
    callbacks = defaultdict(set)
    def callback(root):
        return next((resolve(root, v[4:].strip(), index) for _, k, v in root.commands()
                     if k == r"commandlist\efmiv1\callback_component_drawcustom" and v.lower().startswith("ref ")), None)
    for root in roots:
        if identities[root] and callback(root) and not re.search(r"_lod\d*\b", root.name, re.I) and meshes[identities[root][1]]:
            callbacks[callback(root)].add(identities[root])
    for root in roots:
        if re.search(r"_lod\d*\b", root.name, re.I):
            known = callbacks.get(callback(root), set())
            if len(known) == 1:
                identities[root] = next(iter(known))

    owners, groups, cycles = defaultdict(set), defaultdict(set), set()
    for root in roots:
        reached, cyclic = closure(root)
        context = identities[root]
        if context:
            groups[context].update(reached)
            if cyclic:
                cycles.add(context)
        for sec in reached:
            owners[sec].add(context)

    edits, inserts, tails = defaultdict(dict), defaultdict(lambda: defaultdict(list)), defaultdict(list)
    definitions = defaultdict(dict)
    reset_defined = set()
    reports = []
    used_ids = defaultdict(set)
    for doc in documents:
        used_ids[doc.scope].update(int(n) for n in re.findall(r"Component(\d+)", doc.text, re.I))

    for context, reached in sorted(groups.items()):
        scope, mesh = context
        context_roots = [r for r in roots if identities[r] == context]
        home = context_roots[0].doc
        roles = meshes[mesh]
        if any(MARKER in s.doc.text for s in reached):
            reports.append(dict(ib=mesh, file=str(home.path), status="skipped",
                                reason="Already converted; restore before converting again"))
            continue
        todo, set_calls, inputs = [], [], defaultdict(set)
        reason = ""
        for sec in sorted(reached, key=lambda s: (str(s.doc.path), s.start)):
            for line, key, value in sec.commands():
                basic, slot, call = BASIC.fullmatch(key), SLOT.fullmatch(key), SET.fullmatch(value) if key == "run" else None
                if basic:
                    role = INTERFACE_ROLES[basic[2].lower()]
                    inputs[basic[1].lower()].add(role)
                    todo.append((sec, line, "interface", role, value))
                elif call:
                    set_calls.append((sec, line, call[1].lower()))
                elif slot and int(slot[1]) < 32:
                    role = semantic_role(value)
                    if role is None:
                        candidates = [r for r, v in roles.items() if v["slot"] == int(slot[1])]
                        role = candidates[0] if len(candidates) == 1 else None
                    if role:
                        todo.append((sec, line, "slot", role, value))
                elif key.startswith("post ") and (BASIC.search(key) or SLOT.fullmatch(key[5:]) or SET.fullmatch(value)):
                    reason = "Post-phase material writes require manual review"
        if not todo:
            continue
        affected = {s for s, *_ in todo} | {s for s, *_ in set_calls}
        affected.update(s for s in reached if any(BASIC.search(s.doc.lines[i].split(";", 1)[0])
                                                 for i in range(s.start + 1, s.end)))
        if any(owners[s] != {context} for s in affected):
            reason = "Material command list has callers with different or unknown IBs"
        for sec in reached:
            for _, key, value in sec.commands():
                if key == "post run":
                    target = resolve(sec, value, index)
                    if target and closure(target)[0] & affected:
                        reason = "Post-phase material command list requires review"
        if any(s.doc.scope != scope for s in reached):
            reason = "Cross-namespace command list requires manual review"
        if any(s.doc.error for s in reached):
            reason = next(s.doc.error for s in reached if s.doc.error)
        if context in cycles:
            reason = "Recursive command list requires manual review"
        if any(missing[s] for s in reached):
            reason = "Unresolved command list: " + next(t for s in reached for t in missing[s])
        needed = {r for _, _, _, r, _ in todo}
        if needed - roles.keys():
            reason = "No unique confident native fingerprint for: " + ", ".join(sorted(needed - roles.keys()))
        if any(not RESOURCE.fullmatch(v) for _, _, _, _, v in todo):
            reason = "Material source is not a Resource reference or null"
        for interface in inputs:
            if not any(it == interface for _, _, it in set_calls):
                reason = "Interface inputs have no reachable SetTextures consumer"
        for _, _, interface in set_calls:
            if not inputs[interface]:
                reason = "SetTextures consumes inputs outside the known call graph"
        for interface in inputs:
            entries = set(context_roots) | {callback(r) for r in context_roots if callback(r)}
            if any(pending_interface(s, interface, index) for s in entries):
                reason = "Interface inputs can escape this IB without a SetTextures consumer"
        # Unconsumed interface inputs in unrelated entry points cannot be remapped safely.
        for interface in inputs:
            other_roots = [r for r in roots if r not in reached and r.doc.scope == scope]
            entries = set(other_roots) | {callback(r) for r in other_roots if callback(r)}
            if any(pending_interface(s, interface, index) for s in entries):
                reason = "Interface state can escape another entry point"
            for sec in sections:
                if sec in reached or sec.doc.scope != scope:
                    continue
                if any((m := BASIC.fullmatch(k)) and m[1].lower() == interface for _, k, _ in sec.commands()):
                    if not owners[sec] and pending_interface(sec, interface, index):
                        reason = "Interface state is also written outside a known IB context"

        candidates = set()
        for root in context_roots:
            target = callback(root)
            match = re.search(r"Component(\d+)", target.name, re.I) if target else None
            match = match or re.search(r"Component(\d+)", root.name, re.I)
            if match:
                candidates.add(int(match[1]))
        if len(candidates) > 1:
            reason = "One material context has conflicting component identifiers"
        component = next(iter(candidates)) if candidates else next(n for n in range(100000) if n not in used_ids[scope])
        filter_names = {r: f"Filter_Component{component}_{ROLES[r]}" for r in needed}
        for role in needed & roles.keys():
            name, definition = filter_names[role], roles[role]
            existing = index.get((scope, name.lower()), [])
            if existing and (len(existing) != 1 or existing[0].values().get("match_dhash", "").lower() != definition["dhash"]
                             or existing[0].values().get("match_format", "").upper() != definition["format"]
                             or existing[0].values().keys() - {"match_format", "match_dhash"}):
                reason = "Existing Filter definition conflicts: " + name
            old = definitions[scope].get(name)
            if old and old != definition:
                reason = "Component identifier collides with another IB"
        for interface in inputs:
            for role in ROLES:
                name = f"Resource_EFMIRepair_Component{component}_{interface}_{ROLES[role]}"
                if index.get((scope, name.lower())):
                    reason = "Reserved staging resource already exists: " + name
        if any(index.get((scope, name.lower())) for name in (RESET, POOL, RESET + "_Save", RESET + "_Load")):
            reason = "Reserved texture-repair helper already exists"
        if any(re.search(r"\$efmi_repair_slot\b", d.text, re.I) for d in documents if d.scope == scope):
            reason = "Reserved texture-repair variable already exists"

        managed = {}
        for root in context_roots:
            branch, _ = closure(root)
            managed[root] = bool(callback(root)) and any(
                k == "run" and v.lower() == r"commandlist\efmiv1\component_drawinstances"
                for s in branch for _, k, v in s.commands())
            snapshots = [(s, k, v.lower()) for s in branch for _, k, v in s.commands()
                         if v.lower() in {"snapshotinitialtextures", "restoreinitialtextures"}]
            if snapshots and not managed[root]:
                reason = "Manual texture snapshot order requires review"
        report = dict(ib=mesh, file=str(home.path), component=component)
        if reason:
            reports.append(dict(report, status="skipped", reason=reason))
            continue

        used_ids[scope].add(component)
        def stage(interface, role):
            return f"Resource_EFMIRepair_Component{component}_{interface}_{ROLES[role]}"

        def put(sec, i, lines):
            old = sec.doc.lines[i]
            indent = re.match(r"\s*", old.rstrip("\r\n"))[0]
            comment = ";" + old.split(";", 1)[1].rstrip("\r\n") if ";" in old else ""
            if comment:
                lines = [comment] + lines
            edits[sec.doc][i] = "".join(indent + line + sec.doc.nl for line in lines)

        for sec, i, kind, role, value in todo:
            if kind == "slot":
                value = value if value.lower().startswith("ref ") or value.lower() == "null" else "ref " + value
                put(sec, i, ["run = " + RESET, f"ps-t[{filter_names[role]}] = {value}"])
        # Substitute staging references in conditions and copies as well as assignments.
        for sec in reached:
            for i in range(sec.start + 1, sec.end):
                original = edits[sec.doc].get(i, sec.doc.lines[i])
                code, delimiter, comment = original.partition(";")
                if BASIC.search(code):
                    edits[sec.doc][i] = BASIC.sub(lambda m: stage(m[1].lower(), INTERFACE_ROLES[m[2].lower()])
                                                  if m[1].lower() in inputs else m[0], code) + delimiter + comment
        for sec, i, interface in set_calls:
            generated = ["run = " + RESET]
            for role in sorted(inputs[interface]):
                name = stage(interface, role)
                generated += [f"if @{name}", f"    ps-t[{filter_names[role]}] = ref {name}", "endif"]
            for role in ROLES:
                generated.append(stage(interface, role) + " = null")
            # RabbitFX still owns discard/rain bindings and their cleanup flag.
            auxiliary = interface == "rabbitfx" and any(re.search(r"Resource\\RabbitFX\\(?:DiscardMap|Rainmap)\b", s.doc.lines[j], re.I)
                           for s in reached for j in range(s.start + 1, s.end))
            if auxiliary:
                generated += [f"Resource\\RabbitFX\\{r} = null" for r in ("Diffuse", "LightMap", "NormalMap")]
                generated.append(r"run = CommandList\RabbitFX\SetTextures")
            put(sec, i, generated)
        for root, already_managed in managed.items():
            if not already_managed:
                inserts[root.doc][root.start + 1].append("run = SnapshotInitialTextures" + root.doc.nl)
                inserts[root.doc][root.end].append("post run = RestoreInitialTextures" + root.doc.nl + root.doc.nl)
        for role in sorted(needed):
            name = filter_names[role]
            if name not in definitions[scope] and not index.get((scope, name.lower())):
                v = roles[role]
                tails[home] += [f"[{name}]", "match_format = " + v["format"], "match_dhash = " + v["dhash"], ""]
            definitions[scope][name] = roles[role]
        for interface in inputs:
            for role in ROLES:
                tails[home] += ["[" + stage(interface, role) + "]", ""]
        if scope not in reset_defined:
            # XXMI accepts only strictly closer matches within one original draw.
            # Restore resets that ranking, but would erase other per-draw bindings.
            # Preserve the current declared SRVs around the reset, including nulls.
            var = "$efmi_repair_slot"
            tails[home] += ["[Constants]", "global " + var + " = 0", "",
                "[" + POOL + "]", "pool_size = 128", "",
                "[" + RESET + "]", var + " = 0", "run = " + RESET + "_Save",
                "run = RestoreInitialTextures", var + " = 0", "run = " + RESET + "_Load", "",
                "[" + RESET + "_Save]", f"if ps->srv_type({var}) > 0",
                f"    {POOL}[{var}] = ref ps-t[{var}]", "endif",
                var + " = " + var + " + 1", "if " + var + " < 128", "    run = " + RESET + "_Save", "endif", "",
                "[" + RESET + "_Load]", f"if ps->srv_type({var}) > 0",
                f"    ps-t[{var}] = ref {POOL}[{var}]", f"    {POOL}[{var}] = null", "endif",
                var + " = " + var + " + 1", "if " + var + " < 128", "    run = " + RESET + "_Load", "endif", ""]
            reset_defined.add(scope)
        reports.append(dict(report, status="converted", bindings=len(todo), set_texture_calls=len(set_calls)))

    result = {}
    for doc in documents:
        if not edits[doc] and not inserts[doc] and not tails[doc]:
            continue
        out = [MARKER + "; game " + str(catalog.get("game_version")) + doc.nl]
        for i in range(len(doc.lines) + 1):
            if inserts[doc].get(i) and out and not out[-1].endswith(("\n", "\r")):
                out.append(doc.nl)
            out.extend(inserts[doc].get(i, []))
            if i < len(doc.lines):
                out.append(edits[doc].get(i, doc.lines[i]))
        text = "".join(out)
        if tails[doc]:
            text = text.rstrip("\r\n") + doc.nl * 2 + doc.nl.join(tails[doc]) + doc.nl
        # An EOF without newline must not concatenate an inserted post command.
        if any(not line.endswith(("\r", "\n")) for line in doc.lines[:-1]):
            raise ValueError("Unexpected line boundary")
        result[doc.path] = (doc.raw, doc.encode(text))
    return result, reports


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".efmi-repair-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def receipt_paths(path):
    return path.with_name(path.name + BACKUP), path.with_name(path.name + RECEIPT)


def apply_changes(changes, game_version):
    """Prepare all backups before replacing any INI; recover partial writes safely."""
    prepared, written = [], []
    for path, (before, after) in changes.items():
        backup, receipt = receipt_paths(path)
        if not regular(path) or path.read_bytes() != before:
            raise ValueError("File changed during scan: " + str(path))
        if backup.exists() or receipt.exists():
            if not (backup.exists() and receipt.exists() and regular(backup) and regular(receipt)):
                raise ValueError("Incomplete backup pair: " + str(path))
            meta = json.loads(receipt.read_text(encoding="utf-8"))
            if sha(before) != meta.get("original_sha256") or backup.read_bytes() != before:
                raise ValueError("Existing backup does not match original: " + str(path))
        prepared.append((path, backup, receipt, before, after))
    try:
        for path, backup, receipt, before, after in prepared:
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(before)
                    stream.flush()
                    os.fsync(stream.fileno())
            meta = dict(tool=VERSION, game_version=game_version, original_sha256=sha(before),
                        converted_sha256=sha(after), state="prepared")
            atomic_write(receipt, (json.dumps(meta, indent=2) + "\n").encode())
        for path, backup, receipt, before, after in prepared:
            if path.read_bytes() != before:
                raise ValueError("File changed before replacement: " + str(path))
            atomic_write(path, after)
            written.append((path, before, after))
            meta = json.loads(receipt.read_text(encoding="utf-8"))
            meta["state"] = "converted"
            atomic_write(receipt, (json.dumps(meta, indent=2) + "\n").encode())
    except Exception:
        for path, before, after in reversed(written):
            if path.read_bytes() == after:
                atomic_write(path, before)
        raise


def restore(root):
    restored, errors = [], []
    for receipt in walk_files(root, RECEIPT):
        path = receipt.with_name(receipt.name[:-len(RECEIPT)])
        backup, _ = receipt_paths(path)
        try:
            if not path.is_file() or not backup.is_file() or not regular(path) or not regular(backup):
                raise ValueError("Original or backup is missing or linked")
            meta = json.loads(receipt.read_text(encoding="utf-8"))
            original, current = backup.read_bytes(), path.read_bytes()
            if sha(original) != meta["original_sha256"]:
                raise ValueError("Backup checksum mismatch")
            if sha(current) not in {meta["original_sha256"], meta["converted_sha256"]}:
                raise ValueError("INI has subsequent edits; preserved instead of overwriting them")
            if current != original:
                atomic_write(path, original)
                restored.append(str(path))
            meta["state"] = "restored"
            atomic_write(receipt, (json.dumps(meta, indent=2) + "\n").encode())
        except (ValueError, KeyError, OSError) as exc:
            errors.append(dict(file=str(path), reason=str(exc)))
    return restored, errors


def validate_report(path, catalog_path=None):
    """Validate destinations before an INI or backup can be modified."""
    if path is None:
        return None
    if path.exists() and not regular(path):
        raise ValueError("Report must not be a link")
    path = path.resolve()
    protected = {Path(__file__).resolve()}
    if catalog_path:
        protected.add(catalog_path.resolve())
    if (path.suffix.lower() != ".json" or path.name.lower().endswith(RECEIPT)
            or path in protected or not path.parent.is_dir()):
        raise ValueError("Report requires a separate .json file in an existing directory")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("tool") != "efmi-texture-repair":
            raise ValueError("Refusing to overwrite a file that is not a texture-repair report")
    return path


def print_result(result):
    if result["mode"] == "restore":
        print(f"Restored {len(result['restored'])} file(s); {len(result['errors'])} error(s).")
        for item in result["errors"]:
            print("PRESERVED " + item["file"] + ": " + item["reason"])
        return
    converted = [i for i in result["entries"] if i["status"] == "converted"]
    skipped = [i for i in result["entries"] if i["status"] == "skipped"]
    print(f"{result['mode'].upper()}: scanned {result['scanned']} INIs; "
          f"{len(result['changed_files'])} file(s), {len(converted)} material context(s); {len(skipped)} skipped.")
    print("Catalog game version: " + result["game_version"])
    for path in result["changed_files"]:
        print(("WOULD CHANGE " if result["mode"] == "preview" else "CONVERTED ") + path)
    for item in skipped:
        print("SKIPPED " + item["file"] + " [" + item["ib"] + "]: " + item["reason"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="Recursively scan this directory; default: the script's folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--restore", action="store_true", help="Restore originals from verified adjacent backups")
    mode.add_argument("--dry-run", action="store_true", help="Report proposed conversions without changing INIs")
    parser.add_argument("--catalog", type=Path, help="Override the embedded catalog with a catalog or slot-table JSON")
    parser.add_argument("--report", type=Path, help="Write a JSON result report")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("Scan root is not a directory")
    try:
        report_path = validate_report(args.report, args.catalog)
        if args.restore:
            files, errors = restore(root)
            result = dict(mode="restore", restored=files, errors=errors)
            code = 1 if errors else 0
        else:
            catalog = load_catalog(args.catalog)
            docs = [Document(path, path.read_bytes()) for path in walk_files(root, ".ini")]
            changes, items = plan(docs, catalog)
            if not args.dry_run:
                apply_changes(changes, catalog.get("game_version"))
            result = dict(mode="preview" if args.dry_run else "convert", game_version=catalog.get("game_version"),
                          scanned=len(docs), changed_files=[str(p) for p in changes], entries=items)
            code = 0
        result["tool"] = "efmi-texture-repair"
        print_result(result)
        if report_path:
            atomic_write(report_path, (json.dumps(result, indent=2) + "\n").encode())
        return code
    except (OSError, ValueError, KeyError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) == 1 and sys.stdin.isatty():
        input("Press Enter to close. Use --restore to restore originals, or --dry-run to preview. ")
    raise SystemExit(exit_code)
