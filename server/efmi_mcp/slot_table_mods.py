"""Read-only mod declarations for comparison AFTER native slot analysis.

Names describe INI authors' intent, not proof of the native shader role.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections import defaultdict

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
