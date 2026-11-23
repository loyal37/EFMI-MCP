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

VERSION = "1.3.0"
CATALOG_B85 = ""
MARKER = "; EFMI texture repair"
BACKUP = ".efmi-texture-repair.bak"
RECEIPT = ".efmi-texture-repair.json"
ROLES = {"diffuse": "Diffuse", "lightmap": "Light", "normal": "Normal"}
INTERFACE_ROLES = {"diffuse": "diffuse", "lightmap": "lightmap", "normalmap": "normal"}
HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$")
BASIC = re.compile(r"Resource\\(RabbitFX|EFMITTL)\\(Diffuse|LightMap|NormalMap)\b", re.I)
SET = re.compile(r"CommandList\\(RabbitFX|EFMITTL)\\SetTextures$", re.I)
SLOT = re.compile(r"ps-t(\d+)$", re.I)
RESOURCE = re.compile(r"(?:ref\s+)?(Resource[\w.\\-]+|null)$", re.I)
SURE = {"dataflow", "dataflow+position", "dataflow+format", "sequence", "format", "other pass"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def application_path():
    """A one-file executable extracts __file__ into a temporary directory."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()


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


def pending_interface(sec, interface, index, pending=None, active=None, direct_errors=None):
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
            if direct_errors is not None and m[2].lower() in state:
                direct_errors.add("Interface input is overwritten before SetTextures; direct binding would change its meaning")
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
                    state = pending_interface(target, interface, index, state, active, direct_errors)
                elif state and direct_errors is not None:
                    direct_errors.add("External command runs before SetTextures consumes its inputs")
        elif state and direct_errors is not None and (key.startswith("draw") or "ps-t" in code):
            direct_errors.add("A draw or slot operation occurs before SetTextures consumes its inputs")
    return state | (set(INTERFACE_ROLES) if stack else set())


def lifecycle_insertions(sec, post_restore):
    """Complete only an unambiguous standard entry/exit texture lifecycle."""
    commands, code_lines, depth = [], [], 0
    for i in range(sec.start + 1, sec.end):
        code = sec.doc.lines[i].split(";", 1)[0].strip()
        if not code:
            continue
        code_lines.append(i)
        item = assignment(code)
        if item:
            commands.append((i, *item, depth))
        if re.match(r"if\s", code, re.I):
            depth += 1
        elif code.lower() == "endif":
            depth -= 1
    snapshots = [c for c in commands if c[2].lower() == "snapshotinitialtextures"]
    restores = [c for c in commands if c[2].lower() == "restoreinitialtextures"]
    if len(snapshots) > 1 or len(restores) > 1:
        raise ValueError("Multiple manual texture lifecycle calls require review")
    if snapshots:
        i, key, _, nesting = snapshots[0]
        prior = [assignment(sec.doc.lines[j]) for j in code_lines if j < i]
        if key != "run" or nesting or any(not item or not (
                item[0] in {"hash", "handling", "filter_index"} or item[0].startswith("match_")) for item in prior):
            raise ValueError("Manual snapshot is conditional or follows executable commands")
    if restores:
        i, key, _, nesting = restores[0]
        expected = "post run" if post_restore else "run"
        # Explicitly skipped original draws can restore before the game call.
        allowed = {expected}
        if post_restore and sec.values().get("handling", "").lower() == "skip":
            allowed.add("run")
        if key not in allowed or nesting or i != code_lines[-1]:
            raise ValueError("Manual restore phase or placement requires review")
    insertions = []
    if not snapshots:
        insertions.append((sec.start + 1, "run = SnapshotInitialTextures" + sec.doc.nl))
    if not restores:
        restore = ("post run" if post_restore else "run") + " = RestoreInitialTextures"
        insertions.append((code_lines[-1] + 1, sec.doc.nl + restore + sec.doc.nl))
    return insertions


def material_restore_insertions(entries, todo, index, filter_names):
    """Reset matching when a later material changes, retaining known bindings."""
    bindings = {(s, i): (f"ps-t[{filter_names[r]}]", v) for s, i, _, r, v in todo}
    additions, spellings, visited = {}, {}, set()

    def value_id(value):
        value = value if value.lower().startswith("ref ") or value.lower() == "null" else "ref " + value
        spellings[value.lower()] = value
        return value.lower()

    def merge(states):
        keys = set().union(*(s.keys() for s in states))
        return {k: frozenset().union(*(s.get(k, {None}) for s in states)) for k in keys}

    def walk(sec, initial):
        state, branches = dict(initial), []
        ends, nesting = {}, []
        for i in range(sec.start + 1, sec.end):
            code = sec.doc.lines[i].split(";", 1)[0].strip().lower()
            if code.startswith("if "):
                nesting.append([i])
            elif code.startswith("elif ") or code == "else":
                nesting[-1].append(i)
            elif code == "endif":
                for start in nesting.pop():
                    ends[start] = i

        def overwritten_before_draw(start):
            overwritten, i = set(), start + 1
            while i < sec.end:
                code = sec.doc.lines[i].split(";", 1)[0].strip().lower()
                if code.startswith("elif ") or code == "else":
                    i = ends[i] + 1
                    continue
                if code.startswith("if "):
                    break
                item = assignment(sec.doc.lines[i])
                binding = bindings.get((sec, i))
                if binding and binding[1].lower() != "null":
                    overwritten.add(binding[0])
                elif item:
                    key, value = item
                    if key == "run" and SET.fullmatch(value):
                        pass
                    elif key.startswith("draw") or key in {"run", "post run"} or "ps-t" in value.lower():
                        break
                    elif SLOT.fullmatch(key):
                        overwritten.add(key)
                i += 1
            return overwritten

        for i in range(sec.start + 1, sec.end):
            code = sec.doc.lines[i].split(";", 1)[0].strip().lower()
            if code.startswith("if "):
                branches.append([dict(state), [], False])
            elif code.startswith("elif ") or code == "else":
                branches[-1][1].append(state)
                branches[-1][2] |= code == "else"
                state = dict(branches[-1][0])
            elif code == "endif":
                entry, exits, has_else = branches.pop()
                state = merge(exits + [state] + ([] if has_else else [entry]))
            item = assignment(sec.doc.lines[i])
            if not item:
                continue
            key, value = item
            binding = bindings.get((sec, i))
            if binding:
                visited.add((sec, i))
                if value.lower() == "null":
                    continue
                destination, _ = binding
                wanted = value_id(value)
                reset = any(v is not None and v != wanted for v in state.get(destination, ()))
                commands = []
                if reset:
                    commands.append("run = RestoreInitialTextures")
                    overwritten = overwritten_before_draw(i) | {destination}
                    retained = {}
                    for slot, values in state.items():
                        if slot in overwritten or values == {None}:
                            continue
                        if len(values) != 1 or None in values or "unknown" in values:
                            raise ValueError("Material switch would lose a conditional or dynamic texture binding")
                        commands.append(f"{slot} = {spellings[next(iter(values))]}")
                        retained[slot] = values
                    state = retained
                indent = re.match(r"\s*", sec.doc.lines[i].rstrip("\r\n"))[0]
                text = "".join(indent + command + sec.doc.nl for command in commands)
                if (sec, i) in additions and additions[sec, i] != text:
                    raise ValueError("Shared material helper needs different restoration for its callers")
                additions[sec, i] = text
                state[destination] = frozenset({wanted})
            elif SLOT.fullmatch(key):
                state[key] = frozenset({value_id(value) if RESOURCE.fullmatch(value) else "unknown"})
            elif key == "run":
                if value.lower() == "restoreinitialtextures":
                    state.clear()
                else:
                    target = resolve(sec, value, index)
                    if target and not SET.fullmatch(value):
                        state = walk(target, state)
            elif key.startswith("resource"):
                # A later replay must not read a resource alias that has changed.
                reference_id = "ref " + key
                state = {slot: frozenset("unknown" if v == reference_id else v for v in values)
                         for slot, values in state.items()}
        return state

    for entry in entries:
        walk(entry, {})
    if bindings.keys() - visited:
        raise ValueError("Material assignment is outside the known draw lifecycle")
    return {site: text for site, text in additions.items() if text}


def plan(documents, catalog):
    """Follow real run/ref edges; specialize only unambiguous material contexts.

    Only binding destinations and basic-only SetTextures calls are edited.
    Conditions, resources, draw commands and existing snapshot logic stay intact.
    Standard SnapshotInitialTextures/RestoreInitialTextures bracket the draw.
    No staging resources, guards or custom command lists are generated.
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

    edits, tails = defaultdict(dict), defaultdict(list)
    inserts = defaultdict(lambda: defaultdict(list))
    wrapped = set()
    definitions = defaultdict(dict)
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
        if any(kind == "slot" and value.lower() == "null" for _, _, kind, _, value in todo):
            reason = "Fixed-slot null writes cannot be expressed by a direct Filter"
        for sec in reached:
            for line in sec.doc.lines[sec.start + 1:sec.end]:
                code = line.split(";", 1)[0]
                item = assignment(line)
                if BASIC.search(code) and (not item or not BASIC.fullmatch(item[0]) or BASIC.search(item[1])):
                    reason = "Interface resource is read outside an input assignment; keep original conditions unchanged"
        for interface in inputs:
            if not any(it == interface for _, _, it in set_calls):
                reason = "Interface inputs have no reachable SetTextures consumer"
        for _, _, interface in set_calls:
            if not inputs[interface]:
                reason = "SetTextures consumes inputs outside the known call graph"
        for interface in inputs:
            entries = set(context_roots) | {callback(r) for r in context_roots if callback(r)}
            direct_errors = set()
            pending = [pending_interface(s, interface, index, direct_errors=direct_errors) for s in entries]
            if any(pending):
                reason = "Interface inputs can escape this IB without a SetTextures consumer"
            if direct_errors:
                reason = sorted(direct_errors)[0]
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
        lifecycle = {}
        for root in context_roots:
            branch = closure(root)[0]
            if not affected.intersection(branch):
                continue
            target = callback(root)
            # Restore a complete draw callback, never a texture-only helper that
            # returns before its caller draws with the new bindings.
            if not target or not affected.intersection(branch).issubset(closure(target)[0]):
                target = root
            target_branch = closure(target)[0]
            if owners[target] != {context}:
                reason = "Texture lifecycle entry has callers with different or unknown IBs"
            if any(v.lower() in {"snapshotinitialtextures", "restoreinitialtextures"}
                   for s in target_branch if s is not target for _, _, v in s.commands()):
                reason = "Nested manual texture lifecycle requires review"
            try:
                lifecycle[target] = lifecycle_insertions(target, post_restore=target is root)
            except ValueError as exc:
                reason = str(exc)
        report = dict(ib=mesh, file=str(home.path), component=component)
        material_restores = {}
        if not reason:
            try:
                material_restores = material_restore_insertions(lifecycle, todo, index, filter_names)
            except ValueError as exc:
                reason = str(exc)
        if reason:
            reports.append(dict(report, status="skipped", reason=reason))
            continue

        used_ids[scope].add(component)
        for (sec, position), text in material_restores.items():
            inserts[sec.doc][position].append(text)
        for sec, additions in lifecycle.items():
            if sec not in wrapped:
                for position, text in additions:
                    inserts[sec.doc][position].append(text)
                wrapped.add(sec)
        def put(sec, i, replacement):
            old = sec.doc.lines[i]
            indent = re.match(r"\s*", old.rstrip("\r\n"))[0]
            comment = ";" + old.split(";", 1)[1].rstrip("\r\n") if ";" in old else ""
            content = replacement + (" " if replacement and comment else "") + comment
            ending = old[len(old.rstrip("\r\n")):]
            edits[sec.doc][i] = indent + content + ending if content else ""

        for sec, i, kind, role, value in todo:
            if kind == "interface" and value.lower() == "null":
                # A null API input means no replacement, not clearing a PS slot.
                put(sec, i, "")
            else:
                value = value if value.lower().startswith("ref ") or value.lower() == "null" else "ref " + value
                put(sec, i, f"ps-t[{filter_names[role]}] = {value}")
        for sec, i, interface in set_calls:
            # Keep the original call verbatim when RabbitFX consumes auxiliary maps.
            auxiliary = interface == "rabbitfx" and any(re.search(r"Resource\\RabbitFX\\(?:DiscardMap|Rainmap)\b", s.doc.lines[j], re.I)
                           for s in reached for j in range(s.start + 1, s.end))
            if not auxiliary:
                put(sec, i, "")
        for role in sorted(needed):
            name = filter_names[role]
            if name not in definitions[scope] and not index.get((scope, name.lower())):
                v = roles[role]
                tails[home] += [f"[{name}]", "match_format = " + v["format"], "match_dhash = " + v["dhash"], ""]
            definitions[scope][name] = roles[role]
        reports.append(dict(report, status="converted", bindings=len(todo), set_texture_calls=len(set_calls),
                            material_restores=len(material_restores), warnings=[]))

    result = {}
    for doc in documents:
        if not edits[doc] and not tails[doc] and not inserts[doc]:
            continue
        out = [MARKER + " " + VERSION + "; game " + str(catalog.get("game_version")) + doc.nl]
        for i in range(len(doc.lines) + 1):
            if inserts[doc].get(i):
                if out and not out[-1].endswith(("\r", "\n")):
                    out.append(doc.nl)
                out.extend(inserts[doc][i])
            if i < len(doc.lines):
                out.append(edits[doc].get(i, doc.lines[i]))
        text = "".join(out)
        if tails[doc]:
            text = text.rstrip("\r\n") + doc.nl * 2 + doc.nl.join(tails[doc]) + doc.nl
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
    protected = {Path(__file__).resolve(), application_path()}
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
    for item in converted:
        for warning in item.get("warnings", []):
            print("NOTE " + item["file"] + " [" + item["ib"] + "]: " + warning)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=application_path().parent,
                        help="Recursively scan this directory; default: the script or EXE's folder")
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
