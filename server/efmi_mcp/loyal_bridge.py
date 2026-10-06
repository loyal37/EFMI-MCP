"""Offline bridge from a probe archive to LoyalTools' merged-skeleton extractor.

The archive contains capture.json (the compact probe document), cb-snapshots.bin,
buffers/<resource-id>.buf (whole resources), textures/<resource-id>.dds, and
readback.json.  Optional readback.eligible_draws restricts the draw indices used.
All skeleton grouping/deduplication and workspace writing remain in LoyalTools.
The external package is imported without bpy or writing its bytecode caches.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import types

from .capture_model import Capture


DEFAULT_LOYALTOOLS = Path(r"E:\loyal tools\tools\LoyalTools")


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


def _build_extractor(archive: Path, native, capture: Capture, draws: list[dict], object_name: str):
    e, r, b, fd = native.extract, native.resources, native.buffers, native.descriptors
    readback_doc = json.loads((archive / "readback.json").read_text(encoding="utf-8"))
    checked_buffers = {}

    class ArchiveVertexBuffer(r.VertexBuffer):
        def load_format(self, *args, **kwargs):
            # The capture's real input layout replaces a FrameAnalysis .txt file.
            if self.migoto_format is None:
                raise BridgeError("Missing capture vertex format")

    class ArchiveIndexBuffer(r.IndexBuffer):
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

        def _load_txt_format(self, resource):
            return getattr(resource, "migoto_format", None)

        def get_merged_skeleton_candidates(self, ignore_incomplete_draw_calls=False):
            if self._bridge_candidates is None:
                self._bridge_candidates = super().get_merged_skeleton_candidates(ignore_incomplete_draw_calls)
            return self._bridge_candidates

        def select_merged_skeleton_object(self, candidates, requested_name=""):
            name = object_name or requested_name
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
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
            raise BridgeError(f"Archived buffer {rid} differs from its recorded SHA256")
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
            geometry_windows.append({"draw": index, "slot": f"vb{slot_id}", "resource": bind["res"],
                                     "offset": byte_offset + base * stride, "size": addressed_count * stride,
                                     "vertices": addressed_count, "stride": stride,
                                     "xxmi_hash_offset": region["offset"], "xxmi_hash_size": region["size"]})
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
                    resource = r.Resource(hash=bind["hash"] or "00000000", pointer=source["ptr"],
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
    signature, matrices, geometry_payloads = {}, {}, {}
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
            geometry_payloads[(key, f"vb{slot.slot_id}")] = slot.raw_bytes
            geometry_bytes += len(slot.raw_bytes)
            entry["vbs"][str(slot.slot_id)] = {
                "stride": slot.stride, "bytes": len(slot.raw_bytes),
                "sha256": hashlib.sha256(slot.raw_bytes).hexdigest(),
                "layout": slot.layout.to_string(),
            }
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
        if record.get("meta", {}).get("unchanged_since_capture") is not True:
            raise BridgeError(f"Geometry buffer {rid} is not verified unchanged since capture")
    if not weighted_cross_bones or not weighted_cross_channels:
        raise BridgeError("Native mapping has no cross-component merge carrying nonzero vertex weights")
    return {
        "signature": signature, "matrices": matrices, "geometry_payloads": geometry_payloads, "frame": capture.frame,
        "selected_object": str(selected.id), "components": len(selected.components),
        "geometry_bytes": geometry_bytes, "geometry_buffers": sorted(geometry_resources),
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
    reference = samples[0]
    changed = set()
    summaries = []
    for archive, sample in zip(archives, samples):
        if sample["signature"] != reference["signature"]:
            different = sorted(key for key in set(reference["signature"]) | set(sample["signature"])
                               if reference["signature"].get(key) != sample["signature"].get(key))
            raise BridgeError(f"Static geometry/layout or canonical bone sources differ in {archive}: {different}")
        if sample.get("geometry_payloads") != reference.get("geometry_payloads"):
            raise BridgeError(f"Actual complete exported geometry bytes differ in {archive}")
        if set(sample["matrices"]) != set(reference["matrices"]):
            raise BridgeError(f"Used bone identities differ in {archive}")
        import numpy as np
        this_changed = [key for key, matrix in sample["matrices"].items()
                        if not np.array_equal(matrix, reference["matrices"][key])]
        changed.update(this_changed)
        summary = {key: value for key, value in sample.items()
                   if key not in ("signature", "matrices", "geometry_payloads")}
        summary.update(archive=str(archive), changed_used_bone_matrices_vs_first=len(this_changed))
        summaries.append(summary)
    if not changed:
        raise BridgeError("Bone matrices did not change between samples; repeated poses do not validate canonical stability")
    return {
        "scope": "static_geometry_and_canonical_vertex_groups", "samples": summaries,
        "changed_used_bone_matrices": len(changed),
        "canonical_source_comparison": "component unique_str and local bone id, not numeric merged group alone",
        "geometry_comparison": "Direct byte equality and SHA256 of actual index range and all exported vertex bytes, with unchanged whole geometry buffers",
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
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with redirect_stdout(log):
            native = _load_loyaltools(loyaltools_root)
            extractor, missing_textures = _build_extractor(archive, native, capture, draws, object_name)
            candidates = extractor.get_merged_skeleton_candidates()
            selected = extractor.select_merged_skeleton_object(candidates)
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
                        se, _ = _build_extractor(sample_archive, native, sc, sd, object_name)
                        so = se.select_merged_skeleton_object(se.get_merged_skeleton_candidates())
                    if sr.get("complete") is not True or sr.get("errors") or sr.get("validation_scope") != "deferred_skeleton_static":
                        raise BridgeError(f"Archive {sample_archive} is not a completed deferred_skeleton_static collection")
                    samples.append(_static_sample(se, so, native, sc, sr))
                validation = _validate_static_samples(samples, archives)
                verified = True
            result = extractor.extract_merged_skeleton(str(workspace), copy_textures=copy_textures)
            profile = native.profile.load_profile(str(workspace))
    finally:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("workspace", nargs="?")
    parser.add_argument("--loyaltools-root", default=str(DEFAULT_LOYALTOOLS))
    parser.add_argument("--object-name", default="")
    parser.add_argument("--no-textures", action="store_true")
    parser.add_argument("--allow-unverified", action="store_true", help="Debug only: allow an incomplete/failed readback archive")
    parser.add_argument("--validation-archive", action="append", default=[], help="Additional independent static-validation archive; supply at least two")
    args = parser.parse_args(argv)
    if args.workspace:
        report = export_merged_workspace(args.archive, args.workspace, args.loyaltools_root,
                                          args.object_name, not args.no_textures, args.allow_unverified,
                                          args.validation_archive)
    else:
        report = inspect_archive(args.archive)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
