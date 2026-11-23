"""Automatic fingerprints with no client, GPU parity, budgets and restart deduplication."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from efmi_mcp import dhash, slot_table, slot_fingerprints
from efmi_mcp.capture_model import Capture
import selftest


def wait_for(fn, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Timed out waiting for background evidence")


def run():
    hardware = "--hardware" in sys.argv
    xxmi = "--xxmi" in sys.argv
    with tempfile.TemporaryDirectory(dir=ROOT / "sandbox/build") as tmp:
        folder = Path(tmp)
        shutil.copy2(selftest.PROBE, folder / "efmi_probe.dll")
        selftest.PROBE = folder / "efmi_probe.dll"
        (folder / "efmi_probe.ini").write_text("[SlotCache]\nenable=1\ndhash=1\nevery=1\nskinned_only=0\nbones_only=0\nmax_hashes=1\n")
        args = ["--slot-textures"] + ([] if hardware else ["--warp"])
        sb = selftest.Sandbox(args, xxmi=xxmi)
        client = None
        try:
            assert sb.ready.wait(30)
            # No client has connected: only browsing/rendering can produce these files.
            cache = wait_for(lambda: next((folder / "slot-cache").glob("*/draws.jsonl"), None))
            def entries():
                return slot_fingerprints.automatic_entries(slot_table.load_records(cache.parent))
            wait_for(lambda: len({e["texture"] for e in entries()}) == 2)
            print("PASS no client/no capture: both declared BC textures fingerprinted automatically")
            client = selftest.connect(sb.pid)
            status = lambda: client.request("status")[0]["slot_cache"]
            wait_for(lambda: status()["dhash_status"]["pending"] == 0)
            a = status()
            assert a["dhash"] and a["dhash_status"]["completed"] == 2, a
            assert a["dhash_status"]["deferred"] > 0, a
            time.sleep(.5)
            b = status()
            assert b["dhash_status"]["completed"] == 2, b
            print("PASS max_hashes=1 defers work without losing it; stable textures are not rehashed")
            assert {e["mip"] for e in entries() if e["slot"] == 1} == {1}
            print("PASS preferred 256x256 mip selected from a full 512x512 chain")
            # Change the material without a client; then rewrite a mip of the same resource.
            client.close(); client = None
            selftest.signal(f"efmi_sandbox_swap_{sb.pid}")
            wait_for(lambda: len({e["texture"] for e in entries()}) == 3)
            old_values = {e["dhash"] for e in entries()}
            selftest.signal(f"efmi_sandbox_upload_{sb.pid}")
            wait_for(lambda: len({e["dhash"] for e in entries()} - old_values) == 1)
            client = selftest.connect(sb.pid)
            assert status()["dhash_status"]["completed"] == 4, status()
            print("PASS material switch and same-resource mip upload both refresh automatically")
            client.request("slot_cache", enable=False)
            time.sleep(.3)
            client.request("slot_cache", flush=True)
            doc, blob = client.capture(contents={"inputs":["tex"]})
            cap = Capture(doc, blob)
            by_texture = {e["texture"]: e for e in entries()}
            verified = set()
            for d in cap.draws():
                for name, e in d.get("tex", {}).items():
                    ref = by_texture.get(e.get("hash"))
                    if not name.startswith("ps-t") or not ref or e["hash"] in verified:
                        continue
                    ids = e.get("mip_contents", [])
                    cid = ids[ref["mip"]] if ids else e.get("content")
                    meta, data = client.read_content(cap.frame, cid)
                    expected = dhash.compute(meta, data, ref["view_format"])
                    distance = (int(expected, 16) ^ int(ref["dhash"], 16)).bit_count()
                    if hardware:
                        # Cross-device float comparisons are observational, not an equality contract.
                        print(f"Hardware/WARP distance: {ref['view_format']} mip {ref['mip']}: {distance} bits")
                    else:
                        assert expected == ref["dhash"], (ref, expected, distance)
                    verified.add(e["hash"])
            assert len(verified) == 2
            if not hardware:
                print("PASS async WARP fingerprints exactly equal independent captured-pixel computation")
            assert sb.proc.poll() is None, "Sandbox pipeline state guard failed"
            print("PASS shader, constant buffer, SRV, sampler and UAV state preserved across fingerprint dispatches")
            table = slot_table.build(cache.parent)
            assert table["dhash_coverage"]["computed"] > 0, table["dhash_coverage"]
            assert all(e["slot"] in (1, 2) for e in entries())
            print("PASS undeclared t90 excluded; offline table includes probe_gpu fingerprints")
            before = cache.read_bytes()
        finally:
            if client: client.close()
            sb.quit()
        sb = selftest.Sandbox(args, xxmi=xxmi)
        try:
            assert sb.ready.wait(30)
            time.sleep(1.5)
            assert cache.read_bytes() == before
            print("PASS process restart deduplicates records and fingerprints")
        finally:
            sb.quit()


if __name__ == "__main__":
    run()
