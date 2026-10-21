"""Actual F11 -> EFMI switches mods off/on -> fresh-capture test in a new private DX11 sandbox.

The private copy holds the real XXMI runtime and the real EFMI core, so F11 is
bound exactly as in the game: [KeyToggleMods] flips $enable_mods, and the
core's own rule stops applying index-buffer overrides while it is 0. The test
mod has no shader rule of its own; only that core rule makes it apply.

Real keys are pressed (one F10 to load the test mod, F11 several times), by the
probe and only while its own sandbox window has the foreground, so a game
running next to it does not react. Nothing is launched, stopped or edited
outside the private copy under sandbox/build/toggle-*.
This validates the sandbox path, not the real game or its integrity level.

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
from prepare_run import DEFAULT_EFMI  # noqa: E402
from reload_integration_test import game_ib_counts, private_copy, signal_own_quit, write_json  # noqa: E402

failures = 0


def check(name, condition, detail=""):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not condition else ""), flush=True)
    failures += not condition
    return bool(condition)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--efmi-root", type=Path, default=DEFAULT_EFMI,
                        help="Read-only source for a new private XXMI copy")
    args = parser.parse_args()
    probe = Path(os.environ.get("EFMI_PROBE_DLL") or ROOT / "dist" / "efmi_probe.dll").resolve()
    run, efmi = private_copy(args.efmi_root.resolve(), probe)
    run = run.rename(run.with_name(run.name.replace("reload-", "toggle-", 1)))
    efmi = run / "EFMI"
    print(f"Private real-DX11/XXMI F11 test: {run}", flush=True)
    evidence = {"scope": "private DX11/XXMI sandbox, not the real game", "pass": False, "run": str(run),
                "source_read_only": str(args.efmi_root.resolve()),
                "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest()}
    proc = client = None
    output = []
    try:
        proc = subprocess.Popen([str(run / "efmi_sandbox.exe"), "--xxmi", str(efmi / "d3d11.dll"),
                                 "--probe", str(probe), "--seconds", "120"], cwd=run,
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
        if "mods_key" not in status.get("features", ()):
            raise RuntimeError(f"Probe {status.get('probe')} cannot press F11; build 0.1.3 or later and set EFMI_PROBE_DLL")
        session = tools.Session(efmi, run / "mcp-out", probe)
        session.client = client
        with patch.object(tools, "Session", return_value=session):
            server = tools.build_server(efmi, run / "mcp-out", probe)

        def call(name, **arguments):
            reply = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
            text = reply["result"]["content"][0]["text"]
            return (json.loads(text) if text.lstrip().startswith("{") else text), reply["result"].get("isError")

        def toggle(step, **arguments):
            result, error = call("efmi_toggle_mods", **arguments)
            evidence[step] = result
            if error:
                raise RuntimeError(f"{step}: {json.dumps(result, ensure_ascii=False)[:600]}")
            return result, game_ib_counts(session.capture("latest"))

        print("== a mod that only EFMI's own rule applies")
        call("efmi_capture_frame", detail="full")
        before = session.capture("latest")
        original = game_ib_counts(before)
        target_draw = next(d for d in before.draws() if d["origin"] != "xxmi"
                           and d["call"] == "DrawIndexedInstanced" and d["args"]["index_count"] == 864)
        target = target_draw["ib"]["hash"]
        evidence.update(target_ib=target, original_ib_counts=dict(original))
        (efmi / "Mods" / "ToggleIntegration.ini").write_text(
            f"[TextureOverrideToggleIntegration]\nhash = {target}\n"
            f"match_index_count = {target_draw['args']['index_count']}\nhandling = skip\ndrawindexed = auto\n",
            encoding="utf-8")
        result, error = call("efmi_reload_and_capture", settle_ms=1000, timeout_ms=10000)
        evidence["reload"] = result
        if error:
            raise RuntimeError("reload: " + json.dumps(result, ensure_ascii=False)[:600])
        with_mod = original.copy()
        del with_mod[target]
        loaded = game_ib_counts(session.capture("latest"))
        if not check("after the reload the mod hides its mesh (EFMI's $enable_mods rule applies it)", loaded == with_mod,
                     f"{dict(loaded)} != {dict(with_mod)}"):
            raise RuntimeError("EFMI's core rule does not apply index-buffer overrides in this sandbox")
        frame_on = session.latest

        print("== off")
        result, counts = toggle("off", state="off")
        check("one press", result["presses"] == 1, str(result["presses"]))
        check("EFMI reports mods disabled", result["mods_enabled"] is False and "Disabled" in result["state_evidence"].get("text", ""),
              json.dumps(result["state_evidence"], ensure_ascii=False))
        check("the game draws the mesh again", counts == original, f"{dict(counts)} != {dict(original)}")
        check("the summary counts it as a mesh that a mod overrides",
              result["drawn"]["game_draws_of_meshes_that_mods_override"] == original[target], str(result["drawn"]))
        pressed = result["input"][0]
        check("F11 was held across a frame and released at a frame boundary",
              pressed["key"] == "F11" and pressed["action"] == "toggle_mods" and pressed["key_down_observed"]
              and pressed["key_up_sent"] and pressed["released_on"] in ("two_frames", "stalled_frame"), str(pressed))
        check("the foreground was handed back", not pressed["focus_changed"] or pressed["focus_restored"], str(pressed))
        check("the capture taken with mods on is still in memory", frame_on in session.captures)
        text, error = call("efmi_diff_captures", a=frame_on)
        check("the diff shows the mesh only in the frame with mods off", not error and target in str(text), str(text)[:300])
        text, _ = call("efmi_capture_frame", detail="light")
        check("a later capture reminds that mods are off", "switched all mods OFF" in str(text), str(text)[-300:])

        print("== off again: the first press switches on, the second off")
        result, counts = toggle("off_again", state="off")
        check("two presses", result["presses"] == 2, str(result["presses"]))
        check("EFMI reports mods disabled", result["mods_enabled"] is False, str(result["state_evidence"]))
        check("the game still draws the mesh", counts == original, f"{dict(counts)}")

        print("== toggle")
        result, counts = toggle("toggle")
        check("one press", result["presses"] == 1)
        check("EFMI reports mods enabled", result["mods_enabled"] is True and "Enabled" in result["state_evidence"].get("text", ""),
              json.dumps(result["state_evidence"], ensure_ascii=False))
        check("the mod hides the mesh again", counts == with_mod, f"{dict(counts)}")

        print("== on when known to be on: no key")
        result, counts = toggle("on_known", state="on")
        check("no press, because EFMI said so last time and the frame agrees",
              result["presses"] == 0 and result["mods_enabled"] is True and result["state_evidence"]["source"] == "no key pressed",
              json.dumps(result["state_evidence"], ensure_ascii=False))
        check("the mod hides the mesh", counts == with_mod, f"{dict(counts)}")

        print("== on when on but not known (a fresh server): two presses, back on")
        session.mods_display = None
        result, counts = toggle("on_again", state="on")
        check("two presses and enabled", result["presses"] == 2 and result["mods_enabled"] is True, str(result["presses"]))
        check("the mod hides the mesh", counts == with_mod, f"{dict(counts)}")

        print("== the probe checks the bindings itself (asked directly, past the server's own check)")
        files = {path: path.read_bytes() for path in (efmi / "Core" / "EFMI" / "KeyBindings.ini", efmi / "d3dx.ini")}
        bindings, d3dx = files
        try:
            bindings.write_bytes(files[bindings].replace(b"no_modifiers VK_F11", b"no_modifiers VK_F9"))
            receipt = client.mods_key()
            check("a changed [KeyToggleMods] binding is refused without a key",
                  receipt["code"] == "binding_mismatch" and not receipt["key_down_sent"], str(receipt))
            bindings.write_bytes(files[bindings])
            text, count = re.subn(rb"(?mi)^\[Hunting\][ \t]*\r?$",
                                  lambda m: m.group(0) + b"\nshow_original = no_modifiers VK_F11\r", files[d3dx], count=1)
            d3dx.write_bytes(text)
            receipt = client.mods_key()
            check("a [Hunting] function on plain F11 is refused and named",
                  count == 1 and receipt["code"] == "binding_mismatch" and "show_original" in receipt["message"]
                  and not receipt["key_down_sent"], str(receipt))
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
        check("the reload succeeds", not error and result.get("reload_requested") is True, json.dumps(result, ensure_ascii=False)[:400])
        check("the mod hides the mesh again without another F11", game_ib_counts(session.capture("latest")) == with_mod)
        check("the server no longer claims to know the switch", session.mods_display is None)
        write_json(run / "last-capture.json", session.capture("latest").doc)
    except Exception as exc:
        evidence["error"] = str(exc)
        check("the test ran to its end", False, str(exc))
    finally:
        if client is not None:
            client.close()
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
        write_json(run / "toggle-evidence.json", evidence)
    print(("PASS" if not failures else f"FAIL: {failures} failed") + f"; evidence: {run / 'toggle-evidence.json'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
