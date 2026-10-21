"""Switch all mods off or on with the installation's own key, then retain a frame of the result.

EFMI binds F11 to a command list that flips $enable_mods; ZZMI binds F6 to a
cycle of $costume_mods. While the variable is 0 the core stops checking buffers
against the mods' TextureOverride sections, so the game draws its own meshes;
the mods stay loaded and nothing is reloaded. The variable is not persistent: a
reload (F10) or a game restart sets it to 1.

XXMI cannot be asked for the value of a variable. EFMI shows a notification for
1.5 s after each press, and the buffer holding its text is bound to the draw
that prints it, so the frame captured right after the press says which state the
press led to; the two texts are read from the installed core, not assumed here.
ZZMI prints nothing: there the state is read from the draws, which works only
while a mesh that a mod overrides is in view.
"""
from __future__ import annotations

from collections import Counter
import json
import time
from pathlib import Path

from .capture_model import STAGES, Capture
from .mcp_stdio import ToolError
from .mods import ini_sections, plain_key_sections
from .probe_client import ProbeError
from .reload_input import ReloadInputError, send_mods_key_via_probe
from .reload_workflow import _identity
from .xxmi_install import Install, toggle_binding

STATES = {"toggle": None, "on": True, "off": False}
MAX_BUFFERS_READ = 48     # the notification has to be found while it is still on screen
_OLD_PROBE_HINT = ("Probe {version} cannot press {key}, and Windows silently drops keys that this unelevated server "
                   "sends to a game running as administrator. Probe {needed} or later presses it from inside the game: "
                   "restart the game so the updated efmi_probe.dll is loaded.")


def _toggle_binding(install: Install) -> dict:
    """Read only; never assume the key still means 'toggle mods' in a customized install."""
    try:
        return toggle_binding(install)
    except OSError as exc:
        raise ValueError(f"{install.root / 'd3dx.ini'} cannot be read ({exc}). No key was sent.")


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


_MATCH_ARGS = {"match_first_index": "fi", "match_index_count": "ic"}


def _skip_matches(ref, d: dict) -> bool:
    """Does this 'handling = skip' section apply to the draw? Conditions other than first index and
    index count cannot be judged from here, so such a section never counts."""
    for key, value in ref.match.items():
        field = _MATCH_ARGS.get(key)
        try:
            if field is None or int(value.split(";", 1)[0].strip()) != d.get(field, 0):
                return False
        except ValueError:
            return False
    return True


def drawn_summary(session, cap: Capture) -> dict:
    """What the frame shows of the meshes that installed mods replace.

    A game draw counts only when a mod's section for its index buffer skips it
    ('handling = skip', with first index and index count matched): such a draw
    is not issued while mods are on, whereas a section that only swaps textures
    lets the game's draw through either way. XXMI's draws count as a mod's mesh
    only with a vertex shader that the game's own draws use too: a mod draws
    with the game's shaders, XXMI's overlay text with its own.
    """
    by_hash = session.mods.get().by_hash
    origins: Counter = Counter()
    by_game: Counter = Counter()
    game_vs = {d["sh"][0] for d in cap.raw_draws
               if d.get("o") != "xxmi" and d.get("sh") and d["sh"][0] >= 0}
    xxmi_meshes = 0
    for d in cap.raw_draws:
        origin = d.get("o", "unknown")
        origins[origin] += 1
        if "ib" not in d:
            continue
        if origin == "xxmi":
            if d.get("sh") and d["sh"][0] in game_vs:
                xxmi_meshes += 1
            continue
        h = cap.ib_hash(d)
        if h and any(ref.kind == "texture" and ref.skips and _skip_matches(ref, d) for ref in by_hash.get(h, ())):
            by_game[h] += 1
    return {"draws": len(cap), "origins": dict(origins),
            # Meshes a mod replaces are drawn by XXMI while mods are on, by the game while they are off.
            "mesh_draws_by_xxmi": xxmi_meshes,
            "game_draws_of_meshes_that_mods_override": sum(by_game.values()),
            "those_meshes": [{"ib": h, "draws": n} for h, n in by_game.most_common(12)]}


def draws_agree(drawn: dict, enabled: bool, before: dict | None = None) -> bool:
    """Does the frame look the way it does with mods on (off)? Only a clear picture counts.

    Off: the game draws meshes that a mod replaces, and XXMI draws none with
    the game's shaders. On: the game draws none of them, and either XXMI draws
    meshes or ``before`` (the frame right before a key press) had the game
    drawing them, i.e. they are in view and now skipped. With no replaced mesh
    in view the frame says nothing.
    """
    by_xxmi, by_game = drawn["mesh_draws_by_xxmi"], drawn["game_draws_of_meshes_that_mods_override"]
    if not enabled:
        return by_xxmi == 0 and by_game > 0
    return by_game == 0 and (by_xxmi > 0 or bool(before) and draws_agree(before, False))


def _note(install: Install, enabled, presses: int, evidence: dict) -> str:
    key, core = install.toggle.key, install.name
    if not presses:
        return (f"No key was pressed: mods were {'ON' if enabled else 'OFF'} after this server's last {key} "
                f"(frame {evidence['last_notification_frame']}), and the draws of the frame captured now agree with that.")
    pressed = f"{key} was pressed {presses} time(s) inside the game. "
    if enabled is None:
        return (pressed + f"The frame after it does not tell the state ({evidence.get('reason', '')}), so whether mods "
                "are now on or off is unknown here. Compare the draws with an earlier capture, or look at the game. "
                "No further key was sent.")
    if evidence.get("text"):
        said = f"{core} then printed '{evidence['text']}': mods are {'ON' if enabled else 'OFF'}. "
    else:
        said = (f"{core} prints no notification; the draws of the frame after it show mods "
                f"{'ON' if enabled else 'OFF'}. ")
    if enabled:
        return pressed + said + "Meshes that mods override are drawn by XXMI again (origin 'xxmi')."
    return (pressed + said + "The game draws its own meshes (origin 'game'), including the ones mods override; the "
            f"mods stay loaded. They remain off until the next {key}, a reload (F10 switches them on again) or a game "
            "restart: switch them back on when the comparison is done.")


def draws_evidence(drawn: dict, frame: int, before: dict | None = None, before_frame: int | None = None) -> dict:
    """The state as the draws show it, for a core that prints no notification (ZZMI)."""
    for enabled in (True, False):
        if draws_agree(drawn, enabled, before):
            evidence = {"mods_enabled": enabled, "source": "draws", "frame": frame,
                        "mesh_draws_by_xxmi": drawn["mesh_draws_by_xxmi"],
                        "game_draws_of_meshes_that_mods_override": drawn["game_draws_of_meshes_that_mods_override"]}
            if enabled and not drawn["mesh_draws_by_xxmi"]:
                # Mods that only hide meshes draw nothing; the frame before the key had them drawn by the game.
                evidence["before_key"] = {"frame": before_frame,
                                          "game_draws_of_meshes_that_mods_override":
                                              before["game_draws_of_meshes_that_mods_override"]}
            return evidence
    return {"mods_enabled": None, "source": "draws", "frame": frame,
            "reason": "the core prints no notification, and the draws do not show it either: no mesh that an "
                      "installed mod replaces is drawn clearly by XXMI or by the game in this frame"}


def toggle_mods(session, *, state="toggle", cb_data=False, timeout_ms=8000, focus_game=True, restore_focus=True):
    """Press the installation's key (EFMI F11, ZZMI F6) once, or twice when the first press led to the
    state that was not asked for.

    'on' and 'off' press nothing when the state last read after a press by this
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
        # Hold this exact connection through the operation, as the reload does. Connecting first
        # also settles which installation (EFMI, ZZMI) the game uses.
        client = session.probe()
        install = session.install
        bindings = _toggle_binding(install)
        key = install.toggle.key
        texts = _notification_texts(session.efmi_root) if install.toggle.notifies else {}
        before = client.status()
        identity = _identity(before, session.efmi_root)
        if client.endpoint.pid != identity[0]:
            raise ValueError("Connected pipe PID does not match the probe status PID")
        needed = "mods_key" if install.is_efmi else "mods_key_install"
        if needed not in (before.get("features") or ()):
            raise ValueError(_OLD_PROBE_HINT.format(version=before.get("probe", "?"), key=key,
                                                    needed="0.1.3" if install.is_efmi else "0.1.12"))
        if before.get("capture", {}).get("recording") and not before.get("capture", {}).get("live"):
            raise ValueError("A capture is already recording; wait for that operation before pressing the key")

        known = session.mods_display
        # Without a notification the frame before each press is compared with the one after it.
        previous = None
        if want is not None and known and known["enabled"] is want:
            # A manual press since then would show in what is drawn; a frame that
            # shows no modded mesh either way proves nothing and leads to the key.
            phase = "capture"
            doc, blob = client.capture(detail="full", cb_data=cb_data, timeout_ms=timeout_ms)
            now = Capture(doc, blob)
            previous = now
            if now.frame > before["frame"] and len(now) and draws_agree(drawn_summary(session, now), want):
                cap, enabled = now, want
                session.add_capture(cap)
                evidence = {"mods_enabled": want, "source": "no key pressed", "frame": cap.frame,
                            "last_notification_frame": known["frame"], "draws_agree": True}
        if not cap and not install.toggle.notifies and previous is None:
            phase = "capture"
            doc, blob = client.capture(detail="light", timeout_ms=timeout_ms)
            previous = Capture(doc, blob)

        for _ in range(0 if cap else 2):
            phase = "input"
            sent = None      # An unexpected error at the input boundary has unknown delivery.
            receipt = send_mods_key_via_probe(client, focus_game=focus_game, restore_focus=restore_focus, key=key)
            presses.append(receipt)
            sent = bool(receipt.get("key_down_sent"))
            if not sent or not receipt.get("key_up_sent"):
                raise ValueError(f"The probe did not confirm the complete {key} down/up pair")
            if receipt.get("key_down_observed") is not True:
                raise ValueError(f"The probe did not observe {key}'s pressed state")
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
            if install.toggle.notifies:
                evidence = mods_display_evidence(cap, client, texts)
            else:
                evidence = draws_evidence(drawn_summary(session, cap), cap.frame,
                                          drawn_summary(session, previous) if previous else None,
                                          previous.frame if previous else None)
                previous = cap
            enabled = evidence["mods_enabled"]
            if want is None or enabled is None or enabled == want:
                break
        if want is not None and enabled is not None and enabled != want:
            raise ValueError(f"{install.name} still shows mods {'on' if enabled else 'off'} after two presses; no third press was made")

        if presses:
            session.mods_display = None if enabled is None else {"enabled": enabled, "frame": cap.frame}
        vk = install.toggle.vk
        others = [item for name in (vk, vk[3:]) for item in plain_key_sections(session.efmi_root, name)
                  if item["section"].lower() != "keytogglemods"]
        result = {
            "status": "state_unknown" if enabled is None else "ok", "requested": state, "pid": identity[0],
            "installation": install.name, "key": key,
            "mods_enabled": enabled, "presses": len(presses), "state_evidence": evidence,
            "note": _note(install, enabled, len(presses), evidence),
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
            # A person pressing the key sets these off as well; they are the mods' own business.
            result["other_f11_bindings" if install.is_efmi else "other_key_bindings"] = others[:20]
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
                   "next": "No automatic key retry was made. If the key was already sent, mods may have changed state: "
                           "use efmi_capture_frame and compare the draws, or look at the game, before pressing again."}
        raise ToolError(json.dumps(failure, ensure_ascii=False)) from exc
