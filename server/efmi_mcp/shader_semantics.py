"""Conservative SM4/5 scalar provenance and inspectable texture-use evidence.

This is not a shader decompiler or a proof of runtime branch execution. No slot
number, texture hash, mod name, or texture format determines a material role.
Sample resource swizzles precede destination masks, as specified by Microsoft:
https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sample--sm4---asm-
Unsupported control flow suppresses affected role suggestions; raw references survive.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .shader import resource_usage

CHANNELS = "xyzw"
REG = re.compile(r"^(r\d+|v\d+|o\d+)(?:\.([xyzw]+))?$")
CB = re.compile(r"^cb\d+\[[^]]+\](?:\.([xyzw]+))?$", re.I)
TEX = re.compile(r"^t(\d+)(?:\.([xyzw]+))?$")


@dataclass(frozen=True)
class Value:
    data: frozenset[tuple[int, str]] = frozenset()
    inputs: frozenset[str] = frozenset()
    marks: frozenset[tuple[str, int, tuple[int, ...]]] = frozenset()
    constant: float | None = None
    uncertain: bool = False


EMPTY = Value()


def merge(values):
    values = list(values)
    if not values:
        return EMPTY
    return Value(frozenset().union(*(v.data for v in values)),
                 frozenset().union(*(v.inputs for v in values)),
                 frozenset().union(*(v.marks for v in values)),
                 values[0].constant if all(v.constant == values[0].constant for v in values) else None,
                 any(v.uncertain for v in values))


def operands(text: str) -> list[str]:
    out, start, depth = [], 0, 0
    for i, c in enumerate(text):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "," and not depth:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    return out


def analyze(text: str) -> dict:
    usage = resource_usage(text)
    state: dict[str, Value] = {}
    branches = []
    limitations = set()
    position = set()
    uv_inputs = set()
    loops = []
    unsafe_control = False
    sites = defaultdict(list)
    events = defaultdict(lambda: defaultdict(list))
    outputs = defaultdict(set)
    controls = defaultdict(set)
    normals = set()
    lines = text.splitlines()

    def evidence(slot, kind, numbers):
        numbers = tuple(dict.fromkeys(numbers))
        if numbers and numbers not in events[slot][kind]:
            events[slot][kind].append(numbers)

    def read(arg, component):
        raw = arg.strip()
        negative = raw.startswith("-")
        raw = raw.lstrip("-").strip("|")
        if raw.startswith("l("):
            try:
                vs = [float(x.strip()) for x in raw[2:-1].split(",")]
                v = vs[component if len(vs) == 4 else 0]
                return Value(constant=-v if negative else v)
            except ValueError:
                return EMPTY
        m = REG.fullmatch(raw)
        if m:
            reg, swizzle = m.groups()
            swizzle = swizzle or CHANNELS
            ch = swizzle[component] if len(swizzle) == 4 else swizzle[min(component, len(swizzle) - 1)]
            if reg.startswith("v"):
                return Value(inputs=frozenset({f"{reg}.{ch}"}))
            return state.get(f"{reg}.{ch}", EMPTY)
        if CB.fullmatch(raw):
            return Value(inputs=frozenset({raw}))
        return EMPTY

    def combine_states(a, b):
        return {k: merge([a.get(k, EMPTY), b.get(k, EMPTY)]) for k in a.keys() | b.keys()}

    for number, raw in enumerate(lines, 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        token, _, tail = line.partition(" ")
        op = token.split("(", 1)[0].replace("_indexable", "")
        base = op.removesuffix("_sat")
        if token.startswith("dcl_"):
            if "position" in tail:
                position.update(re.findall(r"\bv\d+\b", tail))
            elif token.startswith("dcl_input_ps"):
                m = re.search(r"\b(v\d+)\.([xyzw]+)", tail)
                if m and len(m[2]) == 2:
                    uv_inputs.add(m[1])
            continue
        args = operands(tail) if tail.strip() else []
        if base.startswith("if_"):
            branches.append((state.copy(), None))
            continue
        if base == "else":
            if branches:
                before, _ = branches[-1]
                branches[-1] = before, state.copy()
                state = before.copy()
            continue
        if base == "endif":
            if branches:
                before, then = branches.pop()
                state = combine_states(state, before if then is None else then)
            continue
        if base == "loop":
            loops.append(state.copy())
            limitations.add("loop dataflow is unresolved; only unaffected local evidence is used")
            continue
        if base == "endloop":
            if loops:
                before = loops.pop()
                for k in state.keys() | before.keys():
                    if state.get(k) != before.get(k):
                        v = merge([state.get(k, EMPTY), before.get(k, EMPTY)])
                        state[k] = Value(v.data, v.inputs, uncertain=True)
            continue
        if base.startswith("break"):
            continue
        if base in {"switch", "case", "default", "endswitch", "call", "callc", "label", "retc"}:
            unsafe_control = True
            limitations.add("loop/switch/call/conditional-return dataflow is not resolved")
            continue
        if base.startswith("discard"):
            for a in args:
                for s, ch in read(a, 0).data:
                    controls[s].add(ch)
                    evidence(s, "discard", [number])
            continue
        if base == "ret":
            if branches or loops:
                unsafe_control = True
                limitations.add("early return inside control flow is not resolved")
                continue
            break
        if not args:
            continue
        dest = REG.fullmatch(args[0])
        if not dest or not dest[1].startswith(("r", "o")):
            continue
        reg, mask = dest.groups()
        mask = mask or CHANNELS
        is_sample = base.startswith(("sample", "gather4")) and not base.startswith(("sample_info", "sample_pos"))
        is_load = base.startswith("ld")
        tex = next((TEX.fullmatch(a) for a in args[1:] if TEX.fullmatch(a)), None)
        if tex and (is_sample or is_load):
            slot, swizzle = int(tex[1]), tex[2] or CHANNELS
            coords = merge(read(args[1], i) for i in range(3 if "cube" in token or "texture3d" in token else 2))
            sites[slot].append({"line": number, "text": line, "op": base, "coordinates": args[1],
                                "coordinate_inputs": sorted(coords.inputs),
                                "coordinate_textures": sorted({s for s, _ in coords.data}),
                                "coordinate_uncertain": coords.uncertain or bool(loops),
                                "direct_coordinates": bool(REG.fullmatch(args[1]) and args[1].startswith("v")),
                                "comparison": base.startswith("sample_c"),
                                "dimension": usage["resources"].get(slot, {}).get("dimension")})
            for ch in mask:
                source_ch = swizzle[CHANNELS.index(ch)] if len(swizzle) == 4 else swizzle[0]
                if base.startswith("gather4"):
                    limitations.add("gather lane/channel semantics are not resolved for role inference")
                value = Value(data=frozenset({(slot, source_ch)}), uncertain=bool(loops) or base.startswith("gather4"))
                state[f"{reg}.{ch}"] = value
                if reg.startswith("o"):
                    outputs[slot].add(f"{reg}.{ch}")
            continue
        if base.startswith(("resinfo", "bufinfo", "sample_info", "sample_pos", "lod")):
            for ch in mask:
                state[f"{reg}.{ch}"] = EMPTY
            continue
        if base in {"imul", "umul", "udiv", "sincos", "swapc"}:
            unsafe_control = True
            limitations.add("multi-destination arithmetic is not resolved")
        supported = {"mov", "movc", "add", "mul", "mad", "div", "min", "max", "dp2", "dp3", "dp4",
                     "sqrt", "rsq", "log", "exp", "frc", "lt", "le", "gt", "ge", "eq", "ne", "and", "or",
                     "xor", "not", "ishl", "ishr", "ushr", "iadd", "imad", "ineg", "imin", "imax", "ine",
                     "ieq", "ige", "ilt", "umad", "umin", "umax", "uge", "ult", "utof", "itof", "ftou", "ftoi",
                     "round_ne", "round_ni", "round_pi", "round_z", "deriv_rtx", "deriv_rty",
                     "ubfe", "ibfe", "bfi", "firstbit_lo", "firstbit_hi", "firstbit_shi", "f16tof32", "f32tof16"}
        if base not in supported:
            limitations.add(f"unsupported arithmetic: {base}")
        result = {}
        all_sources = []
        for ch in mask:
            i = CHANNELS.index(ch)
            src = [read(a, i) for a in args[1:]]
            if base in {"dp2", "dp3", "dp4"}:
                n = int(base[-1])
                src = [read(a, j) for a in args[1:] for j in range(n)]
            value = merge(src)
            all_sources.extend(src)
            marks = frozenset()
            if base in {"mov", "min", "max", "mul"}:
                marks = value.marks
            trusted = not value.uncertain and not loops and base in supported
            if trusted and base == "mad" and len(src) == 3 and src[1].constant == 2 and src[2].constant == -1:
                marks = frozenset(("signed", s, (number,)) for s, _ in src[0].data)
            if base == "dp2" and len(args) == 3 and args[1] == args[2]:
                a, b = read(args[1], 0), read(args[1], 1)
                common = {s for kind, s, _ in a.marks if kind == "signed"} & {s for kind, s, _ in b.marks if kind == "signed"}
                marks = frozenset(("length2", s, tuple(sorted({n for v in (a, b) for k, t, ns in v.marks
                                                             if k == "signed" and t == s for n in ns})) + (number,)) for s in common)
            if base == "add" and len(src) == 2:
                for j in range(2):
                    if src[1-j].constant == 1 and args[j+1].startswith("-"):
                        marks |= frozenset(("complement", s, ns + (number,)) for k, s, ns in src[j].marks if k == "length2")
            if trusted and base == "sqrt":
                for k, s, ns in value.marks:
                    if k == "complement":
                        normals.add(s)
                        evidence(s, "normal_reconstruction", ns + (number,))
            result[f"{reg}.{ch}"] = Value(value.data, value.inputs, marks, uncertain=not trusted)
            if reg.startswith("o"):
                for s, _ in value.data:
                    outputs[s].add(f"{reg}.{ch}")
            # Scalar material channels entering calculations already dependent on a
            # reconstructed normal or comparison-sampled shadow. Evidence, not naming.
            if trusted and base in {"mul", "mad"}:
                for j, v in enumerate(src):
                    # A scalar can control a whole lighting vector. RGB being
                    # multiplied componentwise is not three independent controls.
                    if len(mask) > 1 and any(read(args[j+1], CHANNELS.index(c)).data != v.data for c in mask):
                        continue
                    channels = defaultdict(set)
                    for s, c in v.data:
                        channels[s].add(c)
                    other = merge(src[:j] + src[j+1:])
                    shadow = {s for s, _ in other.data if any(q["comparison"] for q in sites[s])}
                    if not (normals & {s for s, _ in other.data} or shadow):
                        continue
                    for s, cs in channels.items():
                        if len(cs) == 1 and s not in normals and len(channels) <= 2:
                            c = next(iter(cs))
                            evidence(s, f"lighting_channel_{c}", [number])
        # RGB texture values multiplied componentwise by a vector constant buffer
        # are color candidates. A single replicated scalar does not qualify.
        if base in {"mul", "mad"} and len(mask) >= 3 and not loops and not any(v.uncertain for v in all_sources):
            for j, a in enumerate(args[1:3], 1):
                if not CB.fullmatch(a.lstrip("-")):
                    continue
                cb_channels = (CB.fullmatch(a.lstrip("-"))[1] or CHANNELS)
                if len(set(cb_channels)) < 3:
                    continue
                other = args[2 if j == 1 else 1]
                data = merge(read(other, CHANNELS.index(c)) for c in mask).data
                per_slot = defaultdict(set)
                for s, c in data:
                    per_slot[s].add(c)
                if len(per_slot) <= 2:
                    for s, cs in per_slot.items():
                        if {"x", "y", "z"} <= cs:
                            evidence(s, "rgb_vector_tint", [number])
        # Recognize the output scale/bias of the standard linear-to-sRGB curve.
        if base == "mad" and len(mask) >= 3 and len(args) == 4 and not loops and not any(v.uncertain for v in all_sources):
            channels = [CHANNELS.index(c) for c in mask]
            if all(read(args[2], i).constant == 1.055 and read(args[3], i).constant == -0.055 for i in channels):
                per_slot = defaultdict(set)
                for v in all_sources:
                    for s, c in v.data:
                        per_slot[s].add(c)
                for s, cs in per_slot.items():
                    if {"x", "y", "z"} <= cs:
                        evidence(s, "rgb_transfer_curve", [number])
        state.update(result)

    result = {}
    for slot, entry in usage["resources"].items():
        if not entry["instructions"]:
            continue
        sample_sites = sites[slot]
        uv = [s for s in sample_sites if s["dimension"] == "texture2d" and not s["comparison"]
              and not s["coordinate_textures"]
              and not s["coordinate_uncertain"]
              and any(p.split(".")[0] in uv_inputs for p in s["coordinate_inputs"])
              and not any(p.startswith("v") and p.split(".")[0] not in uv_inputs for p in s["coordinate_inputs"])]
        ev = {k: [{"lines": list(ns), "text": [lines[n-1].strip() for n in ns]} for ns in paths[:4]]
              for k, paths in events[slot].items()}
        color = bool(uv and ("rgb_vector_tint" in ev or "rgb_transfer_curve" in ev)
                     and any(x.endswith((".x", ".y", ".z")) for x in outputs[slot]))
        lighting = sorted(k[-1] for k in ev if k.startswith("lighting_channel_"))
        suggestions = []
        if uv and "normal_reconstruction" in ev:
            suggestions.append("normal")
        if color and "normal" not in suggestions:
            suggestions.append("diffuse")
        if uv and len(lighting) >= 2 and not suggestions:
            suggestions.append("lightmap")
        result[slot] = {"dimension": entry["dimension"], "references": entry["instructions"],
                        "samples": sample_sites, "interpolated_2d_sample": bool(uv),
                        "output_components": sorted(outputs[slot]), "discard_channels": sorted(controls[slot]),
                        "lighting_channels": lighting, "evidence": ev,
                        "direct_2d_inputs": sorted({p for site in uv if site["direct_coordinates"] for p in site["coordinate_inputs"]}),
                        "role_candidates": [] if unsafe_control else suggestions}
    return {"profile": usage["profile"], "resources": result, "limitations": sorted(limitations),
            "note": "Static component provenance across if/else alternatives; no runtime branch proof. "
                    "Role candidates describe observed arithmetic, not authored texture names."}
