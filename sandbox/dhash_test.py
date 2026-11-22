"""dHash sampling, modern log import and versioned mip-chain export regression."""
import json
import struct
import sys
import tempfile
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"server"))
from efmi_mcp import dhash, contents, tools
from efmi_mcp.capture_model import Capture
from efmi_mcp.fa_dump import parse_frame_dump
from selftest import Sandbox, connect

def run():
    meta = dict(width=9,height=8,format=28)
    assert dhash.compute(meta, bytes([0,0,0,255])*72) == "000000000000ffff"
    assert dhash.compute(meta, bytes([255,0,0,255])*72) == "ffff00000000ffff"
    assert dhash.compute(dict(width=1,height=1,format=71), struct.pack("<HHI",65535,65535,0)) == "ffffffffffffffff"
    stripes = b"".join(bytes([255 if x%2==0 else 0]*3+[255]) for y in range(8) for x in range(9))
    assert dhash.compute(meta, stripes) == "5555555555555555"
    print("PASS flat-color fallback, channel order, directional comparisons and bit order")
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(dir=root/"captures") as folder:
        p = Path(folder)
        (p/"log.txt").write_text("000001 PSSetShaderResources(StartSlot:0, NumViews:1)\n       0: view=0x1 resource=0x2 hash=12345678 orig_hash=deadbeef dhash=0000000000000000\n000001 Draw(VertexCount:3)\n000002 PSSetShaderResources(StartSlot:0, NumViews:1)\n000002 Draw(VertexCount:3)\n000003 ClearState()\n000003 Draw(VertexCount:3)\n")
        parsed = parse_frame_dump(p)
        assert parsed.draws[1].slots["ps-t0"].dhash == "0000000000000000"
        assert parsed.draws[1].slots["ps-t0"].hash == "12345678"
        assert not parsed.draws[2].slots
        assert not parsed.draws[3].slots
    print("PASS zero dHash retained and ClearState ends binding lifetime")
    sb = Sandbox(["--content-pass", "--state-pass"], xxmi=False)
    client = None
    try:
        assert sb.ready.wait(30)
        client = connect(sb.pid)
        doc, blob = client.capture(contents={"inputs":["tex"], "events":True})
        cap = Capture(doc, blob)
        selected = [(d, name, e) for d in cap.draws() for name,e in d.get("tex",{}).items()
                    if len(e.get("mip_contents", []))==4]
        assert selected
        chains=set()
        for d,name,e in selected:
            ids=e["mip_contents"];chains.add(tuple(ids))
            for mip,cid in enumerate(ids):
                kept=cap.content(cid)
                assert kept["mip"]==mip
                meta,data=client.read_content(cap.frame,cid)
                assert meta["exact"] and (meta["width"],meta["height"])==(16>>mip,16>>mip)
        assert len(chains)>1, "GenerateMips versions must remain distinct"
        d,name,e=selected[0]
        with tempfile.TemporaryDirectory(dir=root/"captures") as folder:
            session=tools.Session(root/"sandbox",Path(folder),root/"dist/efmi_probe.dll")
            session.client=client
            out=contents.dump_frame(session,cap,first=d["index"],last=d["index"],kinds=["tex"],folder=folder,log="xxmi")
            assert out["calls_written"]==[d["index"]]*2
            path=next(Path(folder).glob(f"*-{name}=*.dds"))
            data=path.read_bytes()
            assert struct.unpack_from("<I",data,28)[0]==4
            expected=b"".join(client.read_content(cap.frame,cid)[1] for cid in e["mip_contents"])
            assert data[148:]==expected
            from types import SimpleNamespace
            from unittest.mock import patch
            filtered = Path(folder)/"filtered"
            with patch.object(session, "shader_listing", return_value=SimpleNamespace(usage={"resources":{}})):
                contents.dump_frame(session,cap,first=d["index"],last=d["index"],kinds=["tex"],
                                    folder=str(filtered),declared_only=True)
            assert not list(filtered.glob("*-ps-t*.dds"))
            parsed=parse_frame_dump(folder)
            assert parsed.draws[d["index"]].slots[name].dhash is not None
        print("PASS all four mip payloads, distinct draw versions, DDS byte parity and dHash log roundtrip")
    finally:
        if client: client.close()
        sb.quit()
if __name__=="__main__": run()
