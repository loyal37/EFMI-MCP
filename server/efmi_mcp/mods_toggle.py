"""Switch all mods off or on with EFMI's own key, then retain a frame of the result.

EFMI binds F11 to a command list that flips $enable_mods. While it is 0 the
core stops checking index buffers against the mods' TextureOverride sections,
so the game draws its own meshes; the mods stay loaded and nothing is reloaded.
The variable is not persistent: a reload (F10) or a game restart sets it to 1.

XXMI cannot be asked for the value of a variable. What EFMI does show is a
notification for 1.5 s after each press, and the buffer holding its text is
bound to the draw that prints it. The frame captured right after the press
therefore says which state the press led to. The two texts are read from the
installed core, not assumed here.
"""
from __future__ import annotations

from collections import Counter
import json
import time
from pathlib import Path

from .capture_model import STAGES, Capture
from .mcp_stdio import ToolError
from .mods import ini_sections, is_plain_key, key_tokens, plain_key_sections
from .probe_client import ProbeError
from .reload_input import ReloadInputError, send_mods_key_via_probe
from .reload_workflow import _identity

KEY = "vk_f11"
STATES = {"toggle": None, "on": True, "off": False}
MAX_BUFFERS_READ = 48     # the notification has to be found while it is still on screen
_OLD_PROBE_HINT = ("Probe {version} cannot press F11, and Windows silently drops keys that this unelevated server "
                   "sends to a game running as administrator. Probe 0.1.3 or later presses it from inside the game: "
                   "restart the game so the updated efmi_probe.dll is loaded.")


def _toggle_binding(root: Path) -> dict:
    """Read only; never assume F11 still means 'toggle mods' in a customized install."""
    path = root / "Core" / "EFMI" / "KeyBindings.ini"
    try:
        found = [entries for name, entries in ini_sections(path) if name.lower() == "keytogglemods"]
    except OSError:
        found = []
    keys = [key_tokens(value) for entries in found for key, value in entries if key == "key"]
    runs = [value.strip().lower() for entries in found for key, value in entries if key == "run"]
    if len(found) != 1 or keys != [["no_modifiers", KEY]] or runs != ["commandlisttogglemods"]:
        raise ValueError(f"{path}: expected one [KeyToggleMods] with 'Key = no_modifiers VK_F11' and "
                         "'run = CommandListToggleMods'. No key was sent; custom bindings are not changed automatically.")
    try:
        main = ini_sections(root / "d3dx.ini")
    except OSError as exc:
        raise ValueError(f"{root / 'd3dx.ini'} cannot be read ({exc}). No key was sent.")
    for name, entries in main:
        if name.lower() != "hunting":
            continue
        for key, value in entries:
            if is_plain_key(key_tokens(value), KEY):
                raise ValueError(f"{root / 'd3dx.ini'}: [Hunting] {key} is bound to F11 without a modifier, so F11 "
                                 "would trigger it as well. No key was sent.")
    return {"KeyToggleMods": "no_modifiers VK_F11"}


def _notification_texts(root: Path) -> dict[bytes, bool]:
    """The texts EFMI prints after the key, as the bytes of their buffers; empty if the core has no such pair."""
    names = {"resourcemodsdisplayenabled": True, "resourcemodsdisplaydisabled": False}
    texts: dict[bytes, bool] = {}
    try:
        sections = ini_sections(root / "Core" / "EFMI" / "main.ini")
    except OSError:
        return {}
    for name, entries in sections:
        if name.lower() not in names:
            continue
        for key, value in entries:
            if key == "data" and len(value) >= 2 and value[0] == value[-1] == '"':
                texts[value[1:-1].encode("utf-8")] = names[name.lower()]
    return texts if sorted(texts.values()) == [False, True] else {}


def mods_display_evidence(cap: Capture, client, texts: dict[bytes, bool]) -> dict:
    """Which of EFMI's two notification texts this frame prints.

    Only small buffers bound as shader inputs are read. Finding neither text
    means the state is unknown, not that nothing changed.
    """
    if not texts:
        return {"mods_enabled": None, "reason": "The installed EFMI core does not define the pair of notification "
                                                "texts (ResourceModsDisplayEnabled / ...Disabled) to recognise."}
    limit = max(map(len, texts)) + 64
    # XXMI's own draws first: the text is theirs. The others are looked at too,
    # because which draws count as XXMI's is a heuristic.
    candidates: dict[int, tuple[int, str]] = {}
    for by_xxmi in (True, False):
        for d in cap.raw_draws:
            if (d.get("o") == "xxmi") != by_xxmi:
                continue
            for stage, slot, rid in d.get("srv", []):
                r = cap.resource(rid)
                if r and r.get("kind") == "buffer" and 0 < r.get("size", 0) <= limit:
                    candidates.setdefault(rid, (d["i"], f"{STAGES[stage]}-t{slot}"))
    candidates = dict(list(candidates.items())[:MAX_BUFFERS_READ])
    found = []
    for rid, (draw, slot) in candidates.items():
        try:
            _meta, data = client.read_buffer(cap.frame, rid, 0, cap.resource(rid)["size"])
        except ProbeError:
            continue
        text = bytes(data).split(b"\0", 1)[0]
        if text in texts:
            found.append({"mods_enabled": texts[text], "text": text.decode("utf-8", "replace"),
                          "frame": cap.frame, "draw": draw, "slot": slot})
    if len({item["mods_enabled"] for item in found}) == 1:
        return found[0]
    if found:
        return {"mods_enabled": None, "reason": "The frame prints both notification texts."}
    return {"mods_enabled": None, "buffers_read": len(candidates),
            "reason": "The frame after the key press does not print EFMI's 'mods display' notification."}


def drawn_summary(session, cap: Capture) -> dict:
    """What the frame shows of the meshes that installed mods refer to."""
    by_hash = session.mods.get().by_hash
    origins: Counter = Counter()
    by_game: Counter = Counter()
    xxmi_meshes = 0
    for d in cap.raw_draws:
        origin = d.get("o", "unknown")
        origins[origin] += 1
        if "ib" not in d:
            continue
        if origin == "xxmi":
            xxmi_meshes += 1
            continue
        region = cap.region(d["ib"][3])
        if region and region["hash"] in by_hash:
            by_game[region["hash"]] += 1
    return {"draws": len(cap), "origins": dict(origins),
            # Meshes a mod overrides are drawn by XXMI while mods are on, by the game while they are off.
            "mesh_draws_by_xxmi": xxmi_meshes,
            "game_draws_of_meshes_that_mods_override": sum(by_game.values()),
            "those_meshes": [{"ib": h, "draws": n} for h, n in by_game.most_common(12)]}


def draws_agree(drawn: dict, enabled: bool) -> bool:
    """Does the frame look the way it does with mods on (off)? Only a clear picture counts.

    On: XXMI draws meshes and the game draws none that a mod overrides. Off:
    the reverse. With no modded mesh in view the frame says nothing.
    """
    by_xxmi, by_game = drawn["mesh_draws_by_xxmi"], drawn["game_draws_of_meshes_that_mods_override"]
    return (by_xxmi > 0 and by_game == 0) if enabled else (by_xxmi == 0 and by_game > 0)


def _note(state: str, enabled, presses: int, evidence: dict) -> str:
    if not presses:
        return (f"No key was pressed: EFMI reported mods {'ON' if enabled else 'OFF'} after this server's last F11 "
                f"(frame {evidence['last_notification_frame']}), and the draws of the frame captured now agree with that.")
    pressed = f"F11 was pressed {presses} time(s) inside the game. "
    if enabled is None:
        return (pressed + "EFMI's notification could not be read from the frame after it (" + evidence.get("reason", "")
                + ") so whether mods are now on or off is unknown here. Compare the draws with an earlier capture, "
                "or look at the game. No further key was sent.")
    said = f"EFMI then printed '{evidence['text']}': mods are {'ON' if enabled else 'OFF'}. "
    if enabled:
        return pressed + said + "Meshes that mods override are drawn by XXMI again (origin 'xxmi')."
    return (pressed + said + "The game draws its own meshes (origin 'game'), including the ones mods override; the "
            "mods stay loaded. They remain off until the next F11, a reload (F10 switches them on again) or a game "
            "restart: switch them back on when the comparison is done.")


def toggle_mods(session, *, state="toggle", cb_data=False, timeout_ms=8000, focus_game=True, restore_focus=True):
    """Press F11 once, or twice when the first press led to the state that was not asked for.

    'on' and 'off' press nothing when EFMI's last notification, as read by this
    server, already said so and the frame captured now agrees: without that a
    request for the state mods are in would flip them away and back.
    A failed press is never repeated. Every frame captured here stays in the
    session; the last one is 'latest'. Earlier captures are kept: no resource
    is destroyed by this switch.
    """
    phase = "preflight"
    started = time.monotonic()
    presses: list[dict] = []
    sent = False
    enabled = None
    evidence: dict = {}
    cap = None
    try:
        if state not in STATES or not isinstance(state, str):
            raise ValueError("state must be 'toggle', 'on' or 'off'")
        if type(cb_data) is not bool or type(focus_game) is not bool or type(restore_focus) is not bool:
            raise ValueError("cb_data, focus_game and restore_focus must be booleans")
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 30000:
            raise ValueError("timeout_ms must be an integer from 1000 to 30000")
        want = STATES[state]
        bindings = _toggle_binding(session.efmi_root)
        texts = _notification_texts(session.efmi_root)
        # Hold this exact connection through the operation, as the reload does.
        client = session.probe()
        before = client.status()
        identity = _identity(before, session.efmi_root)
        if client.endpoint.pid != identity[0]:
            raise ValueError("Connected pipe PID does not match the probe status PID")
        if "mods_key" not in (before.get("features") or ()):
            raise ValueError(_OLD_PROBE_HINT.format(version=before.get("probe", "?")))
        if before.get("capture", {}).get("recording") and not before.get("capture", {}).get("live"):
            raise ValueError("A capture is already recording; wait for that operation before pressing the key")

        known = session.mods_display
        if want is not None and known and known["enabled"] is want:
            # A manual F11 since then would show in what is drawn; a frame that
            # shows no modded mesh either way proves nothing and leads to the key.
            phase = "capture"
            doc, blob = client.capture(detail="full", cb_data=cb_data, timeout_ms=timeout_ms)
            now = Capture(doc, blob)
            if now.frame > before["frame"] and len(now) and draws_agree(drawn_summary(session, now), want):
                cap, enabled = now, want
                session.add_capture(cap)
                evidence = {"mods_enabled": want, "source": "no key pressed", "frame": cap.frame,
                            "last_notification_frame": known["frame"], "draws_agree": True}

        for _ in range(0 if cap else 2):
            phase = "input"
            sent = None      # An unexpected error at the input boundary has unknown delivery.
            receipt = send_mods_key_via_probe(client, focus_game=focus_game, restore_focus=restore_focus)
            presses.append(receipt)
            sent = bool(receipt.get("key_down_sent"))
            if not sent or not receipt.get("key_up_sent"):
                raise ValueError("The probe did not confirm the complete F11 down/up pair")
            if receipt.get("key_down_observed") is not True:
                raise ValueError("The probe did not observe F11's pressed state")
            # What was known about the switch is void until the next frame is read.
            session.mods_display = None
            enabled, evidence = None, {}

            phase = "capture"
            doc, blob = client.capture(detail="full", cb_data=cb_data, timeout_ms=timeout_ms)
            cap = Capture(doc, blob)
            if cap.frame <= max(before["frame"], receipt.get("frame_at_key_up", 0)):
                raise ValueError("Probe returned a stale frame rather than a frame after the key press")
            if not len(cap):
                raise ValueError("The frame after the key press contains no draws")
            session.add_capture(cap)
            evidence = mods_display_evidence(cap, client, texts)
            enabled = evidence["mods_enabled"]
            if want is None or enabled is None or enabled == want:
                break
        if want is not None and enabled is not None and enabled != want:
            raise ValueError(f"EFMI still reports mods {'on' if enabled else 'off'} after two presses; no third press was made")

        if presses:
            session.mods_display = None if enabled is None else {"enabled": enabled, "frame": cap.frame}
        others = [item for item in plain_key_sections(session.efmi_root, KEY) if item["section"].lower() != "keytogglemods"]
        result = {
            "status": "state_unknown" if enabled is None else "ok", "requested": state, "pid": identity[0],
            "mods_enabled": enabled, "presses": len(presses), "state_evidence": evidence,
            "note": _note(state, enabled, len(presses), evidence),
            "bindings": bindings, "input": presses,
            "live": bool(before.get("capture", {}).get("live")),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "capture": {"frame": cap.frame, "detail": cap.detail, "resources": len(cap.resources),
                        "shaders": len(cap.shaders), "cb_data": cb_data,
                        "skipped_draws": cap.stats.get("skipped_draws", 0),
                        "arena_full": bool(cap.stats.get("arena_full"))},
            "drawn": drawn_summary(session, cap),
            "next": "The frame is capture='latest'. efmi_diff_captures a=<a frame captured in the other state> shows "
                    "which meshes changed hands between the game and XXMI; earlier captures keep their draw lists, "
                    "but only the latest capture's buffers and textures can still be read.",
        }
        if others:
            # A person pressing F11 sets these off as well; they are the mods' own business.
            result["other_f11_bindings"] = others[:20]
        return result
    except Exception as exc:
        if isinstance(exc, ReloadInputError):
            presses.append(exc.details)
            sent = exc.details.get("key_down_sent", False)
        if phase == "input" and sent is not False:
            # This press may have reached XXMI: an earlier reading no longer holds.
            enabled, evidence = None, {}
            session.mods_display = None
        elif enabled is not None and cap is not None:
            session.mods_display = {"enabled": enabled, "frame": cap.frame}
        delivered = sum(1 for receipt in presses if receipt.get("key_down_sent"))
        failure = {"status": "failed", "phase": phase, "reason": str(exc), "requested": state,
                   "input_error_code": exc.code if isinstance(exc, ReloadInputError) else None,
                   "key_sent": sent, "presses": delivered, "mods_enabled": enabled,
                   "state_evidence": evidence or None, "input": presses,
                   "next": "No automatic key retry was made. If F11 was already sent, mods may have changed state: "
                           "use efmi_capture_frame and compare the draws, or look at the game, before pressing again."}
        raise ToolError(json.dumps(failure, ensure_ascii=False)) from exc
