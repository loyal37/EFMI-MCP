"""Offline bridge from a probe archive to LoyalTools' merged-skeleton extractor.

The archive contains capture.json (the compact probe document), cb-snapshots.bin,
buffers/<resource-id>.buf (whole resources), textures/<resource-id>.dds, and
readback.json.  Optional readback.eligible_draws restricts the draw indices used.
All skeleton grouping/deduplication and workspace writing remain in LoyalTools.
The external package is imported without bpy or writing its bytecode caches.
"""
from __future__ import annotations

import argparse
import builtins
from contextlib import redirect_stdout
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import types

from .capture_model import Capture


# Only a fallback: by default the add-on installed in the user's Blender is used
# (see resolve_loyaltools), so updating the add-on updates every step.
DEFAULT_LOYALTOOLS = Path(r"E:\loyal tools\tools\LoyalTools")


# Two matrices of one bone, as the game computes them for different components, differ by
# rounding at most; different bones that are animated differ by orders of magnitude more.
MERGE_TOLERANCE = 1e-5


class BridgeError(ValueError):
    """The archive cannot faithfully supply the original extraction contract."""


def _load_loyaltools(root: Path):
    root = root.resolve()
    if not (root / "extract" / "dump_workspace_extractor.py").is_file():
        raise BridgeError(f"LoyalTools source root is invalid: {root}")
    # A synthetic package avoids the real __init__, which imports Blender bpy.
    name = "_efmi_loyal_" + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(root)]
        sys.modules[name] = package
    prefix = name + ".efmi_extract.migoto_io."
    return types.SimpleNamespace(
        extract=importlib.import_module(name + ".extract.dump_workspace_extractor"),
        resources=importlib.import_module(prefix + "migoto_model.frame_model.resources"),
        calls=importlib.import_module(prefix + "migoto_model.frame_model.calls"),
        frames=importlib.import_module(prefix + "migoto_model.frame_model.frame_model"),
        descriptors=importlib.import_module(prefix + "migoto_model.filename_descriptors"),
        buffers=importlib.import_module(prefix + "data_model.byte_buffer"),
        profile=importlib.import_module(name + ".common.efmi_merged_skeleton"),
    )


def _read_archive(archive: Path):
    doc = json.loads((archive / "capture.json").read_text(encoding="utf-8"))
    blob = (archive / "cb-snapshots.bin").read_bytes()
    readback_path = archive / "readback.json"
    readback = json.loads(readback_path.read_text(encoding="utf-8")) if readback_path.is_file() else {}
    capture = Capture(doc, blob)
    eligible = readback.get("eligible_draws")
    allowed = None if eligible is None else {int(index) for index in eligible}
    draws = []
    for draw in capture.draws():
        if allowed is not None and draw["index"] not in allowed:
            continue
        if draw["call"] != "DrawIndexedInstanced":
            continue
        if "vs-t0" not in draw.get("tex", {}) or not any(
            slot.startswith("vs-") and cb["num_constants"] == 4096
            for slot, cb in draw.get("cb", {}).items()
        ):
            continue
        draws.append(draw)
    if not draws:
        raise BridgeError("No eligible indexed EFMI draws with an instance CB and vs-t0.")
    return capture, readback, draws


def inspect_archive(archive_dir: str | Path) -> dict:
    """Validate archive metadata and list missing whole-buffer readbacks."""
    archive = Path(archive_dir).resolve()
    capture, readback, draws = _read_archive(archive)
    required = set()
    for draw in draws:
        if "ib" in draw:
            required.add(draw["ib"]["res"])
        required.update(vb["res"] for slot, vb in draw.get("vb", {}).items() if slot in (0, 1, 2))
        required.add(draw["tex"]["vs-t0"]["res"])
    missing = [rid for rid in sorted(required) if not (archive / "buffers" / f"{rid}.buf").is_file()]
    return {
        "archive": str(archive), "frame": capture.frame,
        "eligible_draws": [draw["index"] for draw in draws],
        "required_buffers": sorted(required), "missing_buffers": missing,
        "cb_snapshot_bytes": len(capture.blob), "readback_metadata_present": bool(readback),
    }


def left_out_draws(readback: dict, draw_ids) -> list[int]:
    """Draws an archive left out, because the game rewrites their index data, of the object that issued ``draw_ids``.

    The archive records the per-object constants of every draw it kept and of
    every such draw it left out; an object is the draws that share them.
    """
    left = (readback.get("left_out") or {}).get("rewritten_index_data") or []
    if not left:
        return []
    instance = {int(item["draw"]): item["instance"] for item in readback.get("instances", [])}
    own = {instance[index] for index in draw_ids if index in instance}
    return sorted(int(item["draw"]) for item in left if item["instance"] in own)


def _object_draws(extractor, obj) -> set[int]:
    return {record.shader_call.id for component in obj.components
            for record in extractor.get_component_draw_records(component)}


def _require_complete(extractor, obj, readback: dict, archive: Path) -> None:
    missing = left_out_draws(readback, _object_draws(extractor, obj))
    if missing:
        raise BridgeError(f"Object {obj.id} in {archive.name}: the index data of draw(s) {missing[:8]} is in a "
                          "buffer the game rewrites, which is not supported")


def _build_extractor(archive: Path, native, capture: Capture, draws: list[dict], object_name: str,
                     readback: dict | None = None):
    e, r, b, fd = native.extract, native.resources, native.buffers, native.descriptors
    forced_object_name = object_name
    readback_doc = readback if readback is not None else json.loads((archive / "readback.json").read_text(encoding="utf-8"))
    checked_buffers = {}
    buffer_bytes = {}

    def cached_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and isinstance(path, (str, Path)):
            data = buffer_bytes.get(Path(path))
            if data is not None:
                return io.BytesIO(data)
        return builtins.open(path, mode, *args, **kwargs)

    class CachedNumpy:
        def __getattr__(self, name):
            return getattr(e.numpy, name)

        def fromfile(self, file, dtype=float, count=-1, sep="", offset=0, *, like=None):
            data = buffer_bytes.get(Path(file)) if isinstance(file, (str, Path)) else None
            if data is not None and not sep and like is None and offset >= 0:
                # fromfile returns independent writable storage, unlike frombuffer.
                # It also ignores an incomplete trailing element and returns a
                # short array when the requested count exceeds the file. Leave
                # unusual zero-sized dtypes and out-of-file offsets to NumPy.
                dtype = e.numpy.dtype(dtype)
                if dtype.itemsize > 0 and offset <= len(data):
                    available = (len(data) - offset) // dtype.itemsize
                    length = available if count < 0 else min(count, available)
                    view = memoryview(data)[offset:offset + length * dtype.itemsize]
                    return e.numpy.frombuffer(view, dtype=dtype, count=length).copy()
            return e.numpy.fromfile(file, dtype=dtype, count=count, sep=sep, offset=offset, like=like)

    def native_reader(function, **overrides):
        # Bind the original reader's code to this archive's verified byte cache.
        # No module globals, native algorithms, or source files are patched.
        reader = types.FunctionType(function.__code__, function.__globals__ | overrides,
                                    function.__name__, function.__defaults__, function.__closure__)
        reader.__kwdefaults__ = function.__kwdefaults__
        return reader

    class CachedResource(r.Resource):
        build_numpy_buffer = native_reader(r.Resource.build_numpy_buffer, open=cached_open)

    class ArchiveVertexBuffer(r.VertexBuffer, CachedResource):
        def load_format(self, *args, **kwargs):
            # The capture's real input layout replaces a FrameAnalysis .txt file.
            if self.migoto_format is None:
                raise BridgeError("Missing capture vertex format")

    class ArchiveIndexBuffer(r.IndexBuffer, CachedResource):
        def load_format(self, *args, **kwargs):
            if self.migoto_format is None:
                raise BridgeError("Missing capture index format")

    class SnapshotConstantBuffer(r.ConstantBuffer):
        def build_numpy_buffer(self, migoto_format=None, **kwargs):
            if migoto_format is None:
                raise BridgeError("A raw layout is required for a CB snapshot")
            self.buffer = b.NumpyBuffer(migoto_format.vb_layout)
            self.buffer.import_raw_data(self.snapshot_bytes)
            return self.buffer

    class ArchiveExtractor(e.DumpWorkspaceExtractor):
        def __init__(self, model):
            # Do not require or fabricate log.txt: this model is already evaluated.
            self.dump_folder = str(archive)
            self._dump_path = archive
            self.verbose = False
            self._model = model
            self._draw_records = None
            self._txt_format_cache = {}
            self._texture_dimensions_cache = {}
            self._semantic_remap = e._build_semantic_remap()
            self._bridge_candidates = None
            self._bridge_layout_scores = {}
            self._bridge_submeshes = {}
            # The export releases whole-file snapshots explicitly even if the
            # native model/subclass closures await cyclic garbage collection.
            self._bridge_buffer_bytes = buffer_bytes

        _build_slot_data = native_reader(e.DumpWorkspaceExtractor._build_slot_data, open=cached_open)
        _read_original_index_range = native_reader(
            e.DumpWorkspaceExtractor._read_original_index_range, numpy=CachedNumpy())

        def _load_txt_format(self, resource):
            return getattr(resource, "migoto_format", None)

        def _score_slot_layout(self, fmt, slot_id):
            # Formats belong to this immutable archive model and remain alive
            # for the extractor lifetime; native cleanup works on deep copies.
            key = (id(fmt), slot_id)
            if key not in self._bridge_layout_scores:
                self._bridge_layout_scores[key] = super()._score_slot_layout(fmt, slot_id)
            return self._bridge_layout_scores[key]

        def _build_submesh(self, records, unique_str, warnings):
            key = (unique_str, tuple(record.shader_call.id for record in records))
            cached = self._bridge_submeshes.get(key)
            if cached is None:
                added_warnings, output = [], io.StringIO()
                with redirect_stdout(output):
                    build = super()._build_submesh(records, unique_str, added_warnings)
                cached = (build, added_warnings, output.getvalue())
                self._bridge_submeshes[key] = cached
            build, added_warnings, output = cached
            warnings.extend(added_warnings)
            sys.stdout.write(output)
            # Export assigns texture filenames; keep the cached validated build
            # independent of those mutations and of subsequent consumers.
            return copy.deepcopy(build)

        def get_merged_skeleton_candidates(self, ignore_incomplete_draw_calls=False):
            if self._bridge_candidates is None:
                self._bridge_candidates = super().get_merged_skeleton_candidates(ignore_incomplete_draw_calls)
            return self._bridge_candidates

        def select_merged_skeleton_object(self, candidates, object_name=""):
            # Same keyword as the native static method: the LoD mapper passes it by name.
            name = forced_object_name or object_name
            if name and not any(str(candidate.id) == name for candidate in candidates):
                raise BridgeError(f"Requested object {name!r} is absent")
            return e.DumpWorkspaceExtractor.select_merged_skeleton_object(candidates, name)

    def dxgi(value):
        enum_value = e.DXGI_FORMAT(int(value))
        return e.DXGIFormat(enum_value.name.removeprefix("DXGI_FORMAT_"))

    def usage(draw, slot, hash_value):
        return fd.MigotoBufferDescriptor(
            resource_hash=hash_value, original_hash=None, call_id=draw["index"],
            shader_type=slot.shader_type, slot_type=slot.slot_type, slot_id=slot.slot_id,
            contamination=None, shaders={e.ShaderType(stage): value for stage, value in draw["shaders"].items() if value},
        )

    def whole_buffer(rid):
        if rid in checked_buffers:
            return checked_buffers[rid]
        path = archive / "buffers" / f"{rid}.buf"
        resource = capture.resource(rid)
        if not path.is_file():
            raise BridgeError(f"Missing whole-buffer readback: {path}")
        if resource is None or resource.get("kind") != "buffer":
            raise BridgeError(f"Resource {rid} is not a captured buffer")
        if path.stat().st_size != int(resource["size"]):
            raise BridgeError(f"Readback {rid} is not the complete {resource['size']}-byte buffer")
        expected = readback_doc.get("buffers", {}).get(str(rid), {}).get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise BridgeError(f"Readback {rid} is missing its recorded SHA256")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected.lower():
            raise BridgeError(f"Archived buffer {rid} differs from its recorded SHA256")
        buffer_bytes[path] = data
        checked_buffers[rid] = (resource, path)
        return checked_buffers[rid]

    calls = []
    skeleton_resources = {}
    missing_textures = set()
    geometry_windows = []
    for draw in draws:
        index = draw["index"]
        args = draw["args"]
        if draw.get("topology") != "trianglelist":
            raise BridgeError(f"Draw {index}: only triangle-list geometry is supported")
        base = int(args.get("first_vertex", 0))
        if base < 0:
            raise BridgeError(f"Draw {index}: the original object grouper does not support negative base vertex")
        if args.get("first_instance", 0):
            raise BridgeError(f"Draw {index}: nonzero first instance needs a verified instance-CB adapter")
        storage = r.ResourceStorage()
        bind = draw.get("ib")
        if not bind or not bind.get("hash"):
            raise BridgeError(f"Draw {index}: a known index-region hash is required")
        source, path = whole_buffer(bind["res"])
        topo = e.Topology.TriangleList
        fmt = e.MigotoFormat(byte_offset=int(bind["offset"]), topology=topo,
                             format=dxgi(bind["format"]), first_index=int(args.get("first_index", 0)),
                             index_count=int(args["index_count"]))
        fmt.ib_layout = e.MigotoFormat.make_ib_layout(fmt.format, topo)
        slot = e.ResourceSlot.from_string("ib")
        descriptor = fd.MigotoIndexBufferDescriptor(
            resource_hash=bind["hash"], original_hash=bind.get("resource_hash"),
            data_format=e.DXGI_FORMAT(int(bind["format"])), topology=topo,
            byte_offset=fmt.byte_offset, first_index=fmt.first_index, index_count=fmt.index_count,
        )
        ib = ArchiveIndexBuffer(hash=bind["hash"], pointer=f"{source['ptr']}:{index}:ib",
                                bin_path=path, bin_path_deduped=path, migoto_format=fmt,
                                usage_descriptor=usage(draw, slot, bind["hash"]), data_descriptor=descriptor,
                                topology=topo, format=e.DXGI_FORMAT(int(bind["format"])))
        storage.add(slot, ib)
        index_values = ib.build_numpy_buffer().get_field(b.Semantic.Index)
        min_index, max_index = int(index_values.min()), int(index_values.max())
        addressed_count = max_index - min_index + 1
        geometry_windows.append({"draw": index, "slot": "ib", "resource": bind["res"],
                                 "offset": fmt.get_byte_offset(from_ib_layout=True),
                                 "size": fmt.get_byte_size(from_ib_layout=True),
                                 "min_index": min_index, "max_index": max_index, "base_vertex": base})
        layout = capture.layout(draw["layout"])
        if not layout:
            raise BridgeError(f"Draw {index}: input layout is missing")
        for slot_id, bind in draw.get("vb", {}).items():
            if slot_id not in (0, 1, 2):
                continue
            elements = [item for item in layout if item["slot"] == slot_id]
            if not elements:
                continue
            if any(item["per_instance"] for item in elements):
                raise BridgeError(f"Draw {index}: vertex slot {slot_id} is per-instance")
            region = bind.get("region")
            if not region or region["state"] != "ok" or not region["hash"]:
                raise BridgeError(f"Draw {index}: VB{slot_id} needs a captured vertex region")
            stride = int(bind["stride"])
            if stride <= 0:
                raise BridgeError(f"Draw {index}: VB{slot_id} has an invalid byte window")
            source, path = whole_buffer(bind["res"])
            semantics = [b.BufferSemantic(
                b.AbstractSemantic(b.Semantic(item["semantic"]), item["index"]), dxgi(item["format"]),
                offset=item["offset"], input_slot=slot_id,
            ) for item in elements]
            # XXMI's VB hash region uses an estimated count, not the geometry
            # bounds. Only the actual index values define the exported window.
            # A full resource backs a view of the exact IB-addressed vertex range.
            # Keep original BaseVertexLocation for the upstream grouping contract.
            byte_offset = int(bind["offset"]) + min_index * stride
            byte_end = byte_offset + (base + addressed_count) * stride
            if byte_offset < 0 or byte_end > path.stat().st_size:
                raise BridgeError(f"Draw {index}: actual VB{slot_id} geometry exceeds its buffer")
            record = readback_doc.get("buffers", {}).get(str(bind["res"]), {})
            if record.get("volatile") and not any(start <= byte_offset and byte_end <= start + size
                                                  for start, size in record.get("ranges", [])):
                # Only ranges of a buffer the game rewrites are archived; the rest of the file is zero.
                raise BridgeError(f"Draw {index}: VB{slot_id} reads bytes of a rewritten buffer that were not captured")
            geometry_windows.append({"draw": index, "slot": f"vb{slot_id}", "resource": bind["res"],
                                     "offset": byte_offset + base * stride, "size": addressed_count * stride,
                                     "vertices": addressed_count, "stride": stride,
                                     "xxmi_hash_offset": region["offset"], "xxmi_hash_size": region["size"],
                                     **({"volatile": True} if record.get("volatile") else {})})
            fmt = e.MigotoFormat(byte_offset=byte_offset, topology=topo, stride=stride,
                                 first_vertex=base, vertex_count=addressed_count,
                                 first_instance=0, instance_count=int(args.get("instance_count", 1)),
                                 vb_layout=b.BufferLayout(semantics, stride=stride, auto_stride=False, auto_offsets=False))
            slot = e.ResourceSlot.from_string(f"vb{slot_id}")
            descriptor = fd.MigotoVertexBufferDescriptor(
                resource_hash=bind["hash"], original_hash=bind.get("resource_hash"), slot_id=slot_id,
                layout_hash=None, topology=topo, byte_offset=fmt.byte_offset, stride=stride,
                first_vertex=base, vertex_count=fmt.vertex_count, first_instance=0,
                instance_count=fmt.instance_count,
            )
            vb = ArchiveVertexBuffer(hash=bind["hash"], pointer=f"{source['ptr']}:{index}:vb{slot_id}",
                                     bin_path=path, bin_path_deduped=path, migoto_format=fmt,
                                     usage_descriptor=usage(draw, slot, bind["hash"]), data_descriptor=descriptor)
            storage.add(slot, vb)
        for name, bind in draw.get("cb", {}).items():
            if not name.startswith("vs-"):
                continue
            data = capture.snap_bytes(bind["snap"])
            if data is None:
                raise BridgeError(f"Draw {index}: draw-time snapshot for {name} is unavailable")
            if len(data) != capture.snaps[bind["snap"]][2] or not data or len(data) % 16:
                raise BridgeError(f"Draw {index}: draw-time snapshot for {name} is truncated or unaligned")
            slot = e.ResourceSlot.from_string(name)
            source = capture.resource(bind["res"])
            cb = SnapshotConstantBuffer(hash=bind["hash"] or "00000000", pointer=f"{source['ptr']}:{index}:{name}",
                                        first_constant=0, num_constants=int(bind["num_constants"]))
            # Probe snapshots already start at original first_constant * 16.
            cb.snapshot_bytes = data
            storage.add(slot, cb)
        for name, bind in draw.get("tex", {}).items():
            slot = e.ResourceSlot.from_string(name)
            source = capture.resource(bind["res"])
            if name == "vs-t0":
                source, path = whole_buffer(bind["res"])
                resource = skeleton_resources.get(bind["res"])
                if resource is None:
                    resource = CachedResource(hash=bind["hash"] or "00000000", pointer=source["ptr"],
                                          bin_path=path, bin_path_deduped=path,
                                          usage_descriptor=usage(draw, slot, bind["hash"] or "00000000"))
                    skeleton_resources[bind["res"]] = resource
                storage.add(slot, resource)
            elif name.startswith("ps-") and source and source.get("kind") != "buffer":
                path = archive / "textures" / f"{bind['res']}.dds"
                if not path.is_file():
                    missing_textures.add(bind["res"])
                    continue
                if not bind.get("hash"):
                    continue
                descriptor = fd.MigotoTextureDescriptor(bind["hash"], None, e.DXGI_FORMAT(int(source["fmt"])))
                resource = r.Resource(hash=bind["hash"], pointer=f"{source['ptr']}:{index}:{name}",
                                      bin_path=path, bin_path_deduped=path,
                                      usage_descriptor=usage(draw, slot, bind["hash"]), data_descriptor=descriptor)
                storage.add(slot, resource)
        command = e.DrawIndexedInstanced(bindings=[], raw_command=None,
                                          index_count=int(args["index_count"]), first_index=int(args.get("first_index", 0)),
                                          first_vertex=base, instance_count=int(args.get("instance_count", 1)), first_instance=0)
        shaders = [native.calls.Shader(hash=value, pointer=f"shader:{value}", type=e.ShaderType(stage))
                   for stage, value in draw["shaders"].items() if value]
        calls.append(native.calls.ShaderCall(id=index, shaders=shaders, commands=[],
                                             resources=storage, model_resources=storage, draw_call=command))
    model = native.frames.DumpModel(cfg=native.frames.ParseDumpModelConfig(archive), calls=calls, resources={})
    extractor = ArchiveExtractor(model)
    extractor.geometry_windows = geometry_windows
    return extractor, sorted(missing_textures)


def _static_sample(extractor, selected, native, capture: Capture, readback: dict) -> dict:
    """Describe original-algorithm output without writing a workspace."""
    import numpy as np

    metadata = extractor._build_merged_skeleton_vg_metadata(selected)
    keys = []
    for component in selected.components:
        key, _, _ = extractor.get_component_primary_draw(component)
        keys.append(f"{key[0]}-{key[1]}-{key[2]}")
    global_sources = {}
    for key, item in zip(keys, metadata):
        for local in range(item["vg_count"]):
            global_sources[item["vg_offset"] + local] = (key, local)
    signature, matrices, geometry_payloads, palettes = {}, {}, {}, {}
    # Vertex streams the game rewrites every few frames: a pose, not static geometry.
    volatile = {(window["draw"], window["slot"]) for window in extractor.geometry_windows if window.get("volatile")}
    volatile_slots = []
    weighted_cross_bones = weighted_cross_channels = 0
    used_bones = 0
    geometry_bytes = 0
    geometry_resources = set()
    selected_draws = set()
    semantic = native.buffers.Semantic
    warnings = []
    for key, component, item in zip(keys, selected.components, metadata):
        _, records, _ = extractor.get_component_primary_draw(component)
        selected_draws.update(record.shader_call.id for record in extractor.get_component_draw_records(component))
        build = extractor._build_submesh(records, key, warnings)
        first = records[0]
        original_indices = extractor._read_original_index_range(
            first.ib, first.draw_call.first_index, first.draw_call.index_count)
        ib_bytes = original_indices.tobytes()
        geometry_payloads[(key, "ib")] = ib_bytes
        geometry_bytes += len(ib_bytes)
        entry = {
            "vertex_count": build.vertex_count, "index_count": build.index_count,
            "first_vertex": extractor.get_component_first_vertex(component),
            "index_bytes": len(ib_bytes), "index_sha256": hashlib.sha256(ib_bytes).hexdigest(),
            "vbs": {}, "vg_count": item["vg_count"], "cpu_posed": bool(component.mesh.cpu_posed),
            "canonical_sources": {},
        }
        for slot in build.slot_datas:
            geometry_bytes += len(slot.raw_bytes)
            entry["vbs"][str(slot.slot_id)] = {
                "stride": slot.stride, "bytes": len(slot.raw_bytes),
                "sha256": hashlib.sha256(slot.raw_bytes).hexdigest(),
                "layout": slot.layout.to_string(),
            }
            if any((record.shader_call.id, f"vb{slot.slot_id}") in volatile for record in records):
                # Differs from sample to sample by nature; its size and layout still have to agree.
                entry["vbs"][str(slot.slot_id)].update(sha256=None, volatile=True)
                volatile_slots.append([key, f"vb{slot.slot_id}"])
            else:
                geometry_payloads[(key, f"vb{slot.slot_id}")] = slot.raw_bytes
        if not component.mesh.cpu_posed:
            ids = np.asarray(component.mesh.get_data(semantic.Blendindices), dtype=np.uint32)
            weights = component.mesh.get_data(semantic.Blendweights)
            if weights is None:
                weights = component.mesh.get_data(semantic.Blendweight)
            if weights is None:
                weights = np.zeros_like(ids, dtype=np.float32)
                weights[..., 0] = 1.0
            else:
                weights = np.asarray(weights, dtype=np.float32)
            if weights.shape != ids.shape or not np.isfinite(weights).all():
                raise BridgeError(f"Invalid native weights for {key}")
            used = np.unique(ids[weights != 0])
            palette = extractor._get_component_skeleton_data(component)
            palettes[key] = [np.array(palette[local], dtype=np.float32) for local in range(item["vg_count"])]
            for local_text, global_id in item["vg_map"].items():
                source = global_sources.get(int(global_id))
                if source is None:
                    raise BridgeError(f"Canonical source {global_id} is outside the native private pools")
                entry["canonical_sources"][str(local_text)] = list(source)
            for local_value in used:
                local = int(local_value)
                matrix = np.asarray(palette[local], dtype=np.float32)
                if not np.isfinite(matrix).all() or not np.any(matrix != 0):
                    raise BridgeError(f"Used bone {key}/{local} has a nonfinite or zero matrix")
                matrices[(key, local)] = matrix.copy()
                used_bones += 1
                source_key, _ = entry["canonical_sources"][str(local)]
                if source_key != key:
                    weighted_cross_bones += 1
                    weighted_cross_channels += int(np.count_nonzero((ids == local) & (weights != 0)))
        signature[key] = entry
    for window in extractor.geometry_windows:
        if window["draw"] in selected_draws:
            geometry_resources.add(window["resource"])
    for rid in sorted(geometry_resources):
        record = readback.get("buffers", {}).get(str(rid), {})
        if record.get("volatile"):
            continue    # its ranges were proven identical to draw time when read, and each draw's window is inside them
        if record.get("meta", {}).get("unchanged_since_capture") is not True:
            raise BridgeError(f"Geometry buffer {rid} is not verified unchanged since capture")
    if not weighted_cross_bones or not weighted_cross_channels:
        raise BridgeError("Native mapping has no cross-component merge carrying nonzero vertex weights")
    return {
        "signature": signature, "matrices": matrices, "geometry_payloads": geometry_payloads, "palettes": palettes,
        "frame": capture.frame,
        # Local bones that the native grouping gave to a bone of another component.
        "merged_bones": sum(1 for key, entry in signature.items() for local, source in entry["canonical_sources"].items()
                            if (source[0], source[1]) != (key, int(local))),
        "selected_object": str(selected.id), "components": len(selected.components),
        "geometry_bytes": geometry_bytes, "geometry_buffers": sorted(geometry_resources),
        "volatile_vertex_slots": volatile_slots,
        "used_bones": used_bones, "weighted_cross_component_bones": weighted_cross_bones,
        "weighted_cross_component_channels": weighted_cross_channels,
        "canonical_bones": len({tuple(source) for entry in signature.values()
                                for source in entry["canonical_sources"].values()}),
    }


def _validate_static_samples(samples: list[dict], archives: list[Path]) -> dict:
    if len(samples) < 3 or len(set(archives)) != len(archives):
        raise BridgeError("Deferred-skeleton static export requires at least three distinct archives")
    if len({sample["frame"] for sample in samples}) != len(samples):
        raise BridgeError("Validation archives must represent distinct captured frames")
    import numpy as np

    def static_part(signature):
        return {key: {name: value for name, value in entry.items() if name != "canonical_sources"}
                for key, entry in signature.items()}

    reference = samples[0]
    changed = set()
    summaries = []
    for archive, sample in zip(archives, samples):
        if static_part(sample["signature"]) != static_part(reference["signature"]):
            different = sorted(key for key in set(reference["signature"]) | set(sample["signature"])
                               if static_part(reference["signature"]).get(key) != static_part(sample["signature"]).get(key))
            raise BridgeError(f"Static geometry or layout differ in {archive}: {different}")
        if sample.get("geometry_payloads") != reference.get("geometry_payloads"):
            raise BridgeError(f"Actual complete exported geometry bytes differ in {archive}")
        if set(sample["matrices"]) != set(reference["matrices"]):
            raise BridgeError(f"Used bone identities differ in {archive}")
        # The native grouping merges bones of different components whose matrices
        # are bit for bit equal in its frame. The game's matrices of one bone can
        # differ in the last bit between components, so which bones get merged
        # varies from frame to frame, harmlessly. What must not happen is two
        # different bones that merely coincided in the exported frame: every merge
        # of the first sample has to be the same bone, measured, in every sample.
        for key, entry in reference["signature"].items():
            for local, (source_key, source_local) in entry["canonical_sources"].items():
                if (source_key, source_local) == (key, int(local)):
                    continue
                if source_key not in sample["palettes"]:
                    raise BridgeError(f"{key} bone {local} was merged with a bone of {source_key}, which has no bone data")
                mine, theirs = sample["palettes"][key][int(local)], sample["palettes"][source_key][source_local]
                if not np.allclose(mine, theirs, rtol=MERGE_TOLERANCE, atol=MERGE_TOLERANCE):
                    raise BridgeError(f"{key} bone {local} was merged with {source_key} bone {source_local} in the first "
                                      f"sample, but their matrices differ in {archive}: they are not the same bone")
        this_changed = [key for key, matrix in sample["matrices"].items()
                        if not np.array_equal(matrix, reference["matrices"][key])]
        changed.update(this_changed)
        summary = {key: value for key, value in sample.items()
                   if key not in ("signature", "matrices", "geometry_payloads", "palettes")}
        summary.update(archive=str(archive), changed_used_bone_matrices_vs_first=len(this_changed))
        summaries.append(summary)
    if not changed:
        raise BridgeError("Bone matrices did not change between samples; repeated poses do not validate canonical stability")
    return {
        "scope": "static_geometry_and_canonical_vertex_groups", "samples": summaries,
        "changed_used_bone_matrices": len(changed),
        "canonical_source_comparison": "every bone merge of the exported sample holds in all samples, by matrix within "
                                       f"{MERGE_TOLERANCE:g}; samples may find fewer or more merges (see merged_bones)",
        "merged_bones": [sample["merged_bones"] for sample in samples],
        "geometry_comparison": "Direct byte equality and SHA256 of actual index range and all exported vertex bytes, with unchanged whole geometry buffers",
        # CPU-posed components: the game rewrites these vertex streams every few frames, so the samples
        # hold different poses of them. Size and layout were compared; the workspace gets the first sample's.
        "volatile_vertex_data": reference.get("volatile_vertex_slots", []),
        "pose_reconstruction": False,
        "pose_limitation": "Skeleton pools were read after the captured CBs; these samples do not reconstruct the pose of any specified capture frame.",
    }


def export_merged_workspace(archive_dir: str | Path, workspace_dir: str | Path,
                            loyaltools_root: str | Path = DEFAULT_LOYALTOOLS,
                            object_name: str = "", copy_textures: bool = True,
                            allow_unverified: bool = False,
                            validation_archives: list[str | Path] | None = None) -> dict:
    """Run original LoyalTools grouping, skeleton mapping and workspace export."""
    archive, workspace = Path(archive_dir).resolve(), Path(workspace_dir).resolve()
    loyaltools_root = Path(loyaltools_root).resolve()
    if workspace == loyaltools_root or loyaltools_root in workspace.parents:
        raise BridgeError("Output must not be inside the read-only LoyalTools source tree")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise BridgeError("Output must be a new or empty workspace directory")
    capture, readback, draws = _read_archive(archive)
    archives = [archive] + [Path(path).resolve() for path in (validation_archives or [])]
    multisample = bool(validation_archives)
    collection_verified = readback.get("complete") is True and not readback.get("errors")
    deferred = readback.get("validation_scope") == "deferred_skeleton_static"
    verified = collection_verified and not deferred
    if deferred and not multisample and not allow_unverified:
        raise BridgeError("Deferred skeleton readback requires three-sample static validation")
    if not collection_verified and not allow_unverified:
        raise BridgeError("Archive readback is incomplete or has errors; use allow_unverified only for local debugging")
    if allow_unverified and not multisample:
        debug_root = Path(__file__).resolve().parents[2] / "captures"
        if workspace != debug_root and debug_root not in workspace.parents:
            raise BridgeError("Unverified debug exports must stay inside this project's captures directory")
    log = io.StringIO()
    verified_buffer_caches = []
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with redirect_stdout(log):
            native = _load_loyaltools(loyaltools_root)
            extractor, missing_textures = _build_extractor(archive, native, capture, draws, object_name, readback)
            verified_buffer_caches.append(extractor._bridge_buffer_bytes)
            candidates = extractor.get_merged_skeleton_candidates()
            selected = extractor.select_merged_skeleton_object(candidates)
            _require_complete(extractor, selected, readback, archive)
            candidate_summary = [{"name": str(obj.id), "components": len(obj.components),
                                  "vertices": sum(int(c.mesh.format.vertex_count) for c in obj.components)}
                                 for obj in candidates]
            validation = None
            if multisample:
                samples = []
                for sample_archive in archives:
                    if sample_archive == archive:
                        sc, sr, se, so = capture, readback, extractor, selected
                    else:
                        sc, sr, sd = _read_archive(sample_archive)
                        se, _ = _build_extractor(sample_archive, native, sc, sd, object_name, sr)
                        verified_buffer_caches.append(se._bridge_buffer_bytes)
                        so = se.select_merged_skeleton_object(se.get_merged_skeleton_candidates())
                        _require_complete(se, so, sr, sample_archive)
                    if sr.get("complete") is not True or sr.get("errors") or sr.get("validation_scope") != "deferred_skeleton_static":
                        raise BridgeError(f"Archive {sample_archive} is not a completed deferred_skeleton_static collection")
                    samples.append(_static_sample(se, so, native, sc, sr))
                    if se is not extractor:
                        # Validation retains actual exported slices, not another
                        # full copy of each source pool until the export ends.
                        se._bridge_buffer_bytes.clear()
                validation = _validate_static_samples(samples, archives)
                verified = True
            result = extractor.extract_merged_skeleton(str(workspace), copy_textures=copy_textures)
            profile = native.profile.load_profile(str(workspace))
    finally:
        for cache in verified_buffer_caches:
            cache.clear()
        sys.dont_write_bytecode = previous
    report = {
        "archive": str(archive), "frame": capture.frame, "workspace": str(workspace),
        "archive_verified": verified, "debug_unverified": not verified,
        "loyaltools_source": str(loyaltools_root), "selected_object": str(selected.id),
        "candidates": candidate_summary, "eligible_draws": [draw["index"] for draw in draws],
        "components": profile["component_count"], "bones_count": profile["bones_count"],
        "unique_strs": result.unique_strs, "warnings": result.warnings,
        "upstream_warnings": [line for line in log.getvalue().splitlines()
                              if line.startswith(("WARNING", "Skipped", "Failed to build", "Suppressed"))],
        "geometry_windows": extractor.geometry_windows,
        "missing_texture_resources": missing_textures,
        "timing": "CBs are draw-time snapshots; IB/VB/SRV and DDS are later readbacks. See archive readback.json.",
        "binding_scope": "Capture stores complete bindings, not the original explicit Set* command history.",
        "srv_view_scope": "Probe stores underlying SRV resources; FirstElement is not captured. Bone offsets use the full vs-t0 buffer.",
        "readback_metadata": readback,
        "validation": validation,
    }
    (workspace / "live-bridge-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "live-bridge.log").write_text(log.getvalue(), encoding="utf-8")
    return report


def resolve_loyaltools(configured: str | Path | None = None, addon: Path | None = None) -> tuple[Path, str]:
    """Which LoyalTools to call, and why: (folder, origin).

    A configured folder wins. Otherwise the add-on installed for the Blender in
    use, which is also the copy that imports and exports inside Blender. The
    built-in source-tree path is only tried when neither exists.
    """
    def usable(path):
        return path is not None and (Path(path) / "extract" / "dump_workspace_extractor.py").is_file()

    if configured:
        if not usable(configured):
            raise BridgeError(f"Configured LoyalTools folder is not a LoyalTools tree: {configured}")
        return Path(configured).resolve(), "configured"
    if usable(addon):
        return Path(addon).resolve(), "add-on installed in Blender"
    if usable(DEFAULT_LOYALTOOLS):
        return DEFAULT_LOYALTOOLS.resolve(), "source tree (no installed add-on was found)"
    raise BridgeError("LoyalTools was not found: install the add-on in Blender, or set EFMI_LOYALTOOLS / "
                      "--loyaltools-root to its folder")


def loyaltools_version(loyaltools_root: str | Path = DEFAULT_LOYALTOOLS) -> str:
    """The add-on version declared by a LoyalTools source tree, read without importing bpy."""
    import re
    try:
        text = (Path(loyaltools_root) / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'"version"\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', text)
    return ".".join(match.groups()) if match else ""


def list_candidates(archive_dir: str | Path, loyaltools_root: str | Path = DEFAULT_LOYALTOOLS) -> dict:
    """The character objects LoyalTools recognises in one archived frame.

    The names are the ones its extractor selects by; the largest is its default.
    """
    archive = Path(archive_dir).resolve()
    capture, readback, draws = _read_archive(archive)
    log = io.StringIO()
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    extractor = None
    try:
        with redirect_stdout(log):
            native = _load_loyaltools(Path(loyaltools_root).resolve())
            extractor, missing_textures = _build_extractor(archive, native, capture, draws, "", readback)
            candidates = extractor.get_merged_skeleton_candidates()
            default = str(extractor.select_merged_skeleton_object(candidates).id) if candidates else ""
            objects = []
            for obj in candidates:
                components = []
                for component in obj.components:
                    key, _, _ = extractor.get_component_primary_draw(component)
                    components.append({
                        "unique_str": f"{key[0]}-{key[1]}-{key[2]}", "ib_hash": str(key[0]).lower(),
                        "index_count": int(key[1]), "first_index": int(key[2]),
                        "vertex_count": int(component.mesh.format.vertex_count),
                        "cpu_posed": bool(component.mesh.cpu_posed),
                        "draws": sorted(record.shader_call.id for record in extractor.get_component_draw_records(component)),
                    })
                left = left_out_draws(readback, [index for item in components for index in item["draws"]])
                objects.append({"name": str(obj.id), "components": components,
                                "component_count": len(components),
                                "vertices": sum(item["vertex_count"] for item in components),
                                "indices": sum(item["index_count"] for item in components),
                                **({"draws_left_out": left,
                                    "incomplete": "The game rewrites the index data of these draws, so they were not "
                                                  "archived; this object cannot be extracted."} if left else {})})
    finally:
        if extractor is not None:
            extractor._bridge_buffer_bytes.clear()
        sys.dont_write_bytecode = previous
    return {"archive": str(archive), "frame": capture.frame, "default_object": default, "objects": objects,
            "eligible_draws": len(draws), "left_out": readback.get("left_out"),
            "missing_texture_resources": missing_textures,
            "screenshot": str(archive / "current-screen.png") if (archive / "current-screen.png").is_file() else ""}


# ---------------------------------------------------------------- LoD mapping
def _lod_mapper_module(native):
    return importlib.import_module(native.extract.__name__.rsplit(".", 1)[0] + ".merged_skeleton_lod_mapper")


class _LodSources:
    """Stands in for DumpWorkspaceExtractor inside LoyalTools' own LoD mapper.

    The mapper opens the full-detail frame and the LoD frame by folder. A probe
    archive is opened with the archive-backed extractor, a FrameAnalysis dump
    with the native one; everything else the mapper asks of the class is the
    native class's. The mapping algorithm itself is not touched.
    """

    def __init__(self, native):
        self.native = native
        self.opened = []
        self.kinds = {}
        self.readbacks = {}

    def __call__(self, folder, *args, **kwargs):
        path = Path(folder).resolve()
        if (path / "capture.json").is_file():
            capture, readback, draws = _read_archive(path)
            if readback.get("complete") is not True or readback.get("errors"):
                raise BridgeError(f"Archive {path} is not a completed collection")
            extractor, _ = _build_extractor(path, self.native, capture, draws, "", readback)
            self.opened.append(extractor)
            self.kinds[str(path)] = "probe_archive"
            self.readbacks[str(path)] = (extractor, readback)
            return extractor
        self.kinds[str(path)] = "frame_analysis"
        return self.native.extract.DumpWorkspaceExtractor(str(folder), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.native.extract.DumpWorkspaceExtractor, name)

    def left_out(self, folder, object_name: str) -> list[int]:
        """Draws of the named object that the archive in ``folder`` left out (rewritten index data)."""
        extractor, readback = self.readbacks.get(str(Path(folder).resolve()), (None, None))
        if extractor is None:
            return []
        for candidate in extractor.get_merged_skeleton_candidates():
            if str(candidate.id) == object_name:
                return left_out_draws(readback, _object_draws(extractor, candidate))
        return []

    def close(self):
        for extractor in self.opened:
            extractor._bridge_buffer_bytes.clear()


def _lod_report(native, workspace: Path) -> dict:
    profile = native.profile.load_profile(str(workspace), required=True)
    levels = {}
    for component in profile["components"]:
        for lod in component["lods"]:
            entry = levels.setdefault(lod["lod_object_name"], {"matched": [], "fallback": [], "separate_ib": 0})
            entry["fallback" if lod["is_fallback"] else "matched"].append(component["unique_str"])
            if lod["ib_hash"] != component["ib_hash"]:
                entry["separate_ib"] += 1
    return {
        "workspace": str(workspace), "object_name": profile.get("object_name", ""),
        "component_count": profile["component_count"], "max_lod_count": profile["max_lod_count"],
        "lod_objects": {name: {"matched_components": len(item["matched"]), "fallback_components": item["fallback"],
                               "components_with_own_lod_ib": item["separate_ib"],
                               "source": profile.get("lod_sources", {}).get(name, ""),
                               "preview_workspace": profile.get("lod_preview_workspaces", {}).get(name, "")}
                        for name, item in sorted(levels.items())},
        "source_frame_dump": profile.get("source_frame_dump", ""),
        "profile_sha256": hashlib.sha256((workspace / native.profile.PROFILE_FILENAME).read_bytes()).hexdigest(),
    }


def lod_status(workspace_dir: str | Path, loyaltools_root: str | Path = DEFAULT_LOYALTOOLS) -> dict:
    """The LoD levels a workspace profile currently maps, per LoD object."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        native = _load_loyaltools(Path(loyaltools_root).resolve())
        return _lod_report(native, Path(workspace_dir).resolve())
    finally:
        sys.dont_write_bytecode = previous


def restore_lod(workspace_dir: str | Path, source_path: str | Path = "",
                loyaltools_root: str | Path = DEFAULT_LOYALTOOLS) -> dict:
    """LoyalTools' "JSON match": apply a LoD mapping that is already stored as JSON.

    ``source_path`` names the profile JSON, or the workspace, of the same
    character that holds a mapping; empty uses this workspace's own profile or
    the copy kept from before it was extracted again. No capture is read.
    """
    workspace = Path(workspace_dir).resolve()
    log = io.StringIO()
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with redirect_stdout(log):
            native = _load_loyaltools(Path(loyaltools_root).resolve())
            mapper = _lod_mapper_module(native)
            result = mapper.restore_merged_skeleton_lod(str(workspace), str(source_path) if source_path else "")
            report = _lod_report(native, workspace)
    except ValueError as exc:
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError(str(exc)) from exc
    finally:
        sys.dont_write_bytecode = previous
    report.update(method="json", mapping_source=result.source_path, profile_changed=result.changed,
                  applied_lod_objects=result.lod_object_names,
                  matched_component_counts=result.matched_component_counts,
                  lower_poly_component_counts=result.lower_poly_component_counts,
                  warnings=result.warnings, notes=result.notes)
    return report


def map_lod(workspace_dir: str | Path, lod_source: str | Path, full_source: str | Path = "",
            loyaltools_root: str | Path = DEFAULT_LOYALTOOLS) -> dict:
    """LoyalTools' "Frame Analysis match", with either kind of frame as input.

    ``lod_source`` is the frame that shows the character at a distance and
    ``full_source`` the full-detail frame the workspace was extracted from
    (default: the one recorded in the profile). Each may be a probe archive or
    a FrameAnalysis dump folder.
    """
    workspace = Path(workspace_dir).resolve()
    log = io.StringIO()
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sources = None
    try:
        with redirect_stdout(log):
            native = _load_loyaltools(Path(loyaltools_root).resolve())
            profile = native.profile.load_profile(str(workspace), required=True)
            full = Path(full_source or profile.get("source_frame_dump", "")).resolve() if (
                full_source or profile.get("source_frame_dump")) else None
            if full is None or not full.is_dir():
                raise BridgeError("The full-detail frame of this workspace is not available: " + str(full or ""))
            lod = Path(lod_source).resolve()
            if not lod.is_dir():
                raise BridgeError("LoD frame folder not found: " + str(lod))
            mapper = _lod_mapper_module(native)
            sources = _LodSources(native)
            function = mapper.map_merged_skeleton_lod
            bound = types.FunctionType(function.__code__, function.__globals__ | {"DumpWorkspaceExtractor": sources},
                                       function.__name__, function.__defaults__, function.__closure__)
            bound.__kwdefaults__ = function.__kwdefaults__
            result = bound(str(workspace), str(full), str(lod), allow_overwrite=True)
            left = sources.left_out(lod, str(result.lod_object_name))
            report = _lod_report(native, workspace)
    except ValueError as exc:
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError(str(exc)) from exc
    finally:
        if sources is not None:
            sources.close()
        sys.dont_write_bytecode = previous
    report.update(method="frame_match", full_source=str(full), lod_source=str(lod),
                  source_kinds=sources.kinds, lod_object_name=result.lod_object_name,
                  matched_component_count=result.matched_component_count,
                  lower_poly_component_count=result.lower_poly_component_count,
                  preview_workspace_folder=result.preview_workspace_folder,
                  preview_component_count=result.preview_component_count,
                  warnings=list(result.warnings) + ([
                      f"The LoD frame was archived without draw(s) {left[:8]} of {result.lod_object_name}: the game "
                      "rewrites their index data. A component drawn only that way has no LoD mesh here."] if left else []),
                  upstream_log=[line for line in log.getvalue().splitlines() if line.strip()][-40:])
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("workspace", nargs="?")
    parser.add_argument("--loyaltools-root", default=os.environ.get("EFMI_LOYALTOOLS", ""),
                        help="LoyalTools folder to call (default: the add-on installed in Blender)")
    parser.add_argument("--blender", default="",
                        help="blender.exe whose installed add-on is used (default: EFMI_BLENDER, else the known install)")
    parser.add_argument("--object-name", default="")
    parser.add_argument("--no-textures", action="store_true")
    parser.add_argument("--allow-unverified", action="store_true", help="Debug only: allow an incomplete/failed readback archive")
    parser.add_argument("--validation-archive", action="append", default=[], help="Additional independent static-validation archive; supply at least two")
    args = parser.parse_args(argv)
    if args.workspace:
        addon = None
        if not args.loyaltools_root:
            from . import blender_runner
            try:
                addon = blender_runner.find_addon(blender_runner.find_blender(args.blender))
            except blender_runner.BlenderError:
                if args.blender:
                    raise
        root, origin = resolve_loyaltools(args.loyaltools_root or None, addon)
        report = export_merged_workspace(args.archive, args.workspace, root,
                                          args.object_name, not args.no_textures, args.allow_unverified,
                                          args.validation_archive)
        report["loyaltools"] = {"path": str(root), "version": loyaltools_version(root), "origin": origin}
    else:
        report = inspect_archive(args.archive)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
