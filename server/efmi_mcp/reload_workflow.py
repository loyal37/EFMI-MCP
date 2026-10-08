"""Request the configured F10 reload, then retain a strictly later capture.

Input delivery, the fresh capture and evidence that XXMI really reloaded are
reported separately. XXMI gives no acknowledgement of its own; the evidence is
what it has to do after a reload: create the mod resources again.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .capture_model import Capture
from .mcp_stdio import ToolError
from .reload_input import ReloadInputError, send_reload_f10, send_reload_via_probe

RESUME_TIMEOUT_S = 10.0    # how long a reload may keep the game from presenting a frame
CONFIRM_PAUSE_S = 0.25     # between looks at the frames after a reload, while mods are still coming back

# Probes before 0.1.4 kept region hashes by buffer address. A reload destroys the mods' buffers and
# creates new ones, some of them at addresses earlier buffers had: such a probe can record an old
# buffer's hash.
STALE_HASH_CAUTION = ("This probe ({version}) can record, for a buffer created by the reload, the region hash of the "
                      "buffer that was at the same address before it. Do not conclude from an unchanged ib/vb hash "
                      "that an edited buffer was not loaded: read it with efmi_read_buffer. Probe 0.1.4 fixes this; "
                      "restart the game to load the updated efmi_probe.dll.")

# The server cannot deliver a key to an elevated game (UIPI); only the probe can.
_OLD_PROBE_HINT = ("This probe cannot press the key itself, and Windows silently drops keys that this "
                   "unelevated server sends to a game running as administrator. Probe 0.1.2 or later sends "
                   "F10 from inside the game: restart the game so the updated efmi_probe.dll is loaded.")


def _options(detail, cb_data, settle_ms, timeout_ms, focus_game, restore_focus, confirm_wait_ms=0):
    if type(confirm_wait_ms) is not int or not 0 <= confirm_wait_ms <= 15000:
        raise ValueError("confirm_wait_ms must be an integer from 0 to 15000")
    if detail not in ("full", "light") or not isinstance(detail, str):
        raise ValueError("detail must be 'full' or 'light'")
    if type(cb_data) is not bool or type(focus_game) is not bool or type(restore_focus) is not bool:
        raise ValueError("cb_data, focus_game and restore_focus must be booleans")
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


def reload_evidence(cap: Capture, receipt: dict | None) -> dict | None:
    """What XXMI created after the key went down, among the things this frame uses.

    A config reload destroys every mod resource and custom shader; XXMI creates
    them again the next time a mod draws. Finding such objects in the fresh
    frame therefore shows that the reload took place. Finding none proves
    nothing by itself: without mod content on screen there is nothing to create.
    Needs a probe that reports creation frames and pressed the key itself.
    """
    key_frame = (receipt or {}).get("frame_at_key_down")
    if type(key_frame) is not int:
        return None
    if not any("cf" in r for r in cap.resources if r.get("known")):
        return None

    used: set[int] = set()
    mesh: set[int] = set()          # index/vertex buffers of draws issued by XXMI
    shaders: set[int] = set()
    for d in cap.raw_draws:
        by_xxmi = d.get("o") == "xxmi"
        bound = [d["ib"][0]] if "ib" in d else []
        bound += [vb[1] for vb in d.get("vb", [])]
        if by_xxmi:
            mesh.update(bound)
        bound += [cb[2] for cb in d.get("cb", [])]
        bound += [view[2] for view in d.get("srv", [])]
        bound += [target[2] for target in d.get("out", [])]
        used.update(bound)
        shaders.update(sid for sid in d.get("sh", []) if sid >= 0)

    def from_xxmi(rid):
        r = cap.resource(rid)
        # IniParams is a Texture1D that XXMI keeps across reloads.
        return r if r and r.get("known") and r.get("origin") == "xxmi" and r.get("kind") != "tex1d" else None

    created = [rid for rid in sorted(used) if from_xxmi(rid) and from_xxmi(rid).get("cf", 0) > key_frame]
    older_mesh = [rid for rid in sorted(mesh) if from_xxmi(rid) and from_xxmi(rid).get("cf", 0) <= key_frame]
    new_shaders = [sid for sid in sorted(shaders) if cap.shaders[sid].get("known")
                   and cap.shaders[sid].get("origin") == "xxmi" and cap.shaders[sid].get("cf", 0) > key_frame]
    # Resources XXMI kept creating on its own before the key press say nothing
    # about a reload, so they must not be read as its acknowledgement.
    churn = (receipt or {}).get("xxmi_creates_before_press", 0)
    examples = [{"res": rid, "kind": cap.resource(rid).get("kind"), "hash": cap.resource(rid).get("hash"),
                 "created_frame": cap.resource(rid).get("cf")} for rid in created[:6]]
    return {
        "key_down_frame": key_frame,
        "resources_created_after_key": len(created),
        "shaders_created_after_key": len(new_shaders),
        "xxmi_mesh_buffers_older_than_key": len(older_mesh),
        "xxmi_creations_just_before_key": churn,
        "confirmed": bool(created or new_shaders) and not churn,
        "examples": examples,
    }


def _note(in_process: bool, receipt: dict, evidence: dict | None, looks: int = 1, settled: bool | None = None) -> str:
    if not in_process:
        return ("F10 events were accepted, the sender observed the pressed key state, "
                "and a later nonempty frame was captured. "
                "This probe has no reload evidence; inspect expected draws/resources "
                "to verify the edited mod. Do not treat this as proof that every INI parsed successfully.")
    pressed = (f"F10 was pressed inside the game process and seen there "
               f"(held across {receipt.get('frames_while_held')} frame(s), released on {receipt.get('released_on')}). ")
    if evidence and evidence["confirmed"]:
        text = (pressed + f"XXMI then created {evidence['resources_created_after_key']} resource(s) and "
                f"{evidence['shaders_created_after_key']} shader(s) that this frame uses. A reload destroys mod "
                "resources and they are created again on first use, so the reload took place. "
                "This does not prove that every INI parsed without errors: check the draws you edited.")
        if settled is False:
            text += (" Mods were still coming back when the wait ended (what XXMI draws of its new objects was "
                     "still growing): this frame may show only some of them. Capture again with efmi_capture_frame, "
                     "without another F10.")
        return text
    text = pressed + "Nothing that XXMI created after the key press is used by the captured frame"
    if looks > 1:
        text += f" (nor by the {looks - 1} frame(s) looked at before it)"
    if evidence and evidence["xxmi_creations_just_before_key"]:
        text += (" in a way that can be told apart: XXMI was already creating resources on its own before the "
                 "key press. Verify the edit by its content.")
    elif evidence and evidence["xxmi_mesh_buffers_older_than_key"]:
        text += (f", and {evidence['xxmi_mesh_buffers_older_than_key']} index/vertex buffer(s) drawn by XXMI are "
                 "older than the key press. A reload replaces mod buffers, so unless those belong to XXMI's own "
                 "overlay the reload did not take effect.")
    else:
        text += ". That is expected when no mod content is on screen; check the draws/resources you edited."
    return text


def confirm_on_frame(reload: dict, cap: Capture) -> dict | None:
    """Evidence from a later frame for a reload that its first frame could not confirm.

    For some frames after a reload no mod may be drawn at all, so nothing that
    XXMI created is in use yet; a frame captured afterwards shows it. Returns
    the fields to update in the reload result, or None when this frame
    confirms nothing either.
    """
    if reload.get("reload_confirmed") or not reload.get("reload_requested"):
        return None
    evidence = reload_evidence(cap, reload.get("input"))
    if not evidence or not evidence["confirmed"]:
        return None
    return {"reload_confirmed": True, "reload_evidence": evidence, "confirmed_on_frame": cap.frame,
            "confirmation_note": _note(True, reload["input"], evidence).replace(
                "that this frame uses", f"that frame {cap.frame} uses (the first frame after the reload used none yet)")}


def reload_and_capture(session, *, detail="full", cb_data=True, settle_ms=2000,
                       timeout_ms=15000, focus_game=True, restore_focus=True, confirm_wait_ms=3000):
    """One input attempt, no reconnect/reload retry; the resulting capture stays readable.

    Read-only preflight errors leave previous captures intact. Once release starts,
    all previous session captures and mod-index data are invalidated, even on error.
    Continuous mode is stopped and left off; restarting it is a separate decision.

    After a reload mods come back over several frames: buffers and shaders are
    created on first use, so at first no mod draws, then some of them, then
    all. Light frames are looked at until what XXMI draws of the objects it
    created since the key press has stopped growing, for up to
    ``confirm_wait_ms``, and the frame that is returned is taken after that.
    The key is pressed once in any case.
    """
    phase = "preflight"
    started = time.monotonic()
    requested = False
    invalidated = False
    live_stopped = False
    in_process = False
    receipt = None
    before = None
    try:
        _options(detail, cb_data, settle_ms, timeout_ms, focus_game, restore_focus, confirm_wait_ms)
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
        in_process = "reload_key" in (before.get("features") or ())

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
        # A reload sets EFMI's $enable_mods back to 1: what efmi_toggle_mods last saw no longer holds.
        session.mods_display = None
        if in_process:
            receipt = send_reload_via_probe(client, focus_game=focus_game, restore_focus=restore_focus)
        else:
            receipt = send_reload_f10(identity[0], focus_game=focus_game)
        requested = bool(receipt.get("key_down_sent"))
        if not requested or not receipt.get("key_up_sent"):
            raise ValueError("Input helper did not confirm the complete F10 down/up pair")
        if receipt.get("key_down_observed") is not True:
            raise ValueError("Input helper did not observe F10's asynchronous pressed state; "
                             "an accepted SendInput event alone cannot establish effective input")

        phase = "settle"
        # Loading a large mod can keep the game from presenting for longer than the
        # settle time, which would then be over before the first frame after the
        # reload. The wait counts from the moment the game presents again.
        resumed_ms = None
        key_up = receipt.get("frame_at_key_up")
        if in_process and type(key_up) is int:
            waiting = time.monotonic()
            while client.status()["frame"] <= key_up:
                if time.monotonic() - waiting >= RESUME_TIMEOUT_S:
                    raise ValueError(f"The game presented no frame for {RESUME_TIMEOUT_S:.0f} s after the reload key "
                                     "was released")
                time.sleep(0.05)
            resumed_ms = round((time.monotonic() - waiting) * 1000, 1)
        time.sleep(settle_ms / 1000)
        barrier = client.status()
        if _identity(barrier, session.efmi_root) != identity:
            raise ValueError("Process or selected rendering device changed after the reload request")

        phase = "capture"
        stale = max(before["frame"], barrier["frame"])

        def fresh(frame: Capture) -> Capture:
            if frame.frame <= stale:
                raise ValueError("Probe returned a stale frame rather than a frame after the reload wait")
            if not len(frame):
                raise ValueError("Post-request frame contains no draws; fresh rendering is not verified")
            return frame

        looks, waited_ms, settled = 0, None, None
        if in_process and confirm_wait_ms:
            # Mods come back over several frames (seen in the game: three frames after the
            # key 272 of 508 mod draws were back, and the reload already counted as
            # confirmed). Light frames are enough to watch what XXMI draws of its new
            # objects grow; the frame to return is recorded once that has stopped. It is
            # recorded last because only the probe's most recent capture can be read.
            waiting = time.monotonic()
            amount = None
            while True:
                doc, _ = client.capture(detail="light", cb_data=False, timeout_ms=timeout_ms)
                seen = reload_evidence(fresh(Capture(doc)), receipt)
                looks += 1
                if seen is None or seen["xxmi_creations_just_before_key"]:
                    break                   # nothing that looking longer could tell apart
                if seen["confirmed"]:
                    now = seen["resources_created_after_key"] + seen["shaders_created_after_key"]
                    if amount is not None and now <= amount:
                        settled = True
                        break
                    amount = now
                if time.monotonic() - waiting >= confirm_wait_ms / 1000:
                    settled = None if amount is None else False
                    break
                time.sleep(CONFIRM_PAUSE_S)
            waited_ms = round((time.monotonic() - waiting) * 1000, 1)
        doc, blob = client.capture(detail=detail, cb_data=cb_data, timeout_ms=timeout_ms)
        cap = fresh(Capture(doc, blob))
        looks += 1
        after = client.status()
        if _identity(after, session.efmi_root) != identity:
            raise ValueError("Process or selected rendering device changed during the fresh capture")
        session.add_capture(cap)
        evidence = reload_evidence(cap, receipt) if in_process else None
        counts = {}
        for draw in cap.draws():
            origin = draw["origin"]
            counts[origin] = counts.get(origin, 0) + 1
        caution = None if "region_hash_per_object" in (before.get("features") or ()) else \
            STALE_HASH_CAUTION.format(version=before.get("probe", "?"))
        return {
            **({"hash_caution": caution} if caution else {}),
            "status": "captured_after_reload_request", "pid": identity[0],
            "reload_requested": True, "reload_confirmed": bool(evidence and evidence["confirmed"]),
            "confirmation_note": _note(in_process, receipt, evidence, looks, settled),
            "reload_evidence": evidence,
            # mods_settled: true once what XXMI draws of its new objects stopped growing between two looks,
            # false when it was still growing as the wait ended, null when nothing of it appeared or nobody looked.
            "frames_looked_at": looks, "waited_for_xxmi_ms": waited_ms, "mods_settled": settled,
            "sender": "probe" if in_process else "server",
            "input": receipt, "bindings": bindings,
            "before_frame": before["frame"], "barrier_frame": barrier["frame"],
            "live_stopped": live_stopped, "old_captures_invalidated": invalidated,
            "settle_ms": settle_ms, "resumed_after_ms": resumed_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
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
                   "input_error_code": exc.code if isinstance(exc, ReloadInputError) else None,
                   "reload_requested": requested, "reload_confirmed": False,
                   "old_captures_invalidated": invalidated, "live_stopped": live_stopped,
                   "sender": "probe" if in_process else "server",
                   "input": receipt, "fresh_capture": None,
                   "next": "No automatic key retry was made. If F10 was already sent, inspect status and use "
                           "efmi_capture_frame for another frame; only retry reload after addressing the reported cause."}
        if phase == "input" and not in_process and isinstance(exc, ReloadInputError) \
                and exc.code in ("keydown_not_observed", "keydown_failed", "focus_refused"):
            failure["hint"] = _OLD_PROBE_HINT
        raise ToolError(json.dumps(failure, ensure_ascii=False)) from exc
