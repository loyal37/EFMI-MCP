"""ZZMI in a new private DX11 sandbox: hashes, F10 and F6 as Zenless Zone Zero's XXMI has them.

The private copy holds the real XXMI runtime and the real ZZMI core. ZZMI does
not track region hashes: a mod names a mesh by the creation hash of the whole
index buffer, and its [KeyToggleMods] binds F6 to $costume_mods, which the
core's CommandListSkin checks before it applies a mod's index-buffer override.
ZZMI prints nothing when the key is pressed, so the server reads the state from
the frame's draws.

Checks:
  - the server adopts the installation of the connected game (ZZMI), whatever
    folder it was started with, and shows whole-buffer hashes;
  - XXMI itself matches a mod written with the hash this server shows (the
    mesh disappears after one F10), and not one written with the region hash;
  - the trigger fires on that hash;
  - F6 switches mods off and on, with the state read from the draws; 'on' when
    known presses nothing; a reload switches mods on again;
  - the probe refuses a changed binding and a [Hunting] function on plain F6.

Real keys are pressed by the probe and only while its own sandbox window has
the foreground. Nothing is launched, stopped or edited outside the private copy
under sandbox/build/zzmi-*. This validates the sandbox path, not the real game.

EFMI_PROBE_DLL selects the probe build to test (default dist/efmi_probe.dll).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(HERE))

from efmi_mcp import tools  # noqa: E402
from efmi_mcp.probe_client import ProbeClient, ProbeError  # noqa: E402
from reload_integration_test import (assert_no_other_probes, game_ib_counts, private_copy,  # noqa: E402
                                     signal_own_quit, write_json)

DEFAULT_ZZMI = Path(r"D:\XXMI-Launcher-Portable-v2.1.6\ZZMI")
failures = 0


def check(name, condition, detail=""):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not condition else ""), flush=True)
    failures += not condition
    return bool(condition)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zzmi-root", type=Path, default=DEFAULT_ZZMI,
                        help="Read-only source for a new private XXMI copy")
    args = parser.parse_args()
    probe = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll").resolve()
    assert_no_other_probes()
    run, xxmi = private_copy(args.zzmi_root.resolve(), probe)
    run = run.rename(run.with_name(run.name.replace("reload-", "zzmi-", 1)))
    xxmi = (run / "EFMI").rename(run / "ZZMI")
    print(f"Private real-DX11/ZZMI test: {run}", flush=True)
    evidence = {"scope": "private DX11/XXMI sandbox with the ZZMI core, not the real game", "pass": False,
                "run": str(run), "source_read_only": str(args.zzmi_root.resolve()),
                "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest()}
    proc = client = session = None
    output = []
    try:
        proc = subprocess.Popen([str(run / "efmi_sandbox.exe"), "--xxmi", str(xxmi / "d3d11.dll"),
                                 "--probe", str(probe), "--seconds", "150"], cwd=run,
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
        status = client.status()
        evidence["probe"] = status.get("probe")
        if "mods_key_install" not in status.get("features", ()):
            raise RuntimeError(f"Probe {status.get('probe')} cannot press F6; build 0.1.12 or later and set EFMI_PROBE_DLL")

        print("== the installation")
        check("the probe reports the loaded XXMI", Path(status["xxmi"]["path"]).resolve() == (xxmi / "d3d11.dll").resolve(),
              str(status.get("xxmi")))
        check("and ZZMI's hash settings", status.get("xxmi_settings", {}).get("track_region_hashes") is False
              or status.get("xxmi_settings", {}).get("track_region_hashes") == 0, str(status.get("xxmi_settings")))
        client.close()
        client = None
        # Started for another folder, as the configured EFMI would be: the game's installation wins.
        configured = run / "configured-elsewhere"
        session = tools.Session(configured, run / "mcp-out", probe)
        with patch.object(tools, "Session", return_value=session):
            server = tools.build_server(configured, run / "mcp-out", probe)

        def call(name, **arguments):
            reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
            text = reply["result"]["content"][0]["text"]
            return (json.loads(text) if text.lstrip().startswith("{") else text), reply["result"].get("isError")

        text, error = call("efmi_status")         # the server connects and adopts the installation itself
        evidence["status"] = text
        check("efmi_status names ZZMI and its folder", not error and f"ZZMI folder: {xxmi}" in str(text), str(text)[-600:])
        check("efmi_status shows whole-buffer hashes", "the whole buffer (creation hashes)" in str(text))
        check("efmi_status names F6 for the mods switch", "F6" in str(text))
        check("the session uses ZZMI", session.install.name == "ZZMI" and not session.region_hashes,
              f"{session.install} {session.region_hashes}")

        print("== hashes")
        call("efmi_capture_frame", detail="full")
        before = session.capture("latest")
        check("the capture says how XXMI hashes buffers", before.region_hashes is False)
        indexed = [d for d in before.draws() if d["origin"] != "xxmi" and d.get("ib")]
        check("every game draw names its index buffer by the whole buffer's hash",
              indexed and all(d["ib"]["hash"] == d["ib"]["resource_hash"] for d in indexed),
              str([(d["ib"]["hash"], d["ib"]["resource_hash"]) for d in indexed][:5]))
        check("vertex buffers likewise", all(vb["hash"] == vb["resource_hash"] for d in indexed
                                             for vb in d.get("vb", {}).values()))
        target_draw = next(d for d in indexed if d["call"] == "DrawIndexedInstanced" and d["args"]["index_count"] == 864)
        target = target_draw["ib"]["hash"]
        region = (target_draw["ib"].get("region") or {}).get("hash")
        original = game_ib_counts(before)
        evidence.update(target_ib=target, target_region_hash=region, original_ib_counts=dict(original))
        check("the region hash differs (a mod written with it would not match)", region and region != target,
              f"{region} vs {target}")
        text, error = call("efmi_list_draws", ib_hash=target)
        check("efmi_list_draws filters by the whole-buffer hash", not error and f"ib={target}" in str(text), str(text)[:300])

        print("== trigger")
        result, error = call("efmi_trigger_arm", ib=target)
        check("the trigger is armed", not error, json.dumps(result, ensure_ascii=False)[:300])
        result, error = call("efmi_trigger_wait", wait_s=20)
        evidence["trigger"] = result
        check("it fires on the whole-buffer hash", not error and target in json.dumps(result),
              json.dumps(result, ensure_ascii=False)[:400])

        print("== a mod written with the region hash does nothing")
        mod = xxmi / "Mods" / "ZZMIIntegration.ini"
        mod.write_text(f"[TextureOverrideZZMIRegion]\nhash = {region}\nhandling = skip\n", encoding="utf-8")
        result, error = call("efmi_reload_and_capture", settle_ms=1000, timeout_ms=10000)
        evidence["reload_region"] = result
        check("the reload ran", not error and result.get("reload_requested") is True, json.dumps(result, ensure_ascii=False)[:400])
        check("the mesh is still drawn", game_ib_counts(session.capture("latest")) == original,
              str(dict(game_ib_counts(session.capture("latest")))))

        print("== a mod written with the hash this server shows")
        # Like a mod that replaces the mesh: the game's draw is skipped and XXMI draws it (here unchanged).
        mod.write_text(f"[TextureOverrideZZMIIntegration]\nhash = {target}\n"
                       f"match_index_count = {target_draw['args']['index_count']}\nhandling = skip\n"
                       "drawindexed = auto\n", encoding="utf-8")
        result, error = call("efmi_reload_and_capture", settle_ms=1000, timeout_ms=10000)
        evidence["reload"] = result
        if error:
            raise RuntimeError("reload: " + json.dumps(result, ensure_ascii=False)[:600])
        with_mod = original.copy()
        del with_mod[target]
        loaded = game_ib_counts(session.capture("latest"))
        if not check("XXMI matches it: the mesh disappears", loaded == with_mod, f"{dict(loaded)} != {dict(with_mod)}"):
            raise RuntimeError("ZZMI does not apply the override written with the shown hash")

        def toggle(step, **arguments):
            result, error = call("efmi_toggle_mods", **arguments)
            evidence[step] = result
            if error:
                raise RuntimeError(f"{step}: {json.dumps(result, ensure_ascii=False)[:600]}")
            return result, game_ib_counts(session.capture("latest"))

        print("== off (F6)")
        result, counts = toggle("off", state="off")
        check("one press", result["presses"] == 1, str(result["presses"]))
        check("the installation and key are named", result.get("installation") == "ZZMI" and result.get("key") == "F6",
              f"{result.get('installation')} {result.get('key')}")
        check("mods disabled, read from the draws", result["mods_enabled"] is False
              and result["state_evidence"].get("source") == "draws", json.dumps(result["state_evidence"], ensure_ascii=False))
        check("the game draws the mesh again", counts == original, f"{dict(counts)} != {dict(original)}")
        pressed = result["input"][0]
        check("F6 was held across a frame and released at a frame boundary",
              pressed["key"] == "F6" and pressed["action"] == "toggle_mods" and pressed["key_down_observed"]
              and pressed["key_up_sent"] and pressed["released_on"] in ("two_frames", "stalled_frame"), str(pressed))
        check("the foreground was handed back", not pressed["focus_changed"] or pressed["focus_restored"], str(pressed))

        print("== toggle")
        result, counts = toggle("toggle")
        check("one press, mods enabled", result["presses"] == 1 and result["mods_enabled"] is True,
              json.dumps(result["state_evidence"], ensure_ascii=False))
        check("the mod hides the mesh again", counts == with_mod, f"{dict(counts)}")

        print("== on when known to be on: no key")
        result, counts = toggle("on_known", state="on")
        check("no press", result["presses"] == 0 and result["mods_enabled"] is True, str(result["state_evidence"]))

        print("== the probe checks ZZMI's binding itself")
        main_ini, d3dx = xxmi / "Core" / "ZZMI" / "main.ini", xxmi / "d3dx.ini"
        files = {path: path.read_bytes() for path in (main_ini, d3dx)}
        try:
            main_ini.write_bytes(files[main_ini].replace(b"Key = no_modifiers F6", b"Key = no_modifiers F7", 1))
            receipt = session.client.mods_key()
            check("a changed [KeyToggleMods] binding is refused without a key",
                  receipt["code"] == "binding_mismatch" and not receipt["key_down_sent"] and "ZZMI" in receipt["message"],
                  str(receipt))
            main_ini.write_bytes(files[main_ini])
            text, count = re.subn(rb"(?mi)^\[Hunting\][ \t]*\r?$",
                                  lambda m: m.group(0) + b"\nshow_original = no_modifiers VK_F6\r", files[d3dx], count=1)
            d3dx.write_bytes(text)
            receipt = session.client.mods_key()
            check("a [Hunting] function on plain F6 is refused and named",
                  count == 1 and receipt["code"] == "binding_mismatch" and "show_original" in receipt["message"]
                  and "F6" in receipt["message"] and not receipt["key_down_sent"], str(receipt))
        finally:
            for path, data in files.items():
                path.write_bytes(data)
        call("efmi_capture_frame", detail="light")
        check("mods are still on after the refused presses", game_ib_counts(session.capture("latest")) == with_mod)

        print("== a reload switches mods back on")
        result, counts = toggle("off_before_reload", state="off")
        check("mods are off before the reload", result["mods_enabled"] is False and counts == original)
        result, error = call("efmi_reload_and_capture", settle_ms=1000, timeout_ms=10000)
        evidence["reload_after_off"] = result
        check("the reload succeeds", not error and result.get("reload_requested") is True,
              json.dumps(result, ensure_ascii=False)[:400])
        check("the mod hides the mesh again without another F6", game_ib_counts(session.capture("latest")) == with_mod)
        write_json(run / "last-capture.json", session.capture("latest").doc)
    except Exception as exc:
        evidence["error"] = str(exc)
        check("the test ran to its end", False, str(exc))
    finally:
        if client is not None:
            client.close()
        if session is not None and session.client is not None:
            session.client.close()
        if proc is not None:
            # Only our exact child PID is addressed; never terminate the user's game.
            evidence["quit_event_sent"] = signal_own_quit(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
                evidence["forced_termination"] = True
            evidence["sandbox_exit_code"] = proc.returncode
        (run / "process-output.log").write_text("\n".join(output), encoding="utf-8")
        evidence["pass"] = not failures
        write_json(run / "zzmi-evidence.json", evidence)
    print(("PASS" if not failures else f"FAIL: {failures} failed") + f"; evidence: {run / 'zzmi-evidence.json'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
