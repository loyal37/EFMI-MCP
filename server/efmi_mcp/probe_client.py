"""Client for the named pipe served by efmi_probe.dll inside the game.

Framing in both directions: [u32 header_len][u32 payload_len][header JSON][payload].
"""
from __future__ import annotations

import json
import os
import struct
import threading
from dataclasses import dataclass
from typing import Any

PIPE_DIR = "\\\\.\\pipe\\"
PIPE_PREFIX = "efmi-probe-"


class ProbeError(RuntimeError):
    """The probe answered with ok=false, or the pipe broke."""


class ProbeNotFound(ProbeError):
    """No game process with the probe loaded is listening."""


@dataclass
class ProbeEndpoint:
    pid: int
    path: str


def find_probes() -> list[ProbeEndpoint]:
    """Lists the pipes of every process that currently has the probe loaded."""
    out: list[ProbeEndpoint] = []
    try:
        names = os.listdir(PIPE_DIR)
    except OSError:
        return out
    for name in names:
        if name.startswith(PIPE_PREFIX):
            tail = name[len(PIPE_PREFIX):]
            if tail.isdigit():
                out.append(ProbeEndpoint(int(tail), PIPE_DIR + name))
    return sorted(out, key=lambda e: e.pid)


class ProbeClient:
    """One connection to one probe. The probe serves a single client at a time."""

    def __init__(self, endpoint: ProbeEndpoint):
        self.endpoint = endpoint
        self._f = None
        self._lock = threading.Lock()
        self._next_id = 1

    # -- connection -----------------------------------------------------
    @classmethod
    def connect(cls, pid: int | None = None) -> "ProbeClient":
        probes = find_probes()
        if pid is not None:
            probes = [p for p in probes if p.pid == pid]
        if not probes:
            raise ProbeNotFound(
                "no running process has efmi_probe.dll loaded"
                + (f" (pid {pid})" if pid is not None else "")
            )
        last_error: Exception | None = None
        # Newest process first: stale pipes of a previous run cannot be opened anyway.
        for ep in reversed(probes):
            client = cls(ep)
            try:
                client.open()
                return client
            except OSError as e:
                last_error = e
        raise ProbeError(f"could not open the probe pipe: {last_error}")

    def open(self) -> None:
        self._f = open(self.endpoint.path, "r+b", buffering=0)

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            finally:
                self._f = None

    @property
    def connected(self) -> bool:
        return self._f is not None

    def __enter__(self) -> "ProbeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- wire -----------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        chunks = []
        while n:
            b = self._f.read(n)
            if not b:
                raise ProbeError("the probe closed the pipe (did the game exit?)")
            chunks.append(b)
            n -= len(b)
        return b"".join(chunks)

    def request(self, cmd: str, **fields: Any) -> tuple[dict, bytes]:
        if self._f is None:
            raise ProbeError("not connected")
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            header = json.dumps({"id": rid, "cmd": cmd, **fields}).encode("utf-8")
            try:
                self._f.write(struct.pack("<II", len(header), 0) + header)
                hlen, plen = struct.unpack("<II", self._read_exact(8))
                reply = json.loads(self._read_exact(hlen).decode("utf-8"))
                payload = self._read_exact(plen) if plen else b""
            except OSError as e:
                self.close()
                raise ProbeError(f"pipe error: {e}") from e
        if not reply.get("ok"):
            raise ProbeError(reply.get("error", "probe reported an error"))
        return reply, payload

    # -- commands -------------------------------------------------------
    def status(self) -> dict:
        return self.request("status")[0]

    def capture(self, detail: str = "full", hash_ib: bool = True, hash_vb: bool = True,
                cb_data: bool = False, hold: bool = True, max_draws: int = 0,
                timeout_ms: int = 5000, rt: dict | None = None,
                contents: dict | None = None) -> tuple[dict, bytes]:
        """Records the next frame. Returns (capture document, binary blob).

        ``rt`` (probe 0.1.5) has the render target copied around the draws that
        match: {"match": [{draw, ib, sn, ic, fi, vs, ps, sh, o}, ...], "max", "before",
        "all", "depth", "box", "budget_mb"}. The copies are listed in the
        document's "rts" and read with read_rt.

        ``contents`` (probe 0.1.6) keeps what calls read and wrote as it was when
        they were issued: {"inputs": ["ib", "vb", "cb", "buf", "tex"] for every
        call, "outputs": ["rt", "depth", "uav"] for the calls "match" selects (also
        by "first"/"last" call numbers) or "all" of them, "events": list the
        writes between the calls, "budget_mb"}. The document's "cont" lists them;
        they are read with read_content while the capture is the latest one.
        """
        fields = {"rt": rt} if rt else {}
        if contents:
            fields["contents"] = contents
        reply, payload = self.request(
            "capture", detail=detail, hash_ib=hash_ib, hash_vb=hash_vb, cb_data=cb_data,
            hold=hold, max_draws=max_draws, timeout_ms=timeout_ms, **fields,
        )
        jlen = reply["json_len"]
        doc = json.loads(payload[:jlen].decode("utf-8"))
        return doc, payload[jlen:]

    def read_buffer(self, capture_frame: int, res: int, offset: int = 0, size: int = 0,
                    timeout_ms: int = 5000) -> tuple[dict, bytes]:
        reply, payload = self.request("read_buffer", capture=capture_frame, res=res, offset=offset,
                                      size=size, timeout_ms=timeout_ms)
        return reply.get("meta", {}), payload

    def read_texture(self, capture_frame: int, res: int, mip: int = 0, slice: int = 0,
                     timeout_ms: int = 5000) -> tuple[dict, bytes]:
        reply, payload = self.request("read_texture", capture=capture_frame, res=res, mip=mip,
                                      slice=slice, timeout_ms=timeout_ms)
        return reply.get("meta", {}), payload

    def read_rt(self, capture_frame: int, snap: int, timeout_ms: int = 5000) -> tuple[dict, bytes]:
        """One render target copy of a capture taken with ``rt``, by its position in "rts"."""
        reply, payload = self.request("read_rt", capture=capture_frame, snap=snap, timeout_ms=timeout_ms)
        return reply.get("meta", {}), payload

    def read_content(self, capture_frame: int, snap: int, offset: int = 0, size: int = 0,
                     timeout_ms: int = 5000) -> tuple[dict, bytes]:
        """What a slot held when its call was issued, by its position in the capture's "cont".

        A buffer: bytes [offset, offset+size), size 0 to the end. A texture: the rows of its
        subresource, like read_texture. meta["exact"] is False only when the probe cannot vouch
        for the bytes (a deferred command list ran after the call).
        """
        reply, payload = self.request("read_content", capture=capture_frame, snap=snap, offset=offset,
                                      size=size, timeout_ms=timeout_ms)
        return reply.get("meta", {}), payload

    def release_content(self, capture_frame: int) -> None:
        """Lets go of the contents a capture keeps; the game's writes are no longer watched for it."""
        self.request("release_content", capture=capture_frame)

    def release_rt(self, capture_frame: int) -> None:
        """Frees the render target copies of a capture in the game; the rest of it stays readable."""
        self.request("release_rt", capture=capture_frame)

    def shader(self, hash_hex: str) -> tuple[dict, bytes]:
        reply, payload = self.request("shader", hash=hash_hex)
        return reply, payload

    def release(self) -> None:
        self.request("release")

    def live(self, enable: bool = True, hash_ib: bool = True, hash_vb: bool = False,
             hash_volatile: bool = False, keep_frames: int = 240, every: int = 1) -> dict:
        """Turns the continuous per-frame stream on or off."""
        return self.request("live", enable=enable, hash_ib=hash_ib, hash_vb=hash_vb,
                            hash_volatile=hash_volatile, keep_frames=keep_frames, every=every)[0]

    def live_get(self, since: int = 0, max_frames: int = 0) -> tuple[int, list[dict]]:
        """Frame summaries newer than `since`. Returns (current frame, summaries)."""
        reply, payload = self.request("live_get", since=since, max_frames=max_frames)
        frames = json.loads(payload.decode("utf-8")) if payload else []
        return reply.get("frame", 0), frames

    def live_summary(self, first: int = 0, last: int = 0, base_first: int = 0, base_last: int = 0,
                     seconds: float = 0, base_seconds: float = 0, timeline: bool = True) -> tuple[int, dict]:
        """A stretch of the stream added up per line (probe 0.1.10). Returns (current frame, summary).

        The stretch is frames first..last (0: the oldest / newest kept) or, when ``seconds`` is set,
        the last ``seconds`` of the stream, with ``base_seconds`` before them as the baseline.
        summary["items"] rows: [type, origin, ib, ib_state, ib_res, ic, fi, vc, vs, ps, frames drawn
        in, first frame, last frame, most draws, most instances, all draws, baseline frames].
        """
        reply, payload = self.request("live_summary", first=first, last=last, base_first=base_first,
                                      base_last=base_last, seconds=seconds, base_seconds=base_seconds,
                                      timeline=timeline)
        summary = json.loads(payload.decode("utf-8")) if payload else {}
        return reply.get("frame", 0), summary

    def trigger_arm(self, ib: list[str] | None = None, shader: list[str] | None = None,
                    new_pairs: bool = False, baseline_frames: int = 60, origin: str = "",
                    min_count: int = 0, delay_frames: int = 0, expire_ms: int = 600000,
                    capture: dict | None = None) -> dict:
        """Arms the trigger (probe 0.1.10): the frame after the one where an index buffer hash or a
        shader hash in the lists, or (new_pairs) a vertex/pixel shader pair not drawn during the
        baseline, is drawn is recorded with the ``capture`` options of capture(). Returns its state."""
        fields: dict[str, Any] = {"new_pairs": new_pairs, "baseline_frames": baseline_frames,
                                  "origin": origin, "min_count": min_count,
                                  "delay_frames": delay_frames, "expire_ms": expire_ms}
        if ib:
            fields["ib"] = list(ib)
        if shader:
            fields["shader"] = list(shader)
        if capture:
            fields["capture"] = capture
        return self.request("trigger_arm", **fields)[0].get("trigger", {})

    def trigger_wait(self, wait_ms: int = 0) -> tuple[dict, dict | None, bytes]:
        """Waits up to wait_ms (at most 120000) while the trigger watches or records. Returns
        (state, capture document or None, blob); the capture comes back once."""
        reply, payload = self.request("trigger_wait", wait_ms=wait_ms)
        state = reply.get("trigger", {})
        if "json_len" not in reply:
            return state, None, b""
        jlen = reply["json_len"]
        return state, json.loads(payload[:jlen].decode("utf-8")), payload[jlen:]

    def trigger_cancel(self) -> dict:
        """Disarms the trigger; a capture it recorded and nobody collected is dropped."""
        return self.request("trigger_cancel")[0].get("trigger", {})

    def reload_key(self, focus: bool = True, restore_focus: bool = True, max_hold_ms: int = 3000) -> dict:
        """Has the probe press XXMI's reload key (F10) inside the game. Returns its receipt."""
        reply, _ = self.request("reload_key", focus=focus, restore_focus=restore_focus, max_hold_ms=max_hold_ms)
        return reply.get("result", {})

    def mods_key(self, focus: bool = True, restore_focus: bool = True, max_hold_ms: int = 3000) -> dict:
        """Has the probe press EFMI's key that switches all mods off or on (F11). Returns its receipt."""
        reply, _ = self.request("mods_key", focus=focus, restore_focus=restore_focus, max_hold_ms=max_hold_ms)
        return reply.get("result", {})
