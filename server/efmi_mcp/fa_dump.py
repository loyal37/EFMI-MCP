"""Reader for XXMI / 3DMigoto frame analysis folders (FrameAnalysis-YYYY-MM-DD-HHMMSS).

Only what the dump itself states is used: `log.txt` for the call sequence and
the dumped file names for hashes. Nothing here depends on the probe.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DRAW_CALLS = {
    "Draw": 1,
    "DrawIndexed": 2,
    "DrawInstanced": 3,
    "DrawIndexedInstanced": 4,
    "DrawInstancedIndirect": 5,
    "DrawIndexedInstancedIndirect": 6,
    "DrawAuto": 7,
    "Dispatch": 8,
    "DispatchIndirect": 9,
}
DRAW_NAMES = {v: k for k, v in DRAW_CALLS.items()}

_RE_CALL = re.compile(r"^(\d{6}) (\w+)\((.*)\)(?: hash=([0-9a-f]+))?\s*$")
_RE_DUMP = re.compile(r"^(\d{6}) 3DMigoto Dumping (Buffer|Texture2D|Texture1D|Texture3D) (.*?) -> (.*)$")
# 000012-ib=5ede2273(9a09f1f0)-vs=b960839f9608d37c-ps=7b067f2b06883a0c.buf
_RE_FILE = re.compile(
    r"^(?P<draw>\d{6})-(?P<slot>[a-z]+(?:-[a-z]+)?\d*)=(?P<hash>[0-9a-f]{8})"
    r"(?:\((?P<full>[0-9a-f]{8})\))?(?P<shaders>(?:-[a-z]{2}=[0-9a-f]{16})*)\.(?P<ext>\w+)$"
)
_RE_SHADER = re.compile(r"-([a-z]{2})=([0-9a-f]{16})")


@dataclass
class DumpSlot:
    slot: str                   # "ib", "vb0", "ps-t3", "vs-cb1", "o0", "oD", "u0" ...
    hash: str                   # the hash XXMI put first in the file name
    full_hash: str | None = None  # resource hash in parentheses (region-hashed buffers only)
    files: list[str] = field(default_factory=list)
    deduped: dict[str, str] = field(default_factory=dict)  # extension -> deduped file name


@dataclass
class DumpDraw:
    number: int
    call: str | None = None
    args: dict[str, int] = field(default_factory=dict)
    shaders: dict[str, str] = field(default_factory=dict)   # "vs" -> hash
    slots: dict[str, DumpSlot] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def type(self) -> int:
        return DRAW_CALLS.get(self.call or "", 0)


@dataclass
class FrameDump:
    path: Path
    analyse_options: str | None
    draws: dict[int, DumpDraw]
    file_count: int
    has_log: bool

    def draw_numbers(self) -> list[int]:
        return sorted(self.draws)


def _parse_args(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in text.split(", "):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            out[k] = int(v, 0)
        except ValueError:
            pass
    return out


def parse_frame_dump(path: str | os.PathLike, keep_log: bool = False) -> FrameDump:
    root = Path(path)
    draws: dict[int, DumpDraw] = {}

    def draw(n: int) -> DumpDraw:
        d = draws.get(n)
        if d is None:
            d = draws[n] = DumpDraw(n)
        return d

    analyse_options = None
    log_path = root / "log.txt"
    has_log = log_path.exists()
    if has_log:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if analyse_options is None and line.startswith("analyse_options:"):
                    analyse_options = line.split(":", 1)[1].strip()
                    continue
                m = _RE_DUMP.match(line)
                if m:
                    n = int(m.group(1))
                    src = os.path.basename(m.group(3))
                    dst = os.path.basename(m.group(4))
                    fm = _RE_FILE.match(src)
                    if fm:
                        slot = draw(n).slots.setdefault(fm["slot"], DumpSlot(fm["slot"], fm["hash"], fm["full"]))
                        slot.deduped[fm["ext"]] = dst
                    continue
                m = _RE_CALL.match(line)
                if m:
                    n = int(m.group(1))
                    name = m.group(2)
                    d = draw(n)
                    if name in DRAW_CALLS and d.call is None:
                        d.call = name
                        d.args = _parse_args(m.group(3))
                    if keep_log:
                        d.log.append(line[7:])
                elif keep_log and line[:6].isdigit():
                    draw(int(line[:6])).log.append(line[7:])

    file_count = 0
    for name in os.listdir(root):
        fm = _RE_FILE.match(name)
        if not fm:
            continue
        file_count += 1
        d = draw(int(fm["draw"]))
        slot = d.slots.setdefault(fm["slot"], DumpSlot(fm["slot"], fm["hash"], fm["full"]))
        slot.files.append(name)
        for stage, h in _RE_SHADER.findall(fm["shaders"]):
            d.shaders.setdefault(stage, h)

    return FrameDump(root, analyse_options, draws, file_count, has_log)


def find_frame_dumps(folder: str | os.PathLike) -> list[Path]:
    """Frame analysis folders directly inside `folder`, oldest first."""
    root = Path(folder)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("FrameAnalysis-"))
