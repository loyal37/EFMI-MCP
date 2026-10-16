"""Batch texture-slot inspection without exporting shaders or texture payloads."""
from __future__ import annotations

import json
import re

from .capture_model import STAGES
from .mcp_stdio import ToolError


def _hash(value: str, width: int, name: str) -> str:
    value = str(value or "").strip().lower().removeprefix("0x")
    if value and not re.fullmatch(rf"[0-9a-f]{{{width}}}", value):
        raise ToolError(f"{name} must be {width} hex digits")
    return value


def _resource(cap, binding: dict | None) -> dict:
    if binding is None:
        return {"bound": False}
    r = cap.resource(binding["res"]) or {}
    out = {"bound": True, "resource": binding["res"], "hash": binding.get("hash"),
           **{k: r[k] for k in ("kind", "origin", "w", "h", "d", "mips", "array", "fmt_name", "size", "stride") if k in r}}
    if binding.get("view"):
        out["view"] = binding["view"]
    return out


def _texture(declaration: dict, resource: dict) -> bool:
    dimension = declaration.get("dimension") or ""
    if dimension in ("raw", "structured", "buffer") or resource.get("kind") == "buffer":
        return False
    return True


def inspect_slots(session, *, capture="latest", draws=None, ib_hash="", shader="", stage="ps", origin="",
                  include_unused=False, include_buffers=False, offset=0, limit=40, instruction_limit=2) -> dict:
    if stage not in (*STAGES, "all"):
        raise ToolError("stage must be vs, hs, ds, gs, ps, cs or all")
    if origin not in ("", "game", "xxmi", "unknown"):
        raise ToolError("origin must be game, xxmi, unknown or empty")
    ib_hash, shader = _hash(ib_hash, 8, "ib_hash"), _hash(shader, 16, "shader")
    offset, limit, instruction_limit = int(offset), int(limit), int(instruction_limit)
    if offset < 0 or not 1 <= limit <= 200 or not 0 <= instruction_limit <= 8:
        raise ToolError("offset must be nonnegative, limit 1..200 and instruction_limit 0..8")
    cap = session.capture(capture)
    if cap.detail != "full":
        raise ToolError("texture slots need a full capture; call efmi_capture_frame detail='full'")
    if draws is not None and (not isinstance(draws, list) or
                              any(type(i) is not int or not 0 <= i < len(cap) for i in draws)):
        raise ToolError(f"draws must contain draw numbers in 0..{len(cap) - 1}")
    selected = []
    matched = 0
    for i in sorted(set(draws)) if draws else range(len(cap)):
        d = cap.draw(i)
        ib = d.get("ib") or {}
        if ib_hash and ib_hash not in (ib.get("hash"), ib.get("resource_hash")):
            continue
        shaders = d["shaders"].values() if stage == "all" else [d["shaders"].get(stage)]
        if (shader and shader not in shaders) or (origin and d["origin"] != origin):
            continue
        if offset <= matched < offset + limit:
            selected.append(d)
        matched += 1

    analyses: dict[str, dict] = {}
    usages: dict[str, dict | None] = {}
    groups: dict[tuple, dict] = {}
    for d in selected:
        stages = [stage] if stage != "all" else [st for st in STAGES if st in d["shaders"] or
                  (include_unused and any(k.startswith(st + "-t") for k in d.get("tex", {})))]
        for st in stages:
            h = d["shaders"].get(st)
            if h and h not in analyses:
                try:
                    usage = session.shader_listing(h).usage
                except (ToolError, OSError, ValueError) as exc:
                    analyses[h] = {"error": str(exc), "resources": {}}
                    usages[h] = None
                else:
                    usages[h] = usage["resources"]
                    resources = {}
                    for slot, item in sorted(usage["resources"].items()):
                        if not include_buffers and not _texture(item, {}):
                            continue
                        refs = item["instructions"]
                        if not refs and not include_unused:
                            continue
                        resources[f"t{slot}"] = {"dimension": item["dimension"],
                                                 "access": sorted({r["access"] for r in refs}),
                                                 "instruction_count": len(refs),
                                                 "instructions": refs[:instruction_limit]}
                    analyses[h] = {"profile": usage["profile"], "resources": resources}
            usage = usages.get(h)
            bound = {int(k.split("-t")[1]): v for k, v in d.get("tex", {}).items() if k.startswith(st + "-t")}
            slots = []
            for slot in sorted(set(bound) | set(usage or {})):
                item = (usage or {}).get(slot, {})
                r = _resource(cap, bound.get(slot))
                if not include_buffers and not _texture(item, r):
                    continue
                reference = ("no_shader" if st not in d["shaders"] else "unknown" if usage is None else
                             "referenced" if item.get("instructions") else
                             "declared_only" if item else "bound_only")
                if reference in ("no_shader", "declared_only", "bound_only") and not include_unused:
                    continue
                slots.append({"slot": f"{st}-t{slot}", "reference": reference, **r})
            outputs = {k: _resource(cap, v) for k, v in d.get("out", {}).items()}
            key = (st, h, d["origin"], json.dumps(slots, sort_keys=True), json.dumps(outputs, sort_keys=True))
            if key not in groups:
                groups[key] = {"stage": st, "shader": h, "origin": d["origin"], "draws": [], "meshes": [],
                               "textures": slots, "outputs": outputs}
            group = groups[key]
            group["draws"].append(d["index"])
            mesh = {"call": d["call"], "ib_hash": (d.get("ib") or {}).get("hash"), "args": d["args"]}
            if mesh not in group["meshes"]:
                group["meshes"].append(mesh)
    return {"capture": cap.frame, "matching_draws": matched, "returned_draws": len(selected),
            "next_offset": offset + len(selected) if offset + len(selected) < matched else None,
            "groups": list(groups.values()), "shaders": analyses,
            "note": "References are static shader instructions, including conditional branches, not proof of per-pixel use. "
                    "Material roles are not inferred from slot numbers. No files or texture payloads were written/read."}


def register(server, session, capture_prop):
    @server.tool("efmi_get_texture_slots",
                 "Batch texture-slot lookup for captured draws; no temporary scripts, shader files or texture exports. "
                 "Returns referenced slots, bound texture hashes/dimensions/formats/views, sample/load/query instructions "
                 "and output targets. Groups identical material bindings across draws; each unique shader is read and "
                 "disassembled once, cached in memory for this connection. Select draws, an IB hash or a shader; use "
                 "stage='all' for all stages. include_unused exposes stale bindings/declaration-only slots separately. "
                 "Static references include conditional paths (including mod fallbacks); diffuse/normal roles must be "
                 "judged from the instructions. Does not capture a new frame or change game state. Needs a full capture.",
                 {**capture_prop,
                  "draws": {"type": "array", "items": {"type": "integer"}, "default": [],
                            "description": "Draw numbers; empty selects all draws before applying filters and paging."},
                  "ib_hash": {"type": "string", "default": "", "description": "IB region or whole-resource hash (8 hex digits)."},
                  "shader": {"type": "string", "default": "", "description": "Shader hash in the selected stage(s), 16 hex digits."},
                  "stage": {"type": "string", "enum": [*STAGES, "all"], "default": "ps"},
                  "origin": {"type": "string", "enum": ["", "game", "xxmi", "unknown"], "default": ""},
                  "include_unused": {"type": "boolean", "default": False},
                  "include_buffers": {"type": "boolean", "default": False,
                                      "description": "Also include raw/structured/typed buffers bound to t-slots."},
                  "offset": {"type": "integer", "minimum": 0, "default": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 40},
                  "instruction_limit": {"type": "integer", "minimum": 0, "maximum": 8, "default": 2,
                                        "description": "Evidence lines per slot; use efmi_get_shader to inspect more context."}})
    def efmi_get_texture_slots(**kwargs):
        return inspect_slots(session, **kwargs)
