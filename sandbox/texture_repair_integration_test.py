"""Verify direct Filters and expose XXMI 1.3.3's repeated-match limitation.

The fixture already owns snapshot/restore, like EFMI core callbacks. Conversion
must preserve that lifecycle without adding helpers. First matches work; a second
equal-distance match in the same original draw is deliberately checked as a known
runtime limitation, not reported as successful per-draw texture switching.
"""
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
from efmi_mcp.capture_model import Capture
from efmi_mcp import dhash
import efmi_texture_repair as repair
import selftest


def dds(pixel):
    header = [124, 0x100F, 1, 1, 4, 0, 0] + [0] * 11
    header += [32, 0x41, 0, 32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000]
    header += [0x1000, 0, 0, 0, 0]
    return b"DDS " + struct.pack("<31I", *header) + bytes(pixel)


def run():
    with tempfile.TemporaryDirectory(dir=ROOT / "captures", prefix="texture-repair-runtime-") as tmp:
        folder = Path(tmp)
        for name in ("d3d11.dll", "d3dcompiler_47.dll"):
            shutil.copy2(selftest.RUN / "EFMI" / name, folder / name)
        cfg = (selftest.RUN / "EFMI/d3dx.ini").read_text(encoding="utf-8-sig")
        cfg = re.sub(r"(?im)^\s*include\s*=.*$", "; Isolated test: no installed core includes", cfg)
        cfg = re.sub(r"(?im)^hunting\s*=.*$", "hunting = 0", cfg)
        (folder / "d3dx.ini").write_text(cfg, encoding="utf-8")
        shutil.copy2(selftest.PROBE, folder / "efmi_probe.dll")
        (folder / "efmi_probe.ini").write_text("[SlotCache]\nenable=0\ndhash=0\n")
        old_probe = selftest.PROBE
        selftest.PROBE = folder / "efmi_probe.dll"
        baseline = None
        client = sb = None
        try:
            sb = selftest.Sandbox(["--slot-textures", "--warp"], xxmi=True, xxmi_dll=folder / "d3d11.dll")
            assert sb.ready.wait(30), "Baseline sandbox did not become ready"
            client = selftest.connect(sb.pid)
            raw, blob = client.capture(contents={"inputs": ["tex"]})
            cap = Capture(raw, blob)
            draws = [d for d in cap.draws() if d.get("ib") and d.get("tex", {}).get("ps-t1")]
            assert len(draws) >= 3
            first, second, third = draws[:3]
            slot = first["tex"]["ps-t1"]
            cid = slot["mip_contents"][1]
            meta, data = client.read_content(cap.frame, cid)
            fmt = cap.resource(slot["res"])["fmt_name"]
            fingerprint = dhash.compute(meta, data, fmt)
            baseline = slot["hash"]
            keys = [(d["ib"]["hash"], d["args"]["index_count"]) for d in (first, second)]
            client.close(); client = None
            sb.quit(); sb = None

            mod = folder / "Mods/Fixture"
            mod.mkdir(parents=True)
            for name, color in [("Red", (255, 0, 0, 255)), ("Blue", (0, 0, 255, 255)), ("Green", (0, 255, 0, 255))]:
                (mod / (name + ".dds")).write_bytes(dds(color))
            source = f"""[ShaderOverrideFixture]
hash = {first['shaders']['ps']}
CheckTextureOverride = ib
[TextureOverride_Component0]
hash = {keys[0][0]}
match_index_count = {keys[0][1]}
handling = skip
run = SnapshotInitialTextures
ps-t2 = ref ResourceGreen
Resource\\EFMITTL\\Diffuse = ref ResourceRed
run = CommandList\\EFMITTL\\SetTextures
drawindexed = {keys[0][1]},0,0
ps-t2 = null
Resource\\EFMITTL\\Diffuse = ref ResourceBlue
run = CommandList\\EFMITTL\\SetTextures
drawindexed = {keys[0][1]},0,0
post run = RestoreInitialTextures
[TextureOverride_Component1]
hash = {keys[1][0]}
match_index_count = {keys[1][1]}
run = SnapshotInitialTextures
ps-t1 = ResourceGreen
post run = RestoreInitialTextures
[ResourceRed]
filename = Red.dds
[ResourceBlue]
filename = Blue.dds
[ResourceGreen]
filename = Green.dds
"""
            catalog = dict(game_version="sandbox", meshes={f"{h}:{n}": dict(diffuse=dict(slot=1, format=fmt, dhash=fingerprint)) for h, n in keys})
            path = mod / "mod.ini"
            path.write_text(source)
            original = path.read_bytes()
            changes, report = repair.plan([repair.Document(path, original)], catalog)
            assert len(changes) == 1, report
            assert any(entry.get("warnings") for entry in report), "Repeated Filter calls must be disclosed"
            assert b"EFMIRepair_" not in changes[path][1], "No generated runtime helpers"
            repair.apply_changes(changes, "sandbox")
            sb = selftest.Sandbox(["--slot-textures", "--warp"], xxmi=True, xxmi_dll=folder / "d3d11.dll")
            assert sb.ready.wait(30), "Converted sandbox did not become ready"
            client = selftest.connect(sb.pid)
            raw, blob = client.capture(contents={"inputs": ["tex"]})
            cap = Capture(raw, blob)
            selected = [d for d in cap.draws() if d.get("tex", {}).get("ps-t1")]
            actual = []
            for d in selected:
                binding = d["tex"]["ps-t1"]
                resource = cap.resource(binding["res"])
                if resource.get("w") == 1 and resource.get("h") == 1:
                    ids = binding.get("mip_contents") or [binding["content"]]
                    info, pixels = client.read_content(cap.frame, ids[0])
                    actual.append((d["origin"], list(pixels[:4])))
            assert actual[:3] == [("xxmi", [255, 0, 0, 255]), ("xxmi", [255, 0, 0, 255]), ("game", [0, 255, 0, 255])], actual
            custom = [d for d in selected if d["origin"] == "xxmi"]
            aux = cap.resource(custom[0]["tex"]["ps-t2"]["res"])
            assert aux["w"] == aux["h"] == 1
            assert "ps-t2" not in custom[1]["tex"], "Unrelated deliberate null binding must remain"
            later = [d for d in selected if d.get("ib", {}).get("hash") == third["ib"]["hash"]]
            assert later and all(d["tex"]["ps-t1"]["hash"] == baseline for d in later)
            assert sb.proc.poll() is None, "Sandbox pipeline state guard failed"
            print("PASS direct first-match red, texture-only green, auxiliary/null bindings, existing restoration lifecycle")
            print("CONFIRMED LIMIT: XXMI 1.3.3 keeps red on the second draw despite a direct blue Filter assignment; report warns about repeated matching")
            restored, errors = repair.restore(folder)
            assert not errors and path.read_bytes() == original
            print("PASS byte-exact restore after live sandbox conversion")
        finally:
            if client:
                client.close()
            if sb:
                sb.quit()
            selftest.PROBE = old_probe


if __name__ == "__main__":
    run()
