"""Which XXMI installation a folder is, and what differs between them.

The probe and most tools work at the D3D11 level and do not care which game
they are in. A few things do, and they are read from the installation itself:

- its name (EFMI, ZZMI, ...), from the core main.ini that d3dx.ini includes;
- the game executable it targets ([Loader] target);
- how XXMI hashes index and vertex buffers: with [Rendering]
  track_region_hashes=1 (EFMI) a mod's ``hash =`` is the hash of the part of
  the buffer a draw uses, with 0 (ZZMI) it is the creation hash of the whole
  buffer;
- the key that switches all mods off and on, and whether the core prints a
  notification that tells the state afterwards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .mods import ini_sections, key_tokens

_CORE_INCLUDE = re.compile(r"^core[\\/]+([^\\/]+)[\\/]+main\.ini$", re.I)
_SECTION = re.compile(r"^\s*\[(.+?)\]\s*$")
_ENTRY = re.compile(r"^\s*(\$?[A-Za-z_][\w ]*?)\s*=\s*(.*?)\s*$")     # also "$variable = values"


def _key_sections(path: Path, section: str) -> list[list[tuple[str, str]]]:
    """Every [section] of one ini as its (key, value) pairs, keys in lower case, variables included."""
    out: list[list[tuple[str, str]]] = []
    current = None
    for line in path.read_bytes().decode("utf-8-sig", errors="replace").splitlines():
        if line.lstrip().startswith(";"):
            continue
        m = _SECTION.match(line)
        if m:
            current = [] if m.group(1).strip().lower() == section else None
            if current is not None:
                out.append(current)
            continue
        m = _ENTRY.match(line)
        if m and current is not None:
            current.append((m.group(1).strip().lower(), m.group(2)))
    return out


@dataclass(frozen=True)
class ToggleSpec:
    """How an installation switches all mods off and on."""
    key: str                 # e.g. "F11"
    vk: str                  # e.g. "vk_f11", as bindings are compared
    binding_file: str        # relative to the installation root
    variable: str            # the core variable the key flips
    notifies: bool           # whether the core prints a text that tells the new state


@dataclass(frozen=True)
class Install:
    root: Path
    name: str                         # the core's folder name: "EFMI", "ZZMI", "GIMI", ...
    target: str                       # the game executable d3dx.ini targets, lower case; "" if none
    region_hashes: bool               # [Rendering] track_region_hashes
    toggle: ToggleSpec | None = None
    notes: tuple = field(default_factory=tuple)

    @property
    def is_efmi(self) -> bool:
        return self.name == "EFMI"


EFMI_TOGGLE = ToggleSpec("F11", "vk_f11", r"Core\EFMI\KeyBindings.ini", "$enable_mods", True)
ZZMI_TOGGLE = ToggleSpec("F6", "vk_f6", r"Core\ZZMI\main.ini", "$costume_mods", False)


def _values(sections, section: str, key: str) -> list[str]:
    return [value.strip() for name, entries in sections if name.lower() == section
            for k, value in entries if k == key]


def detect(root: str | Path) -> Install:
    """Reads d3dx.ini and the core of the installation at ``root``; never writes."""
    root = Path(root)
    try:
        main = ini_sections(root / "d3dx.ini")
    except OSError:
        main = []
    core = ""
    for value in _values(main, "include", "include"):
        m = _CORE_INCLUDE.match(value.replace("/", "\\"))
        if m:
            core = m.group(1).upper()
            break
    if not core:
        for candidate in ("EFMI", "ZZMI"):
            if (root / "Core" / candidate / "main.ini").is_file():
                core = candidate
                break
    notes = ()
    if not core:
        # Nothing names another game: the installation this server was first built for.
        core = "EFMI"
        notes = ("no core main.ini found; treated as EFMI",)
    targets = _values(main, "loader", "target")
    region = _values(main, "rendering", "track_region_hashes")
    try:
        region_hashes = bool(int(region[-1].split(";", 1)[0].strip())) if region else core == "EFMI"
    except ValueError:
        region_hashes = core == "EFMI"
    toggle = {"EFMI": EFMI_TOGGLE, "ZZMI": ZZMI_TOGGLE}.get(core)
    return Install(root=root, name=core, target=(targets[-1].lower() if targets else ""),
                   region_hashes=region_hashes, toggle=toggle, notes=notes)


def toggle_binding(install: Install) -> dict:
    """The installation's binding of its mod switch, checked as the probe checks it; raises ValueError otherwise.

    Read only; a changed binding is reported, never assumed or changed.
    """
    spec = install.toggle
    if spec is None:
        raise ValueError(f"{install.root}: this XXMI installation ({install.name}) has no known key for switching all "
                         "mods; only EFMI (F11) and ZZMI (F6) are supported. No key was sent.")
    path = install.root / spec.binding_file
    try:
        found = _key_sections(path, "keytogglemods")
    except OSError:
        found = []
    keys = [[t if t.startswith(("vk_", "no_")) else "vk_" + t for t in key_tokens(value)]
            for entries in found for key, value in entries if key == "key"]
    if spec is EFMI_TOGGLE:
        runs = [value.strip().lower() for entries in found for key, value in entries if key == "run"]
        ok = len(found) == 1 and keys == [["no_modifiers", spec.vk]] and runs == ["commandlisttogglemods"]
        wanted = "'Key = no_modifiers VK_F11' and 'run = CommandListToggleMods'"
    else:
        cycle = [" ".join(value.lower().split()) for entries in found for key, value in entries if key == "$costume_mods"]
        types = [value.strip().lower() for entries in found for key, value in entries if key == "type"]
        ok = len(found) == 1 and keys == [["no_modifiers", spec.vk]] and cycle == ["0, 1"] and types == ["cycle"]
        wanted = "'Key = no_modifiers F6', '$costume_mods = 0, 1' and 'type = cycle'"
    if not ok:
        raise ValueError(f"{path}: expected one [KeyToggleMods] with {wanted}. No key was sent; custom bindings are "
                         "not changed automatically.")
    for name, entries in ini_sections(install.root / "d3dx.ini"):
        if name.lower() != "hunting":
            continue
        for key, value in entries:
            tokens = [t if t.startswith(("vk_", "no_")) or t in ("ctrl", "alt", "shift", "windows") else "vk_" + t
                      for t in key_tokens(value)]
            if spec.vk in tokens and all(t == spec.vk or t.startswith("no_") for t in tokens):
                raise ValueError(f"{install.root / 'd3dx.ini'}: [Hunting] {key} is bound to {spec.key} without a "
                                 f"modifier, so {spec.key} would trigger it as well. No key was sent.")
    return {"KeyToggleMods": f"no_modifiers {'VK_' if spec is EFMI_TOGGLE else ''}{spec.key}",
            "file": spec.binding_file, "variable": spec.variable}
