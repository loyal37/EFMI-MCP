"""Request the configured F10 reload, then retain a strictly later capture.

The probe has no reload acknowledgement. This workflow deliberately reports
input delivery and fresh capture separately from successful INI parsing.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .capture_model import Capture
from .mcp_stdio import ToolError
from .reload_input import ReloadInputError, send_reload_f10


def _options(detail, cb_data, settle_ms, timeout_ms, focus_game):
    if detail not in ("full", "light") or not isinstance(detail, str):
        raise ValueError("detail must be 'full' or 'light'")
    if type(cb_data) is not bool or type(focus_game) is not bool:
        raise ValueError("cb_data and focus_game must be booleans")
    if type(settle_ms) is not int or not 250 <= settle_ms <= 10000:
        raise ValueError("settle_ms must be an integer from 250 to 10000")
    if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 30000:
        raise ValueError("timeout_ms must be an integer from 1000 to 30000")


def _reload_bindings(root: Path):
    """Read only; never assume F10 still means reload in a customized install."""
    path = root / "d3dx.ini"
    section = ""
    found = {"reload_config": [], "reload_fixes": []}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
        elif section == "hunting" and "=" in line:
            key, value = (part.strip().lower() for part in line.split("=", 1))
            if key in found:
                found[key].append(value.split())
    for key, values in found.items():
        if values != [["no_modifiers", "vk_f10"]]:
            raise ValueError(f"{path}: expected one active [Hunting] {key} = no_modifiers VK_F10. "
                             "No key was sent; custom or ambiguous bindings are not changed automatically.")
    return {key: "no_modifiers VK_F10" for key in found}


def _identity(status: dict, root: Path):
    pid = status.get("pid")
    if type(pid) is not int or pid <= 0 or not status.get("exe"):
        raise ValueError("Probe did not identify a valid process")
    if not status.get("attached") or not status.get("swap_chain"):
        raise ValueError("Probe has no attached rendering device/swap chain yet")
    xxmi = status.get("xxmi", {})
    if not xxmi.get("present") or not xxmi.get("path"):
        raise ValueError("XXMI is not loaded in the selected probe process")
    actual = os.path.normcase(str(Path(xxmi["path"]).resolve()))
    expected = os.path.normcase(str((root / "d3d11.dll").resolve()))
    if actual != expected:
        raise ValueError("The selected process uses a different XXMI installation than this MCP's efmi_root")
    target = status.get("capture_target", {})
    if not all(target.get(key) and target[key] != "0x0" for key in ("device", "context", "swap_chain")):
        raise ValueError("Probe does not report the selected device/context/swap chain; use probe 0.1.1 or later")
    return (pid, status["exe"], actual, target["device"], target["context"], target["swap_chain"])


def reload_and_capture(session, *, detail="full", cb_data=True, settle_ms=2000,
                       timeout_ms=15000, focus_game=True):
    """One input attempt, no reconnect/reload retry; the resulting capture stays readable.

    Read-only preflight errors leave previous captures intact. Once release starts,
    all previous session captures and mod-index data are invalidated, even on error.
    Continuous mode is stopped and left off; restarting it is a separate decision.
    """
    phase = "preflight"
    started = time.monotonic()
    requested = False
    invalidated = False
    live_stopped = False
    receipt = None
    before = None
    try:
        _options(detail, cb_data, settle_ms, timeout_ms, focus_game)
        bindings = _reload_bindings(session.efmi_root)
        # Hold this exact connection through the operation. Session.call could
        # reconnect to another newest process, which is unsuitable for input.
        client = session.probe()
        before = client.status()
        identity = _identity(before, session.efmi_root)
        if client.endpoint.pid != identity[0]:
            raise ValueError("Connected pipe PID does not match the probe status PID")
        if before.get("capture", {}).get("recording") and not before.get("capture", {}).get("live"):
            raise ValueError("A capture is already recording; wait for that operation before reloading")

        phase = "invalidate"
        # Even a release/stop transport failure must not leave an old capture
        # advertised as 'latest' for an agent expecting post-reload data.
        session.captures.clear()
        session.latest = None
        session.mods.invalidate()
        invalidated = True
        if before.get("capture", {}).get("live"):
            client.live(enable=False)
            live_stopped = True
        client.release()

        phase = "input"
        requested = None  # An unexpected error at the input boundary has unknown delivery.
        receipt = send_reload_f10(identity[0], focus_game=focus_game)
        requested = bool(receipt.get("key_down_sent"))
        if not requested or not receipt.get("key_up_sent"):
            raise ValueError("Input helper did not confirm the complete F10 down/up pair")

        phase = "settle"
        time.sleep(settle_ms / 1000)
        barrier = client.status()
        if _identity(barrier, session.efmi_root) != identity:
            raise ValueError("Process or selected rendering device changed after the reload request")

        phase = "capture"
        doc, blob = client.capture(detail=detail, cb_data=cb_data, timeout_ms=timeout_ms)
        cap = Capture(doc, blob)
        if cap.frame <= max(before["frame"], barrier["frame"]):
            raise ValueError("Probe returned a stale frame rather than a frame after the reload wait")
        if not len(cap):
            raise ValueError("Post-request frame contains no draws; fresh rendering is not verified")
        after = client.status()
        if _identity(after, session.efmi_root) != identity:
            raise ValueError("Process or selected rendering device changed during the fresh capture")
        session.add_capture(cap)
        counts = {}
        for draw in cap.draws():
            origin = draw["origin"]
            counts[origin] = counts.get(origin, 0) + 1
        return {
            "status": "captured_after_reload_request", "pid": identity[0],
            "reload_requested": True, "reload_confirmed": False,
            "confirmation_note": "F10 input was delivered and a later nonempty frame captured. "
                                 "Probe 0.1.1 has no reload acknowledgement; inspect expected draws/resources "
                                 "to verify the edited mod. Do not treat this as proof that every INI parsed successfully.",
            "input": receipt, "bindings": bindings,
            "before_frame": before["frame"], "barrier_frame": barrier["frame"],
            "live_stopped": live_stopped, "old_captures_invalidated": invalidated,
            "settle_ms": settle_ms, "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "capture": {"frame": cap.frame, "detail": cap.detail, "draws": len(cap), "origins": counts,
                        "resources": len(cap.resources), "shaders": len(cap.shaders), "cb_data": cb_data,
                        "cb_snapshot_bytes": len(blob), "skipped_draws": cap.stats.get("skipped_draws", 0),
                        "arena_full": bool(cap.stats.get("arena_full"))},
            "next": "Use efmi_list_draws / efmi_get_draw / efmi_read_buffer on capture='latest'. "
                    "For additional frames use efmi_capture_frame; do not resend F10 just to get newer data.",
        }
    except Exception as exc:
        if isinstance(exc, ReloadInputError):
            receipt = exc.details
            requested = receipt.get("key_down_sent", False)
        if invalidated:
            session.captures.clear()
            session.latest = None
        # The server exposes tool errors explicitly. Never label input success
        # followed by a capture failure as 'nothing happened' or silently retry.
        failure = {"status": "failed", "phase": phase, "reason": str(exc),
                   "reload_requested": requested, "reload_confirmed": False,
                   "old_captures_invalidated": invalidated, "live_stopped": live_stopped,
                   "input": receipt, "fresh_capture": None,
                   "next": "No automatic key retry was made. If F10 was already sent, inspect status and use "
                           "efmi_capture_frame for another frame; only retry reload after addressing the reported cause."}
        raise ToolError(json.dumps(failure, ensure_ascii=False)) from exc
