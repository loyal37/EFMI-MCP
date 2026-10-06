"""Actual F10 -> XXMI reload -> fresh-capture test in a new private DX11 sandbox.

This sends one real F10 pulse. Refuses to run while any other probe is present.
It never launches/stops the real game or edits its EFMI installation. The private
copy and evidence are retained under ignored sandbox/build/reload-* directories.
This validates the sandbox reload path, not a real game's mod or integrity level.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))

from efmi_mcp import tools
from efmi_mcp.probe_client import ProbeClient, ProbeError, find_probes
from prepare_run import DEFAULT_EFMI, patch_ini


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def game_ib_counts(cap):
    return Counter(d["ib"]["hash"] for d in cap.draws()
                   if d["origin"] != "xxmi" and d.get("ib", {}).get("hash"))


def assert_no_other_probes(own_pid=None):
    other = [p.pid for p in find_probes() if p.pid != own_pid]
    if other:
        raise RuntimeError(f"Other probes are running ({other}); refusing any F10 test.")


def private_copy(source: Path, probe: Path):
    exe = HERE / "build" / "efmi_sandbox.exe"
    needed = [exe, probe, source / "d3d11.dll", source / "d3dcompiler_47.dll", source / "d3dx.ini"]
    for path in needed:
        if not path.is_file():
            raise RuntimeError(f"Missing prerequisite: {path}")
    if not (source / "Core").is_dir():
        raise RuntimeError(f"Missing Core directory: {source}")
    # mkdtemp guarantees a new directory. Never clear/reuse the ordinary run tree.
    run = Path(tempfile.mkdtemp(prefix="reload-", dir=HERE / "build"))
    efmi = run / "EFMI"
    efmi.mkdir()
    shutil.copy2(exe, run / exe.name)
    for name in ("d3d11.dll", "d3dcompiler_47.dll"):
        shutil.copy2(source / name, efmi / name)
    shutil.copytree(source / "Core", efmi / "Core")
    for name in ("Mods", "ShaderFixes", "ShaderCache"):
        (efmi / name).mkdir()
    text = patch_ini((source / "d3dx.ini").read_text(encoding="utf-8-sig"), exe.name)
    # Only this fresh copy gets its ordinary unmodified F10 bindings restored.
    for name in ("reload_config", "reload_fixes"):
        text, count = re.subn(rf"(?im)^\s*;sandbox:\s*{name}\s*=.*$",
                              f"{name} = no_modifiers VK_F10", text)
        if count != 1:
            raise RuntimeError(f"Expected one sandbox-disabled {name}, found {count}")
    text, count = re.subn(r"(?im)^\s*check_foreground_window\s*=.*$",
                         "check_foreground_window = 1", text)
    if count != 1:
        raise RuntimeError("Private copy must have exactly one foreground-window check")
    text, count = re.subn(r"(?im)^\s*hunting\s*=.*$", "hunting = 0", text)
    if count != 1:
        raise RuntimeError("Private copy must have exactly one hunting setting")
    # Retain runtime evidence of key detection / INI parsing as well as draws.
    text, count = re.subn(r"(?im)^\s*log_level\s*=.*$", "log_level = debug", text)
    if count != 1:
        raise RuntimeError("Private copy must have exactly one log level setting")
    (efmi / "d3dx.ini").write_bytes(text.encode("utf-8"))
    return run, efmi


def signal_own_quit(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.SetEvent.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    event = kernel32.OpenEventW(0x0002, 0, f"efmi_sandbox_quit_{pid}")
    if not event:
        return False
    try:
        return bool(kernel32.SetEvent(event))
    finally:
        kernel32.CloseHandle(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--efmi-root", type=Path, default=DEFAULT_EFMI,
                        help="Read-only source for a new private XXMI copy")
    args = parser.parse_args()
    assert_no_other_probes()
    probe = ROOT / "dist" / "efmi_probe.dll"
    run, efmi = private_copy(args.efmi_root.resolve(), probe)
    print(f"Private real-DX11/XXMI F10 test: {run}", flush=True)
    evidence = {"scope": "private DX11/XXMI sandbox, not the real game", "pass": False,
                "run": str(run), "source_read_only": str(args.efmi_root.resolve()),
                "private_hunting": 0,
                "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest()}
    proc = None
    client = None
    session = None
    output = []
    rc = 1
    try:
        assert_no_other_probes()
        # The existing sandbox uses SW_SHOWNOACTIVATE. The tool under test must
        # request and verify foreground itself before its sole real F10 pulse.
        proc = subprocess.Popen([str(run / "efmi_sandbox.exe"), "--xxmi", str(efmi / "d3d11.dll"),
                                 "--probe", str(probe), "--seconds", "60"], cwd=run,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        ready = threading.Event()

        def drain():
            for line in proc.stdout:
                output.append(line.rstrip())
                if line.strip() == "READY":
                    ready.set()

        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(20):
            raise RuntimeError("Private sandbox did not become ready within 20 seconds")
        for _ in range(50):
            try:
                client = ProbeClient.connect(pid=proc.pid)
                break
            except ProbeError:
                time.sleep(0.1)
        if client is None:
            raise RuntimeError("Could not connect to the explicitly spawned sandbox PID")
        evidence["pid"] = proc.pid
        evidence["before_status"] = client.status()
        session = tools.Session(efmi, run / "mcp-out", probe)
        session.client = client
        # Use the production tool registration and JSON-RPC tools/call boundary.
        # Only dependency construction is bound to our exact process/connection.
        with patch.object(tools, "Session", return_value=session):
            server = tools.build_server(efmi, run / "mcp-out", probe)

        def call(name, **arguments):
            reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
            if "error" in reply or reply["result"].get("isError"):
                raise RuntimeError(json.dumps(reply, ensure_ascii=False))
            return reply["result"]

        call("efmi_capture_frame", detail="full", cb_data=True)
        before = session.capture("latest")
        before_counts = game_ib_counts(before)
        if len(before_counts) < 2:
            raise RuntimeError("Sandbox needs multiple game IBs for an observable skip test")
        target_draw = next(d for d in before.draws() if d["origin"] != "xxmi"
                           and d["call"] == "DrawIndexedInstanced" and d["args"]["index_count"] == 864)
        target = target_draw["ib"]["hash"]
        target_vs = target_draw["shaders"]["vs"]
        evidence["target_ib"] = target
        evidence["target_vs"] = target_vs
        evidence["before_frame"] = before.frame
        evidence["before_ib_counts"] = dict(before_counts)
        write_json(run / "before-capture.json", before.doc)
        if target_vs in session.mods.get().by_hash:
            raise RuntimeError("Private mod index unexpectedly contains the new test shader already")
        # This file did not exist when the sandbox loaded its configuration.
        # Explicitly dispatch the sandbox IB override. XXMI region hashes need
        # match_index_count to identify the slice in a shared geometry buffer.
        removed = Counter({target: 1})
        expected_counts = before_counts - removed
        evidence["expected_after_ib_counts"] = dict(expected_counts)
        (efmi / "Mods" / "ReloadIntegration.ini").write_text(
            f"[ShaderOverrideReloadIntegration]\nhash = {target_vs}\nchecktextureoverride = ib\n\n"
            f"[TextureOverrideReloadIntegration]\nhash = {target}\n"
            f"match_index_count = {target_draw['args']['index_count']}\nhandling = skip\n", encoding="utf-8")
        call("efmi_live_start")
        assert_no_other_probes(own_pid=proc.pid)
        result = call("efmi_reload_and_capture", settle_ms=1000, timeout_ms=10000)
        receipt = json.loads(result["content"][0]["text"])
        evidence["reload_tool_result"] = receipt
        after = session.capture("latest")
        after_counts = game_ib_counts(after)
        evidence["after_ib_counts"] = dict(after_counts)
        evidence["after_frame"] = after.frame
        write_json(run / "after-capture.json", after.doc)
        if not receipt["reload_requested"] or receipt["reload_confirmed"]:
            raise RuntimeError("Tool must distinguish delivered input from a reload acknowledgement")
        if not receipt["live_stopped"] or client.status()["capture"]["live"]:
            raise RuntimeError("Continuous mode was not stopped")
        if not receipt["old_captures_invalidated"] or list(session.captures) != [after.frame]:
            raise RuntimeError("Old session captures survived reload")
        if after.frame <= receipt["barrier_frame"] or not len(after):
            raise RuntimeError("The captured frame is stale or empty")
        if after_counts != expected_counts:
            raise RuntimeError("New handling=skip did not suppress exactly the target IB draw; "
                               "F10 reload not demonstrated")
        if target_vs not in session.mods.get().by_hash:
            raise RuntimeError("Reload did not invalidate the pre-edit mod index")
        remaining = next(d for d in after.draws() if d.get("ib", {}).get("hash") in expected_counts)
        evidence["read_after_reload"] = call("efmi_read_buffer", draw=remaining["index"],
                                              slot="ib", capture="latest", count=3)
        evidence["pass"] = True
        evidence["observed_acknowledgement"] = (
            "Test-specific proof: newly written private handling=skip suppressed exactly the target IB draw; "
            "the remaining game IB counts matched.")
        rc = 0
    except Exception as exc:
        evidence["error"] = str(exc)
        print(f"FAIL: {exc}", flush=True)
    finally:
        if client is not None:
            client.close()
        if proc is not None:
            # Only our exact child PID is addressed; never terminate user's game.
            evidence["quit_event_sent"] = signal_own_quit(proc.pid)
            try:
                proc.wait(timeout=10)
                evidence["sandbox_exit_code"] = proc.returncode
                if proc.returncode != 0:
                    evidence["cleanup_error"] = "Private sandbox exited unsuccessfully"
                    evidence["pass"] = False
                    rc = 1
            except subprocess.TimeoutExpired:
                evidence["cleanup_error"] = "Own sandbox did not acknowledge quit; terminating only the Popen child"
                evidence["pass"] = False
                rc = 1
                # A modal message loop can prevent both the named event and
                # --seconds timeout from being processed. Use our retained
                # Popen process handle, never enumerate/terminate game PIDs.
                evidence["forced_termination"] = True
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    evidence["sandbox_exit_code"] = proc.returncode
                except (OSError, subprocess.TimeoutExpired) as exc:
                    evidence["termination_error"] = str(exc)
        (run / "process-output.log").write_text("\n".join(output), encoding="utf-8")
        write_json(run / "reload-evidence.json", evidence)
    print(("PASS" if rc == 0 else "FAIL") + f"; evidence: {run / 'reload-evidence.json'}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
