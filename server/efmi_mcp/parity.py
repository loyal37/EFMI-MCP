"""Does the probe agree with XXMI? Comparing a probe capture with an XXMI frame dump.

Two ways of looking at it:

compare_ordered   draw N of the dump against the N-th draw of the game in the
                  capture. Only meaningful when both show exactly the same
                  frame content (the sandbox's static scene).

compare_sets      frame-independent: identities that appear in both are
                  compared wherever the same draw parameters show up. Works for
                  a dump and a capture taken moments apart in the game.

compare_contents  byte for byte: the buffers and textures XXMI wrote for call N
                  against what the probe kept of the N-th call of the game
                  (a capture taken with contents). Like compare_ordered it needs
                  two recordings of the same frame content.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .capture_model import Capture
from .fa_dump import FrameDump
from .hashes import crc32c, hex32


@dataclass
class ParityResult:
    checks: int = 0
    mismatches: int = 0
    stale: int = 0                 # XXMI's name disagreed with the bytes XXMI itself dumped
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.mismatches == 0 and self.checks > 0


def _bytes_hash(dump: FrameDump, slot, region: dict | None) -> str | None:
    """CRC of the region inside the buffer file XXMI wrote for this slot."""
    name = slot.deduped.get("buf")
    if not region or not name:
        return None
    path = dump.path / "deduped" / name
    if not path.exists():
        return None
    data = path.read_bytes()
    return hex32(crc32c(data[region["offset"]:region["offset"] + region["size"]]))


def compare_ordered(cap: Capture, dump: FrameDump, verbose: bool = False) -> ParityResult:
    res = ParityResult()

    def check(what: str, ours, theirs) -> None:
        res.checks += 1
        if ours != theirs:
            res.mismatches += 1
            res.messages.append(f"  MISMATCH {what}: probe={ours} xxmi={theirs}")
        elif verbose:
            res.messages.append(f"  ok {what}: {ours}")

    def check_region(what: str, mine: dict | None, slot) -> None:
        ours = mine and mine["hash"]
        if ours == slot.hash:
            check(what, ours, slot.hash)
            return
        truth = _bytes_hash(dump, slot, mine and mine.get("region"))
        res.checks += 1
        if truth is not None and truth == ours:
            res.stale += 1
            res.messages.append(f"  note {what}: XXMI named it {slot.hash}, but the bytes it dumped hash to {truth}, "
                                f"which is what the probe reports (XXMI's cached hash was stale)")
        else:
            res.mismatches += 1
            res.messages.append(f"  MISMATCH {what}: probe={ours} xxmi={slot.hash} bytes-in-dump={truth}")

    game = [d for d in cap.draws() if d["origin"] != "xxmi"]
    numbers = [n for n in dump.draw_numbers() if dump.draws[n].call]
    res.messages.append(f"probe: {len(cap)} draws ({len(game)} from the game); xxmi dump: {len(numbers)} draws")
    res.checks += 1
    if len(game) != len(numbers):
        res.mismatches += 1
        res.messages.append("  MISMATCH draw count")

    for ours, n in zip(game, numbers):
        theirs = dump.draws[n]
        tag = f"draw {n:06d} {theirs.call}"
        check(f"{tag} call", ours["call"], theirs.call)
        a = theirs.args
        if theirs.call in ("DrawIndexed", "DrawIndexedInstanced"):
            check(f"{tag} index_count", ours["args"].get("index_count", 0),
                  a.get("IndexCount", a.get("IndexCountPerInstance", 0)))
            check(f"{tag} first_index", ours["args"].get("first_index", 0), a.get("StartIndexLocation", 0))
        for stage, h in theirs.shaders.items():
            check(f"{tag} {stage}", ours["shaders"].get(stage), h)
        for name, slot in sorted(theirs.slots.items()):
            if name == "ib" or name.startswith("vb"):
                mine = ours.get("ib") if name == "ib" else ours.get("vb", {}).get(int(name[2:]))
                if slot.full_hash:
                    # "region(resource)": XXMI hashed the bytes this draw uses.
                    check_region(f"{tag} {name} region hash", mine, slot)
                    check(f"{tag} {name} resource hash", mine and mine["resource_hash"], slot.full_hash)
                else:
                    # A single hash means no region applied and it is the resource hash.
                    check(f"{tag} {name} resource hash", mine and mine["resource_hash"], slot.hash)
            elif "-cb" in name:
                mine = ours.get("cb", {}).get(name)
                check(f"{tag} {name}", mine and mine["hash"], slot.hash)
            elif "-t" in name:
                mine = ours.get("tex", {}).get(name)
                check(f"{tag} {name}", mine and mine["hash"], slot.hash)
            elif name.startswith("o") or name.startswith("u"):
                mine = ours.get("out", {}).get(name)
                check(f"{tag} {name}", mine and mine["hash"], slot.hash)
            else:
                res.messages.append(f"  (slot {name} not compared)")
    return res


def compare_sets(cap: Capture, dump: FrameDump) -> ParityResult:
    """Frame-independent comparison, for a dump and a capture of the same scene."""
    res = ParityResult()

    # What the probe saw, keyed by the parameters that select a mesh.
    ours_ib: dict[tuple, set[str]] = {}
    ours_shaders: set[str] = set()
    ours_tex: set[str] = set()
    for d in cap.draws():
        if d["origin"] == "xxmi":
            continue
        ours_shaders.update(h for h in d["shaders"].values() if h)
        ours_tex.update(v["hash"] for v in d.get("tex", {}).values() if v["hash"])
        ib = d.get("ib")
        if ib and ib["hash"]:
            key = (d["args"].get("index_count", 0), d["args"].get("first_index", 0), ib["offset"])
            ours_ib.setdefault(key, set()).add(ib["hash"])

    same = differ = absent = 0
    for n in dump.draw_numbers():
        dd = dump.draws[n]
        slot = dd.slots.get("ib")
        if not dd.call or not slot or not slot.full_hash:
            continue
        a = dd.args
        ic = a.get("IndexCount", a.get("IndexCountPerInstance", 0))
        fi = a.get("StartIndexLocation", 0)
        # the bind offset is only stated in the name of the text dump
        off = 0
        txt = slot.deduped.get("txt", "")
        if "-offset=" in txt:
            try:
                off = int(txt.split("-offset=")[1].split("-")[0].split(".")[0])
            except ValueError:
                off = 0
        mine = ours_ib.get((ic, fi, off))
        if mine is None:
            absent += 1
            continue
        res.checks += 1
        if slot.hash in mine:
            same += 1
        else:
            differ += 1
            res.mismatches += 1
            if len(res.messages) < 30:
                res.messages.append(f"  MISMATCH draw {n:06d} ib (index_count={ic}, first_index={fi}, offset={off}): "
                                    f"probe={sorted(mine)} xxmi={slot.hash}")
    res.messages.insert(0, f"index buffers: {same} identical, {differ} different, "
                           f"{absent} meshes of the dump not drawn in the captured frame")

    theirs_shaders = {h for d in dump.draws.values() for h in d.shaders.values()}
    theirs_tex = {s.hash for d in dump.draws.values() for name, s in d.slots.items() if "-t" in name and s.hash}
    common_sh = theirs_shaders & ours_shaders
    common_tex = theirs_tex & ours_tex
    res.messages.append(f"shaders: {len(common_sh)} of the dump's {len(theirs_shaders)} hashes also seen by the probe")
    res.messages.append(f"textures: {len(common_tex)} of the dump's {len(theirs_tex)} hashes also seen by the probe")
    res.checks += len(common_sh) + len(common_tex)
    return res


def dds_payload(data: bytes) -> tuple[bytes, dict] | None:
    """The pixel data of a DDS file and what its header says, or None when it is not one."""
    if len(data) < 128 or data[:4] != b"DDS ":
        return None
    height, width, _pitch, _depth, mips = struct.unpack_from("<5I", data, 12)
    start = 128
    info = {"width": width, "height": height, "mips": max(mips, 1), "array": 1, "format": None}
    if data[84:88] == b"DX10":
        fmt, _dim, _misc, array, _misc2 = struct.unpack_from("<5I", data, 128)
        info.update(format=fmt, array=array)
        start = 148
    return data[start:], info


def _content_of(draw: dict, name: str) -> int | None:
    """Where the probe keeps what a slot of a call held, named the way XXMI names its files."""
    if name == "ib":
        return (draw.get("ib") or {}).get("content")
    if name.startswith("vb") and name[2:].isdigit():
        return (draw.get("vb", {}).get(int(name[2:])) or {}).get("content")
    for group in ("cb", "tex", "out"):
        entry = draw.get(group, {}).get(name)
        if entry is not None:
            return entry.get("content")
    return None


def compare_contents(cap: Capture, dump: FrameDump, read, verbose: bool = False) -> ParityResult:
    """Every .buf and .dds XXMI wrote for a call against the bytes the probe kept of that call.

    `read(content id)` returns (meta, bytes) of one entry of the capture's contents: a whole
    buffer, or one subresource of a texture. XXMI writes a buffer whole and of a texture its
    first subresource, whatever part a view shows; where the probe kept another subresource
    (the one the view shows) the two are not about the same pixels and are only counted.
    """
    res = ParityResult()
    kept: dict[int, tuple[dict, bytes]] = {}
    game = [d for d in cap.draws() if d["origin"] != "xxmi"]
    numbers = [n for n in dump.draw_numbers() if dump.draws[n].call]
    if len(game) != len(numbers):
        res.mismatches += 1
        res.messages.append(f"  MISMATCH call count: probe {len(game)}, xxmi {len(numbers)}")
    files = other_part = nothing_kept = 0
    by_kind: dict[str, int] = {}
    for ours, n in zip(game, numbers):
        for name, slot in sorted(dump.draws[n].slots.items()):
            for file in sorted(slot.files):
                ext = file.rsplit(".", 1)[-1]
                if ext not in ("buf", "dds"):
                    continue
                files += 1
                what = f"call {n:06d} {name}.{ext}"
                content = _content_of(ours, name)
                if content is None:
                    nothing_kept += 1
                    res.messages.append(f"  not kept by the probe: {what}")
                    continue
                theirs = (dump.path / file).read_bytes()
                entry = cap.content(content)
                if content not in kept:
                    kept[content] = read(content)
                meta, mine = kept[content]
                if ext == "dds":
                    parsed = dds_payload(theirs)
                    if parsed is None or entry["kind"] != "texture":
                        res.checks += 1
                        res.mismatches += 1
                        res.messages.append(f"  MISMATCH {what}: not a texture on both sides")
                        continue
                    if entry["mip"] or entry["slice"]:
                        other_part += 1          # XXMI: first subresource; the probe: what the view shows
                        continue
                    theirs = parsed[0]
                res.checks += 1
                by_kind[ext] = by_kind.get(ext, 0) + 1
                if mine == theirs and meta.get("exact") is True:
                    if verbose:
                        res.messages.append(f"  ok {what}: {len(mine)} bytes")
                    continue
                res.mismatches += 1
                first = next((i for i in range(min(len(mine), len(theirs))) if mine[i] != theirs[i]), None)
                res.messages.append(f"  MISMATCH {what}: probe {len(mine)} bytes, xxmi {len(theirs)}, first difference at "
                                    f"{first}, exact={meta.get('exact')}")
    res.messages.insert(0, f"{files} buffer and texture files of {len(numbers)} calls: {res.checks} compared byte for byte "
                           f"({by_kind.get('buf', 0)} buffers, {by_kind.get('dds', 0)} textures), {other_part} show another "
                           f"subresource than XXMI wrote, {nothing_kept} had nothing kept; {len(kept)} distinct contents read")
    return res

