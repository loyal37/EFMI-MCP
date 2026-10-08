"""The bytes behind a buffer slot of a draw: which part of which buffer, and where its contents come from.

Three sources, best first:

contents   the capture was taken with contents (probe 0.1.6): the probe kept what the buffer held when the
           call was issued, and hands back exactly that for as long as the capture is the latest one.
snapshot   a constant buffer window copied into the capture itself (cb_data=true).
late read  the buffer as it is when asked. The probe says whether anything wrote to that range since the
           capture; when it cannot say so, the bytes may be newer than the draw.
"""
from __future__ import annotations

from . import decode
from .capture_model import Capture
from .hashes import index_region

DYNAMIC = 2          # D3D11_USAGE_DYNAMIC
RAW_VIEW = "raw"


class SlotError(ValueError):
    """The slot does not name a buffer of this draw."""


def _bound(d: dict) -> str:
    names = ["ib"] if "ib" in d else []
    names += [f"vb{n}" for n in d.get("vb", {})]
    names += list(d.get("cb", {}))
    names += list(d.get("tex", {}))
    names += [k for k in d.get("out", {}) if k.startswith("u")]
    return ", ".join(names) or "nothing"


def _view_element(view: dict | None, resource: dict) -> int:
    """Bytes per element of a buffer view: the structure, four for raw views, else the view's format."""
    if view and RAW_VIEW in (view.get("flags") or []):
        return 4
    if resource.get("stride"):
        return resource["stride"]
    if view and view.get("format") in decode.FORMAT_NAMES:
        return decode.element_size(decode.FORMAT_NAMES.index(view["format"])) or 4
    return 4


def buffer_binding(cap: Capture, d: dict, slot: str) -> dict:
    """What a buffer slot of a draw exposes.

    Returns {"slot", "role", "res", "resource", "offset", "size", "element", "content", ...}. offset and size
    name the part of the buffer the binding lets the call read; `exact_range` is False where the call reads
    less than that and which part is only known from other data (a vertex buffer: from the indices).
    """
    slot = slot.strip().lower()
    out: dict = {"slot": slot, "exact_range": True}
    if slot == "ib":
        ib = d.get("ib")
        if not ib:
            raise SlotError("this draw has no index buffer bound")
        a = d["args"]
        width = 4 if ib["format"] == 42 else 2
        off, size = index_region(ib["format"], ib["offset"], a.get("first_index", 0), a.get("index_count", 0))
        out.update(role="ib", res=ib["res"], element=width, offset=off, size=size, binding=ib)
        if not size:
            # An indirect draw: its count is in the arguments it read from a buffer.
            args = (d.get("indirect") or {}).get("args") or {}
            if args.get("index_count"):
                out["offset"], out["size"] = index_region(ib["format"], ib["offset"], args.get("first_index", 0),
                                                          args["index_count"])
            else:
                out.update(offset=ib["offset"], size=None, exact_range=False)
    elif slot.startswith("vb") and slot[2:].isdigit():
        vb = d.get("vb", {}).get(int(slot[2:]))
        if not vb:
            raise SlotError(f"nothing bound to {slot}; bound: {_bound(d)}")
        out.update(role="vb", res=vb["res"], element=vb["stride"] or 1, offset=vb["offset"], size=None,
                   exact_range=False, binding=vb)
    elif "-cb" in slot:
        cb = d.get("cb", {}).get(slot)
        if not cb:
            raise SlotError(f"nothing bound to {slot}; bound: {_bound(d)}")
        out.update(role="cb", res=cb["res"], element=16, offset=cb["first_constant"] * 16,
                   size=cb["num_constants"] * 16 or None, binding=cb)
    elif "-t" in slot or (slot.startswith("u") and slot[1:].isdigit()):
        entry = d.get("tex", {}).get(slot) if "-t" in slot else d.get("out", {}).get(slot)
        if not entry:
            raise SlotError(f"nothing bound to {slot}; bound: {_bound(d)}")
        resource = cap.resource(entry["res"]) or {}
        if resource.get("kind") != "buffer":
            raise SlotError(f"{slot} holds a {resource.get('kind', 'resource')}, not a buffer: use efmi_read_texture")
        view = entry.get("view")
        element = _view_element(view, resource)
        out.update(role="uav" if slot.startswith("u") else "srv", res=entry["res"], element=element, binding=entry,
                   view=view)
        if view and "first_element" in view:
            out.update(offset=view["first_element"] * element, size=view["num_elements"] * element)
        else:
            out.update(offset=0, size=None)       # the view was not recorded (a probe older than 0.1.5)
    else:
        raise SlotError("slot must be 'ib', 'vbN', a constant buffer like 'vs-cb0', a buffer bound as a shader "
                        "resource like 'vs-t0', or an unordered access view like 'u0'")
    resource = cap.resource(out["res"]) or {}
    total = resource.get("size", 0)
    out["resource"] = resource
    out["offset"] = min(out["offset"], total)
    out["size"] = total - out["offset"] if out["size"] is None else max(0, min(out["size"], total - out["offset"]))
    out["content"] = out["binding"].get("content")
    return out


def read_slot(session, cap: Capture, binding: dict, offset: int | None = None, size: int | None = None,
              whole: bool = False) -> tuple[bytes, dict]:
    """Bytes of a buffer slot and where they come from.

    Without offset/size: the part the binding exposes, or the whole buffer with whole=True. The second value
    says how far the bytes can be trusted to be those of the call: {"source", "exact", "note"}.
    """
    total = binding["resource"].get("size", 0)
    if whole:
        offset, size = 0, total
    elif offset is None:
        offset, size = binding["offset"], binding["size"]
    size = max(0, min(size, total - offset))
    info: dict = {"offset": offset, "size": size}
    if not size:
        return b"", {**info, "source": "empty", "exact": True, "note": "the binding exposes no bytes"}

    if binding.get("content") is not None:
        try:
            meta, data = session.call(lambda c: c.read_content(cap.frame, binding["content"], offset, size))
        except Exception as exc:          # the copy was lost (budget) or the capture is gone: fall through, saying so
            info["contents_unavailable"] = str(exc)
        else:
            exact = bool(meta.get("exact", True))
            when = "after the call" if binding["role"] == "uav" else "when the call was issued"
            return data, {**info, "source": "contents", "exact": exact,
                          "note": f"as it was {when}" + ("" if exact else f" ({meta.get('why', 'not certain')})")}

    if binding["role"] == "cb":
        snap = cap.snap(binding["binding"].get("snap", -1))
        if snap and snap["data"] is not None and snap["offset"] <= offset and offset + size <= snap["offset"] + snap["size"]:
            start = offset - snap["offset"]
            return snap["data"][start:start + size], {**info, "source": "snapshot", "exact": True,
                                                      "note": "as bound for this draw (from the capture)"}

    meta, data = session.call(lambda c: c.read_buffer(cap.frame, binding["res"], offset, size))
    unchanged = meta.get("unchanged_since_capture")
    note = (f"read at frame {meta.get('read_frame')}, not at the call; "
            + ("nothing has written to these bytes since the capture" if unchanged is True else
               "these bytes have been WRITTEN TO since the capture" if unchanged is False else
               "it is not known whether they changed since the capture"))
    if binding["role"] == "cb" and binding["binding"].get("snap", -1) >= 0:
        note += "; the capture asked for this window (cb_data) but the probe could not keep it"
    elif unchanged is not True:
        note += "; capture with contents to get them as they were at the call"
    if "contents_unavailable" in info:
        note += f"; the contents the capture kept could not be used: {info['contents_unavailable']}"
    return data, {**info, "source": "late read", "exact": unchanged is True, "note": note,
                  "read_frame": meta.get("read_frame"), "unchanged_since_capture": unchanged}
