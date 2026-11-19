"""Multiple simultaneous probe connections and capture workspace ownership."""
import concurrent.futures
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from efmi_mcp import tools
from efmi_mcp.probe_client import ProbeError
from selftest import Sandbox, connect

def denied(fn):
    try:
        fn()
    except ProbeError as exc:
        assert "session" in str(exc), str(exc)
    else:
        raise AssertionError("Another session modified the owner's workspace")

def run():
    sb = Sandbox(["--seconds", "260"], xxmi=False)
    clients = []
    try:
        assert sb.ready.wait(30)
        a = connect(sb.pid); clients.append(a)
        b = connect(sb.pid); clients.append(b)
        c = connect(sb.pid); clients.append(c)
        c.status()  # Wait for the final worker to register its connection.
        assert a.status()["session"]["connections"] == 3
        assert len({x.status()["session"]["id"] for x in clients}) == 3
        doc, blob = a.capture(contents={"inputs": ["ib"], "events": True})
        frame = doc["frame"]
        snap = next(d["cib"] for d in doc["draws"] if d.get("cib", -1) >= 0)
        before = a.read_content(frame, snap)
        denied(lambda: b.capture())
        denied(lambda: b.live(enable=True))
        denied(lambda: b.request("mods_key", focus=False))
        denied(lambda: b.request("record", action="start"))
        denied(lambda: b.read_content(frame, snap))
        assert b.status()["session"]["owner"] == a.status()["session"]["id"]
        c.close(); clients.remove(c)
        assert a.read_content(frame, snap)[1] == before[1]
        root = Path(__file__).resolve().parents[1]
        session = tools.Session(root/"sandbox", root/"captures", Path("probe.dll"))
        session.client = a
        with patch.object(tools, "Session", return_value=session):
            server = tools.build_server(session.efmi_root, session.out_dir, session.probe_dll)
        release = server._handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                                 "params":{"name":"efmi_session","arguments":{"action":"release"}}})
        assert not release["result"].get("isError"), release
        newer, _ = b.capture()
        assert newer["frame"] > frame
        b.live(enable=True)
        time.sleep(.15)
        assert a.live_get()[1]  # The stream can be inspected by other sessions.
        b.trigger_arm(shader=["0000000000000000"], expire_ms=60000)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(b.trigger_wait, 1500)
            time.sleep(.15)
            d = connect(sb.pid); clients.append(d)
            assert d.status()["attached"]
            denied(lambda: a.capture())
            pending.result(timeout=10)
        b.close(); clients.remove(b)
        # The worker releases its workspace on disconnect.
        for _ in range(100):
            if not a.status()["session"]["owner"]:
                break
            time.sleep(.03)
        assert not a.status()["session"]["owner"]
        assert not a.live_get()[1]
        assert a.trigger_wait(0)[0]["state"] == "off"
        a.request("session", action="release")
        d.capture()
        print("PASS simultaneous connections, guarded mutations, kept contents, MCP release, trigger wait and disconnect", flush=True)
        # Real-time expiry, without a shortened test-only lease.
        for _ in range(6):
            time.sleep(30)
            assert sb.proc.poll() is None, f"Sandbox exited during idle wait: {sb.proc.returncode}"
        time.sleep(1)
        last, _ = a.capture()
        assert last["frame"] > newer["frame"]
        denied(lambda: d.capture())
        print("PASS idle lease expiry and ownership transfer", flush=True)
    finally:
        for c in clients:
            c.close()
        sb.quit()

if __name__ == "__main__":
    run()
