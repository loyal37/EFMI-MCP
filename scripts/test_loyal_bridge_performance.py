"""Offline A/B export benchmark and equivalence checks using existing archives.

Nothing connects to a probe or writes outside this project's captures directory.
The baseline is read from Git; LoyalTools is imported read-only by the bridge.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import types
import weakref
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from efmi_mcp import loyal_bridge

BASELINE_REF = "8faa4c21828a2313f2c89f7a802cee2850a3e4de"


def load_baseline(ref):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:server/efmi_mcp/loyal_bridge.py"], cwd=ROOT)
    module = types.ModuleType("efmi_mcp._loyal_bridge_baseline")
    module.__file__ = loyal_bridge.__file__
    module.__package__ = "efmi_mcp"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, hashlib.sha256(source).hexdigest()


class SourceReads:
    def __init__(self, archives):
        self.prefixes = tuple(os.path.normcase(str(path)) + os.sep for path in archives)
        self.active = False
        self.counts = Counter()
        self.files = Counter()
        sys.addaudithook(self.audit)

    def audit(self, event, args):
        if not self.active or event != "open" or not isinstance(args[0], str):
            return
        path, mode, flags = args
        if not os.path.normcase(path).startswith(self.prefixes):
            return
        if (mode and any(char in mode for char in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR):
            return
        self.counts[Path(path).suffix] += 1
        self.files[path] += 1

    def measure(self, module, archives, output):
        self.counts.clear()
        self.files.clear()
        self.active = True
        started = time.perf_counter()
        try:
            report = module.export_merged_workspace(
                archives[0], output, validation_archives=archives[1:])
        finally:
            elapsed = time.perf_counter() - started
            self.active = False
        assert report["archive_verified"] is True
        assert report["validation"]["pose_reconstruction"] is False
        return {"seconds": elapsed, "source_read_opens": dict(self.counts),
                "buffer_files": len([p for p in self.files if p.endswith(".buf")]),
                "output": str(output)}


def compare_outputs(reference, candidate):
    def names(root):
        return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    files = names(reference)
    assert files == names(candidate), "Exported file names differ"
    for name in sorted(files):
        before, after = (reference / name).read_bytes(), (candidate / name).read_bytes()
        if name.name == "live-bridge-report.json":
            before, after = json.loads(before), json.loads(after)
            before.pop("workspace")
            after.pop("workspace")
        elif name.name == "live-bridge.log":
            # The upstream log embeds wall-clock timing; every other byte,
            # including native conflict diagnostics, remains significant.
            before = re.sub(rb"in [0-9.]+s\.", b"in <elapsed>s.", before)
            after = re.sub(rb"in [0-9.]+s\.", b"in <elapsed>s.", after)
        assert before == after, f"Exported content differs: {name}"
    return len(files)


def rejection_checks(archives, output):
    passed = []

    def reject(name, fragment, **kwargs):
        destination = output / name
        try:
            loyal_bridge.export_merged_workspace(archives[0], destination, **kwargs)
        except loyal_bridge.BridgeError as error:
            assert fragment in str(error), (name, str(error))
        else:
            raise AssertionError(f"Invalid archive accepted: {name}")
        assert not destination.exists(), f"Rejected export wrote a workspace: {name}"
        passed.append(name)

    reject("deferred-single", "requires three-sample")
    reject("two-samples", "at least three distinct", validation_archives=archives[1:2])
    reject("duplicate-samples", "at least three distinct", validation_archives=[archives[1]] * 2)

    original_read = loyal_bridge._read_archive

    def invalid_archive(path):
        capture, readback, draws = original_read(path)
        readback["errors"] = ["intentional performance regression test"]
        return capture, readback, draws

    with patch.object(loyal_bridge, "_read_archive", invalid_archive):
        reject("collection-errors", "incomplete or has errors", validation_archives=archives[1:])

    def changed_geometry(path):
        capture, readback, draws = original_read(path)
        for record in readback["buffers"].values():
            if not record.get("skeleton"):
                record["meta"]["unchanged_since_capture"] = False
        return capture, readback, draws

    with patch.object(loyal_bridge, "_read_archive", changed_geometry):
        reject("changed-geometry", "not verified unchanged", validation_archives=archives[1:])

    read_bytes = Path.read_bytes

    def corrupt_read(path):
        data = read_bytes(path)
        if path.suffix == ".buf" and path.parent.parent == archives[0]:
            return bytes([data[0] ^ 1]) + data[1:]
        return data

    # Corrupt only the returned bytes, never the archive on disk. A complete
    # resource of the right length must still fail its recorded SHA256 gate.
    with patch.object(Path, "read_bytes", corrupt_read):
        reject("buffer-sha256", "recorded SHA256", validation_archives=archives[1:])
    return passed


def cache_lifetime_checks(archives, output):
    original_build = loyal_bridge._build_extractor
    caches, references, sizes, semantic_cases = [], [], [], []

    def track_build(*args, **kwargs):
        extractor, missing = original_build(*args, **kwargs)
        caches.append(extractor._bridge_buffer_bytes)
        references.append(weakref.ref(extractor))
        sizes.append(sum(len(data) for cache in caches for data in cache.values()))
        if len(caches) == 1:
            import numpy as np
            reader = extractor._read_original_index_range.__func__.__globals__["numpy"]
            path, data = next(iter(caches[0].items()))
            for count, offset in ((-1, len(data) - 7), (3, len(data) - 7),
                                  (-1, len(data) + 5), (2, len(data) + 5),
                                  (0, 3), (4, 3)):
                try:
                    expected = np.fromfile(path, dtype="<u4", count=count, offset=offset)
                except ValueError as error:
                    try:
                        reader.fromfile(path, dtype="<u4", count=count, offset=offset)
                    except ValueError as actual_error:
                        assert str(actual_error) == str(error)
                    else:
                        raise AssertionError("Cached reader accepted an invalid NumPy range")
                    semantic_cases.append({"count": count, "offset": offset, "error": str(error)})
                    continue
                actual = reader.fromfile(path, dtype="<u4", count=count, offset=offset)
                assert np.array_equal(expected, actual), (count, offset)
                assert actual.flags.writeable
                semantic_cases.append({"count": count, "offset": offset, "elements": actual.size})
        return extractor, missing

    with patch.object(loyal_bridge, "_build_extractor", track_build):
        loyal_bridge.export_merged_workspace(
            archives[0], output / "cache-lifetime", copy_textures=False,
            validation_archives=archives[1:])
    assert len(caches) == len(archives)
    assert all(not cache for cache in caches), "Whole-resource byte cache survived export"
    gc.collect()
    assert all(reference() is None for reference in references), "Native extractor retained after export"
    return {"whole_buffer_cache_peak_bytes": max(sizes),
            "all_caches_empty_at_return": True, "all_extractors_collected": True,
            "fromfile_semantic_cases": semantic_cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--archive", action="append", default=[])
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    archives = [Path(path).resolve() for path in args.archive] if args.archive else [
        ROOT / "captures" / f"liimo-static-{letter}" for letter in "abc"]
    if len(archives) < 3:
        parser.error("Supply at least three archives")
    baseline, baseline_sha = load_baseline(args.baseline_ref)
    output = ROOT / "captures" / ("bridge-performance-" + uuid4().hex[:10])
    output.mkdir()
    source_reads = SourceReads(archives)
    modules = {"before": baseline, "after": loyal_bridge}
    measured = {key: [] for key in modules}
    warmup = {key: source_reads.measure(module, archives, output / f"warmup-{key}")
              for key, module in modules.items()}
    compared_files = compare_outputs(output / "warmup-before", output / "warmup-after")
    for round_index in range(args.rounds):
        order = ("before", "after") if round_index % 2 == 0 else ("after", "before")
        for key in order:
            measured[key].append(source_reads.measure(
                modules[key], archives, output / f"{key}-{round_index}"))
        compared_files += compare_outputs(output / f"before-{round_index}", output / f"after-{round_index}")
    for before, after in zip(measured["before"], measured["after"]):
        assert after["source_read_opens"][".buf"] == after["buffer_files"], after
        assert after["source_read_opens"][".buf"] < before["source_read_opens"][".buf"], (before, after)
    before_median, after_median = [statistics.median(
        item["seconds"] for item in measured[key]) for key in ("before", "after")]
    result = {
        "baseline_ref": args.baseline_ref, "baseline_sha256": baseline_sha,
        "candidate_sha256": hashlib.sha256(Path(loyal_bridge.__file__).read_bytes()).hexdigest(),
        "archives": [str(path) for path in archives], "warmup": warmup, "runs": measured,
        "before_median_seconds": before_median, "after_median_seconds": after_median,
        "reduction_percent": 100 * (1 - after_median / before_median),
        "compared_files": compared_files,
        "comparison_exclusions": ["report.workspace", "upstream elapsed seconds in live-bridge.log"],
        "rejection_checks": rejection_checks(archives, output / "rejections"),
        "cache_lifetime": cache_lifetime_checks(archives, output),
    }
    (output / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Evidence: {output / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
