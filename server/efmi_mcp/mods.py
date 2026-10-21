"""Read-only index of the hashes installed mods refer to.

Nothing here writes to the EFMI folder: the ini files are only read, the same
ones XXMI itself would load (d3dx.ini [Include] rules are followed).
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_RE_SECTION = re.compile(r"^\s*\[(.+?)\]\s*$")
_RE_KEY = re.compile(r"^\s*([A-Za-z_][\w ]*?)\s*=\s*(.*?)\s*$")
_RE_HASH = re.compile(r"^[0-9a-fA-F]{8}([0-9a-fA-F]{8})?$")


@dataclass
class HashRef:
    mod: str            # top-level folder (or file) under the include root
    ini: str            # path relative to the EFMI root
    section: str
    kind: str           # "texture" (TextureOverride) or "shader" (ShaderOverride)
    hash: str
    namespace: str | None = None
    match: dict = field(default_factory=dict)   # match_first_index / match_index_count ...
    skips: bool = False     # the section has 'handling = skip': the game's own draw is not issued


@dataclass
class ModIndex:
    root: Path
    refs: list[HashRef]
    by_hash: dict[str, list[HashRef]]
    mods: dict[str, dict]
    scanned_at: float
    include_roots: list[str]
    excludes: list[str]


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _include_rules(root: Path) -> tuple[list[str], list[str]]:
    """include_recursive / exclude_recursive from d3dx.ini's [Include] section."""
    includes: list[str] = []
    excludes: list[str] = []
    ini = root / "d3dx.ini"
    if ini.exists():
        section = ""
        for line in _read_text(ini).splitlines():
            m = _RE_SECTION.match(line)
            if m:
                section = m.group(1).strip().lower()
                continue
            if section != "include" or line.lstrip().startswith(";"):
                continue
            m = _RE_KEY.match(line)
            if not m:
                continue
            key, val = m.group(1).strip().lower(), m.group(2)
            if key == "include_recursive":
                includes.append(val)
            elif key == "exclude_recursive":
                excludes.append(val)
    if not includes:
        includes = ["Mods"]
    return includes, excludes


def _excluded(name: str, excludes: list[str]) -> bool:
    return any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in excludes)


def _parse_ini(path: Path, rel: str, mod: str) -> tuple[list[HashRef], str | None, int]:
    refs: list[HashRef] = []
    namespace = None
    section = ""
    current: HashRef | None = None
    pending_match: dict = {}
    pending_skip = False
    sections = 0
    for line in _read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        m = _RE_SECTION.match(line)
        if m:
            section = m.group(1).strip()
            sections += 1
            current = None
            pending_match = {}
            pending_skip = False
            continue
        m = _RE_KEY.match(line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2)
        if not section and key == "namespace":
            namespace = val
            continue
        low = section.lower()
        if low.startswith("textureoverride") or low.startswith("shaderoverride"):
            if key == "hash" and _RE_HASH.match(val):
                current = HashRef(mod, rel, section, "texture" if low.startswith("texture") else "shader",
                                  val.lower(), namespace, dict(pending_match), pending_skip)
                refs.append(current)
            elif key == "handling" and "skip" in val.lower().split():
                if current is not None:
                    current.skips = True
                else:
                    pending_skip = True
            elif key.startswith("match_"):
                if current is not None:
                    current.match[key] = val
                else:
                    pending_match[key] = val
    for r in refs:
        r.namespace = namespace
    return refs, namespace, sections


def scan_mods(efmi_root: str | os.PathLike) -> ModIndex:
    root = Path(efmi_root)
    includes, excludes = _include_rules(root)
    refs: list[HashRef] = []
    mods: dict[str, dict] = {}
    for inc in includes:
        base = root / inc
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not _excluded(d, excludes)]
            for fn in filenames:
                if not fn.lower().endswith(".ini") or _excluded(fn, excludes):
                    continue
                full = Path(dirpath) / fn
                rel_to_base = full.relative_to(base)
                mod = rel_to_base.parts[0]
                rel = str(full.relative_to(root))
                try:
                    found, namespace, sections = _parse_ini(full, rel, mod)
                except OSError:
                    continue
                refs.extend(found)
                info = mods.setdefault(mod, {"inis": [], "namespaces": [], "hashes": 0, "sections": 0})
                info["inis"].append(rel)
                info["sections"] += sections
                info["hashes"] += len(found)
                if namespace and namespace not in info["namespaces"]:
                    info["namespaces"].append(namespace)
    by_hash: dict[str, list[HashRef]] = {}
    for r in refs:
        by_hash.setdefault(r.hash, []).append(r)
    return ModIndex(root, refs, by_hash, mods, time.time(), includes, excludes)


def ini_sections(path: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """One ini as [(section, [(key, value), ...])]: comment lines dropped, keys in lower case."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for line in _read_text(path).splitlines():
        if line.lstrip().startswith(";"):
            continue
        m = _RE_SECTION.match(line)
        if m:
            sections.append((m.group(1).strip(), []))
            continue
        m = _RE_KEY.match(line)
        if m and sections:
            sections[-1][1].append((m.group(1).strip().lower(), m.group(2)))
    return sections


def key_tokens(value: str) -> list[str]:
    """A key binding as lower-case tokens, without a trailing comment."""
    return value.split(";", 1)[0].lower().split()


def is_plain_key(tokens: list[str], vk: str) -> bool:
    """True when the key alone satisfies the binding: no modifier or second key is required."""
    return vk in tokens and all(token == vk or token.startswith("no_") for token in tokens)


def plain_key_sections(efmi_root: str | os.PathLike, vk: str) -> list[dict]:
    """[Key...] sections that pressing the key alone triggers, in the ini files XXMI may load.

    Core and the include roots are searched. A file under Core that d3dx.ini
    never includes can show up here too: the list says what to look at, not
    what is certain to fire.
    """
    root = Path(efmi_root)
    includes, excludes = _include_rules(root)
    found = []
    for base in dict.fromkeys(["Core", *includes]):
        if not (root / base).is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root / base):
            dirnames[:] = [d for d in dirnames if not _excluded(d, excludes)]
            for fn in filenames:
                if not fn.lower().endswith(".ini") or _excluded(fn, excludes):
                    continue
                full = Path(dirpath) / fn
                try:
                    sections = ini_sections(full)
                except OSError:
                    continue
                for name, entries in sections:
                    if name.lower().startswith("key") and any(
                            key == "key" and is_plain_key(key_tokens(value), vk) for key, value in entries):
                        found.append({"ini": str(full.relative_to(root)), "section": name})
    return found


class ModCache:
    """Rescans at most every few seconds; mods change while people work on them."""

    def __init__(self, efmi_root: str | os.PathLike, max_age: float = 5.0):
        self.root = Path(efmi_root)
        self.max_age = max_age
        self._index: ModIndex | None = None

    def get(self) -> ModIndex:
        if self._index is None or time.time() - self._index.scanned_at > self.max_age:
            self._index = scan_mods(self.root)
        return self._index

    def invalidate(self) -> None:
        """Force the next lookup to see files changed immediately before reload."""
        self._index = None
