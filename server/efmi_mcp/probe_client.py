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
                timeout_ms: int = 5000) -> tuple[dict, bytes]:
        """Records the next frame. Returns (capture document, binary blob)."""
        reply, payload = self.request(
            "capture", detail=detail, hash_ib=hash_ib, hash_vb=hash_vb, cb_data=cb_data,
            hold=hold, max_draws=max_draws, timeout_ms=timeout_ms,
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

    def reload_key(self, focus: bool = True, restore_focus: bool = True, max_hold_ms: int = 3000) -> dict:
        """Has the probe press XXMI's reload key (F10) inside the game. Returns its receipt."""
        reply, _ = self.request("reload_key", focus=focus, restore_focus=restore_focus, max_hold_ms=max_hold_ms)
        return reply.get("result", {})
