"""Persistent material fingerprints, separate from probe-owned draw records.

Original XXMI log values and fresh captured-pixel hashes retain separate provenance.
Matching is scoped to a cache version, mesh, shader pass and material slot. Conflicting
values remain alternatives; a fingerprint never participates in material-role inference.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile

NAME = "texture-dhashes.json"
SOURCES = {"xxmi_log", "captured_pixels", "exported_pixels"}


def load(folder: Path, version: str) -> list[dict]:
    path = Path(folder) / NAME
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != 1 or doc.get("version") != version:
        raise ValueError("Fingerprint cache schema/version does not match the slot table")
    for e in doc["entries"]:
        if not re.fullmatch(r"[0-9a-f]{16}", e.get("dhash", "")) or e.get("source") not in SOURCES:
            raise ValueError("Invalid fingerprint cache entry")
    return doc["entries"]


def _key(e):
    return e["ib"], e["index_count"], e["ps"], e["slot"]


def save(folder: Path, version: str, entries: list[dict]) -> dict:
    """Atomic merge under an exclusive writer lock; repeated observations do not grow the file."""
    folder = Path(folder)
    lock = folder / (NAME + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise ValueError(f"Fingerprint cache is busy: {lock}") from exc
    tmp = None
    try:
        os.close(fd)
        old = load(folder, version)
        def identity(e):
            return tuple(e.get(k) for k in ("ib", "index_count", "ps", "slot", "texture", "format",
                                           "view_format", "dhash", "source", "mip"))
        merged = {identity(e): e for e in old}
        for e in entries:
            if not re.fullmatch(r"[0-9a-f]{16}", e["dhash"]) or e["source"] not in SOURCES:
                raise ValueError("Invalid fingerprint observation")
            merged.setdefault(identity(e), e)
        if len(merged) != len(old):
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as f:
                tmp = Path(f.name)
                json.dump({"schema": 1, "version": version, "entries": list(merged.values())}, f, indent=1)
                f.write("\n")
            os.replace(tmp, folder / NAME)
        return {"added": len(merged) - len(old), "total": len(merged), "path": str(folder / NAME)}
    finally:
        if tmp and tmp.exists():
            tmp.unlink()
        lock.unlink()


def targets(table: dict) -> dict:
    from .slot_table import all_rows
    out = {}
    for _, row in all_rows(table):
        for p in row["passes"]:
            slots = {s["slot"]: s for s in p["slots"]}
            for role in p["roles"].values():
                if role:
                    out[(row["ib"], row["index_count"], p["ps"], role["slot"])] = slots[role["slot"]]
    return out


def annotate(table: dict, entries: list[dict]) -> None:
    """Attach observations to every pass and its displayed variants, without changing slots."""
    from .slot_table import all_rows
    by_key, by_texture = {}, {}
    for e in entries:
        by_key.setdefault(_key(e), []).append(e)
        by_texture.setdefault((e["ib"], e["index_count"], e["texture"], e["format"]), []).append(e)
    counts = {"roles": 0, "xxmi_log": 0, "computed": 0, "ambiguous": 0, "missing": 0}
    def roles(row, p, shaders):
        result = copy.deepcopy(p["roles"])
        for role in result.values():
            if not role:
                continue
            evidence = [e for ps in shaders for e in by_key.get((row["ib"], row["index_count"], ps, role["slot"]), [])
                        if e["format"] == role.get("format")]
            # A texture's fingerprint is reusable across passes of this mesh even when its
            # register changes. Transfer identity evidence, never a material-role label.
            for q in row["passes"]:
                if q["ps"] not in shaders:
                    continue
                for s in q["slots"]:
                    if s["slot"] == role["slot"]:
                        for e in by_texture.get((row["ib"], row["index_count"], s.get("texture"), role.get("format")), []):
                            if e not in evidence:
                                evidence.append(e)
            originals = [e for e in evidence if e["source"] == "xxmi_log"]
            preferred = originals or evidence
            values = {e["dhash"] for e in preferred}
            role["dhashes"] = evidence
            role["dhash"] = next(iter(values)) if len(values) == 1 else None
            role["dhash_status"] = ("missing" if not values else "ambiguous" if len(values) > 1
                                    else "xxmi_log" if originals else "computed")
            role["xxmi_cached"] = bool(originals) and len(values) == 1
        return result
    for _, row in all_rows(table):
        for p in row["passes"]:
            p["roles"] = roles(row, p, [p["ps"]])
        if row["main"]:
            for p in [row["main"], *row["main"]["other_passes"]]:
                p["roles"] = roles(row, p, [p["ps"], *p["also_ps"]])
            for r in row["main"]["roles"].values():
                if r:
                    counts["roles"] += 1
                    counts[r["dhash_status"]] += 1
    table["dhash_coverage"] = counts


def collect_dump(table: dict, folder: Path) -> tuple[list[dict], dict]:
    from .fa_dump import parse_frame_dump
    folder = Path(folder).resolve()
    dump = parse_frame_dump(folder)
    if not dump.has_log:
        raise ValueError("A frame dump with log.txt is required for original dHashes")
    with (folder / "log.txt").open(encoding="utf-8", errors="replace") as f:
        header = "".join(f.readline() for _ in range(12))
    computed = (folder / "texture-fingerprints.json").exists() or "recomputed" in header.lower()
    source = "exported_pixels" if computed else "xxmi_log"
    wanted = targets(table)
    entries = []
    report = {"source": source, "bindings_with_dhash": 0, "matched": 0, "unmatched": 0}
    for d in dump.draws.values():
        ib = d.slots.get("ib")
        count = d.args.get("IndexCount", d.args.get("IndexCountPerInstance"))
        for name, s in d.slots.items():
            if not name.startswith("ps-t") or s.dhash is None:
                continue
            report["bindings_with_dhash"] += 1
            key = (ib.hash if ib else None, count, d.shaders.get("ps"), int(name[4:]))
            target = wanted.get(key)
            # Dumps have no trustworthy origin flag. Require the exact native texture binding,
            # in addition to IB/count/PS/slot, rather than importing a mod's replacement.
            if not target or s.hash != target.get("texture") or s.marks or not re.fullmatch(r"[0-9a-f]{16}", s.dhash):
                report["unmatched"] += 1
                continue
            entries.append(dict(zip(("ib", "index_count", "ps", "slot"), key),
                                texture=s.hash, format=target["format"], dhash=s.dhash, source=source,
                                mip=0 if computed else None, evidence={"folder": str(folder), "draw": d.number}))
            report["matched"] += 1
    return entries, report


def collect_capture(table: dict, cap, read_content, mip: int = -1) -> tuple[list[dict], dict]:
    """Hash each retained content/view once. -1 prefers an exact 256x256 mip, else mip 0."""
    from . import dhash
    if mip < -1:
        raise ValueError("mip must be -1 (prefer 256x256) or a nonnegative level")
    wanted = targets(table)
    entries, memo = [], {}
    report = {"source": "captured_pixels", "matched": 0, "computed_contents": 0, "errors": []}
    for d in cap.draws():
        if d["origin"] != "game":
            continue
        ib = d.get("ib", {}).get("hash")
        count = d["args"].get("index_count")
        ps = d["shaders"].get("ps")
        for name, binding in d.get("tex", {}).items():
            if not name.startswith("ps-t"):
                continue
            key = (ib, count, ps, int(name[4:]))
            target = wanted.get(key)
            if not target:
                continue
            resource = cap.resource(binding["res"])
            if not resource or resource.get("origin") != "game" or resource.get("kind") != "tex2d" or resource.get("array", 1) != 1:
                continue
            fmt = resource.get("fmt_name")
            if fmt != target["format"]:
                continue
            chosen = mip
            if chosen == -1:
                chosen = 0
                for level in range(resource.get("mips", 1)):
                    if (max(1, resource["w"] >> level), max(1, resource["h"] >> level)) == (256, 256):
                        chosen = level
                        break
            ids = binding.get("mip_contents", [])
            kept = cap.content(ids[chosen] if chosen < len(ids) else binding.get("content"))
            view_format = (binding.get("view") or {}).get("format", fmt)
            if not kept or kept.get("mip") != chosen:
                report["errors"].append({"draw": d["index"], "slot": name, "error": "requested mip was not retained"})
                continue
            cache_key = (kept["id"], view_format)
            try:
                if cache_key not in memo:
                    meta, data = read_content(kept["id"])
                    if not meta.get("exact"):
                        raise ValueError("captured pixels are not exact")
                    memo[cache_key] = {"dhash": dhash.compute(meta, data, view_format),
                                       "width": meta["width"], "height": meta["height"]}
                    report["computed_contents"] += 1
                entries.append(dict(zip(("ib", "index_count", "ps", "slot"), key), **memo[cache_key],
                                    texture=binding.get("hash"), format=fmt, view_format=view_format,
                                    source="captured_pixels", mip=chosen,
                                    evidence={"frame": cap.frame, "draw": d["index"]}))
                report["matched"] += 1
            except (ValueError, OSError) as exc:
                report["errors"].append({"draw": d["index"], "slot": name, "error": str(exc)})
    return entries, report


def register(server, session, table_cache):
    @server.tool("efmi_cache_texture_dhashes",
                 "Persist material dHashes for an existing slot-cache version. frame_dump imports original XXMI "
                 "log values with exact native binding matches; otherwise reads a retained native capture with "
                 "contents=inputs (probe 0.1.16). Fresh fingerprints stay marked computed. No game input or mod edits. "
                 "Queries and CLI exports of the slot table then include per-role hashes and coverage.",
                 {"folder": {"type": "string"}, "frame_dump": {"type": "string", "default": ""},
                  "capture": {"type": ["string", "integer"], "default": "latest"},
                  "mip": {"type": "integer", "minimum": -1, "default": -1}}, ["folder"], read_only=False)
    def cache_dhashes(folder, frame_dump="", capture="latest", mip=-1):
        folder = Path(folder).resolve()
        table = table_cache.get(folder, session.efmi_root)
        if frame_dump:
            entries, report = collect_dump(table, Path(frame_dump))
        else:
            from .mcp_stdio import ToolError
            status = session.call(lambda c: c.status())
            current = status.get("slot_cache", {}).get("version")
            if current != table["version"]:
                raise ToolError("The running game's slot-cache version must match the requested folder")
            cap = session.capture(capture)
            entries, report = collect_capture(table, cap,
                                              lambda cid: session.call(lambda c: c.read_content(cap.frame, cid)), mip)
        report.update(save(folder, table["version"], entries))
        report["version"] = table["version"]
        return json.dumps(report)
