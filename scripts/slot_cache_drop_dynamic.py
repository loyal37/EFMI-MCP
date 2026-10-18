"""Removes records of index data the CPU rewrote from a slot cache folder written by probe 0.1.8.

Probe 0.1.8 recorded the game's dynamic batches: draws from a D3D11_USAGE_DYNAMIC index buffer
whose bytes change every frame, each giving a new region hash. 0.1.9 leaves them out, and marks
every record with `ib_usage`. Older records carry no usage, so this tool tells them apart by the
index buffer resource: a buffer hash that occurs only with records of dynamic batches can be named
here, and records of 0.1.9 say "dynamic" themselves. The dropped lines go to
`draws-dropped-dynamic.jsonl` next to the file, nothing is deleted.

    python scripts/slot_cache_drop_dynamic.py <version folder> [--resource <hash> ...] [--apply]

Without --apply only the counts are shown. The game must not be running: the probe keeps
draws.jsonl open while it runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--resource", action="append", default=[],
                    help="index buffer resource hash (8 hex digits) whose records are dynamic batches")
    ap.add_argument("--apply", action="store_true", help="rewrite draws.jsonl; otherwise only count")
    args = ap.parse_args()
    folder = Path(args.folder)
    path = folder / "draws.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    resources = {r.lower() for r in args.resource}
    keep, drop = [], []
    why = Counter()
    for line in lines:
        rec = None
        if line.startswith("{"):
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
        if rec is None:
            keep.append(line)
            continue
        if rec.get("ib_usage") == "dynamic":
            drop.append(line)
            why["ib_usage dynamic"] += 1
        elif rec.get("origin") == "game" and rec.get("ib_resource", "").lower() in resources:
            drop.append(line)
            why[f"resource {rec['ib_resource']}"] += 1
        else:
            keep.append(line)
    print(f"{len(lines)} lines: keep {len(keep)}, drop {len(drop)} {dict(why)}")
    if not args.apply:
        print("dry run; add --apply to rewrite the file")
        return 0
    if not drop:
        return 0
    dropped = folder / "draws-dropped-dynamic.jsonl"
    with open(dropped, "a", encoding="utf-8", newline="") as f:
        f.writelines(drop)
    try:
        with open(path, "r+", encoding="utf-8", newline="") as f:
            f.seek(0)
            f.writelines(keep)
            f.truncate()
    except PermissionError:
        print("draws.jsonl is in use (the game is running); nothing changed", file=sys.stderr)
        return 1
    print(f"rewrote {path} ({len(keep)} lines); dropped lines appended to {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
