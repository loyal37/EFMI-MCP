"""Does the probe agree with XXMI? Comparing a probe capture with an XXMI frame dump.

Two ways of looking at it:

compare_ordered   draw N of the dump against the N-th draw of the game in the
                  capture. Only meaningful when both show exactly the same
                  frame content (the sandbox's static scene).

compare_sets      frame-independent: identities that appear in both are
                  compared wherever the same draw parameters show up. Works for
                  a dump and a capture taken moments apart in the game.
"""
from __future__ import annotations

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
    theirs_tex = {s.hash for d in dump.draws.values() for name, s in d.slots.items() if "-t" in name}
    common_sh = theirs_shaders & ours_shaders
    common_tex = theirs_tex & ours_tex
    res.messages.append(f"shaders: {len(common_sh)} of the dump's {len(theirs_shaders)} hashes also seen by the probe")
    res.messages.append(f"textures: {len(common_tex)} of the dump's {len(theirs_tex)} hashes also seen by the probe")
    res.checks += len(common_sh) + len(common_tex)
    return res
