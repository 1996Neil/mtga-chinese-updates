#!/usr/bin/env python3
"""Build a temporary UnityFS candidate with a four-byte CRC compensation tail.

Only append to the last existing .resS node, after every declared stream range.
All source files are read-only. Outputs must stay in the candidate directory. The altered
bundle has a different SHA-256; this does not restore a signature or source hash.

Example:
  PYTHONPATH=/tmp/mtga-unity-inspect-deps python3 preserve_bundle_crc.py \
    --input modified-fonts.mtga --output /tmp/fonts-crc-candidate.mtga \
    --target-crc dff77010 --reference-original original-fonts.mtga
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import warnings
import zlib
from pathlib import Path

import UnityPy
import lz4.block
from UnityPy.files import BundleFile
from UnityPy.streams import EndianBinaryReader

UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.14f1"
warnings.filterwarnings("ignore")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_fs_crc(self, reader):
    infos, data = _original_read_fs(self, reader)
    payload = data.bytes
    self.audit_crc32 = zlib.crc32(payload) & 0xFFFFFFFF
    self.audit_payload_size = len(payload)
    view = memoryview(payload)
    self.audit_nodes = [
        {
            "path": n.path,
            "offset": n.offset,
            "size": n.size,
            "flags": n.flags,
            "sha256": sha(view[n.offset : n.offset + n.size]),
        }
        for n in infos
    ]
    return infos, data


_original_read_fs = BundleFile.read_fs
BundleFile.read_fs = read_fs_crc


def load_bundle(path):
    env = UnityPy.load(str(path))
    files = list(env.files.values())
    assert len(files) == 1 and isinstance(files[0], BundleFile)
    bundle = files[0]
    assert bundle.signature == "UnityFS"
    return env, bundle


def solve_four_byte_suffix(prefix_crc, target_crc):
    """Solve the 32 CRC effect vectors over GF(2), without brute force."""
    zero_crc = zlib.crc32(b"\0" * 4, prefix_crc) & 0xFFFFFFFF
    columns = [
        (zlib.crc32((1 << bit).to_bytes(4, "little"), prefix_crc) & 0xFFFFFFFF)
        ^ zero_crc
        for bit in range(32)
    ]
    basis = {}
    for bit, vector in enumerate(columns):
        combination = 1 << bit
        while vector:
            pivot = vector.bit_length() - 1
            if pivot in basis:
                v, c = basis[pivot]
                vector ^= v
                combination ^= c
            else:
                basis[pivot] = (vector, combination)
                break
    assert len(basis) == 32, "Four suffix bytes do not span all CRC bits"
    delta = target_crc ^ zero_crc
    solution = 0
    while delta:
        pivot = delta.bit_length() - 1
        vector, combination = basis[pivot]
        delta ^= vector
        solution ^= combination
    tail = solution.to_bytes(4, "little")
    assert zlib.crc32(tail, prefix_crc) & 0xFFFFFFFF == target_crc
    return tail, zero_crc


def streamed_ranges(env):
    """Collect offset/size/path records, including nested stream metadata."""
    rows = []

    def walk(value, field, obj):
        if isinstance(value, dict):
            if {"offset", "size", "path"} <= value.keys():
                offset, size, path = value["offset"], value["size"], value["path"]
                if isinstance(offset, int) and isinstance(size, int) and isinstance(path, str):
                    assert offset >= 0 and size >= 0
                    if size or path:
                        rows.append(
                            {
                                "object_id": obj.path_id,
                                "object_type": obj.type.name,
                                "field": field,
                                "offset": offset,
                                "size": size,
                                "path": path,
                            }
                        )
            for key, child in value.items():
                walk(child, f"{field}.{key}" if field else key, obj)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list, tuple)):
                    walk(child, f"{field}[{index}]", obj)

    for obj in env.objects:
        # Unity Font has embedded FontData and PPtr<Texture>, not a stream range.
        # Its referenced Texture2D objects are scanned independently below.
        if obj.type.name == "Font":
            continue
        walk(obj.read_typetree(), "", obj)
    return rows


def resource_ranges(rows, node_name, node_size):
    selected = []
    for row in rows:
        if row["path"].rsplit("/", 1)[-1] != node_name:
            continue
        assert row["offset"] + row["size"] <= node_size
        selected.append(row)
    assert selected, "No declared references to the selected .resS node"
    assert all(row["size"] > 0 for row in selected)
    return selected


def metadata(bundle):
    return {
        "signature": bundle.signature,
        "format_version": bundle.version,
        "version_player": bundle.version_player,
        "version_engine": bundle.version_engine,
        "nodes": bundle.audit_nodes,
    }


def externals(env):
    result = {}
    for obj in env.objects:
        af = obj.assets_file
        key = af.name
        if key not in result:
            result[key] = [(e.guid.hex(), e.type, e.path) for e in af.externals]
    return result


def raw_object_hashes(env):
    return {obj.path_id: sha(obj.get_raw_data()) for obj in env.objects}


def manual_unityfs_crc(path):
    """Independent UnityFS block decode, to cross-check UnityPy read_fs CRC."""
    data = path.read_bytes()
    position = data.index(b"\0") + 1
    assert data[:position] == b"UnityFS\0"
    version = struct.unpack_from(">I", data, position)[0]
    position += 4
    for _ in range(2):
        position = data.index(b"\0", position) + 1
    length, compressed_size, expanded_size, flags = struct.unpack_from(">QIII", data, position)
    position += 20
    assert length == len(data)
    if version >= 7:
        position = (position + 15) & ~15
    info_position = len(data) - compressed_size if flags & 0x80 else position
    encoded = data[info_position : info_position + compressed_size]

    def expand(block, expected, block_flags):
        compression = block_flags & 0x3F
        if compression == 0:
            expanded = block
        elif compression in (2, 3):
            expanded = lz4.block.decompress(block, uncompressed_size=expected)
        else:
            raise ValueError(f"Independent parser does not support compression {compression}")
        assert len(expanded) == expected
        return expanded

    info = expand(encoded, expanded_size, flags)
    if not flags & 0x80:
        position += compressed_size
    if flags & 0x200:
        position = (position + 15) & ~15
    blocks = struct.unpack_from(">I", info, 16)[0]
    cursor = 20
    crc, size = 0, 0
    for _ in range(blocks):
        expanded, compressed, block_flags = struct.unpack_from(">IIH", info, cursor)
        cursor += 10
        payload = expand(data[position : position + compressed], expanded, block_flags)
        position += compressed
        crc = zlib.crc32(payload, crc)
        size += expanded
    node_count = struct.unpack_from(">I", info, cursor)[0]
    cursor += 4
    nodes = []
    for _ in range(node_count):
        offset, node_size, node_flags = struct.unpack_from(">QQI", info, cursor)
        cursor += 20
        end = info.index(b"\0", cursor)
        node_name = info[cursor:end].decode()
        cursor = end + 1
        assert offset + node_size <= size
        nodes.append({"path": node_name, "offset": offset, "size": node_size, "flags": node_flags})
    assert cursor == len(info)
    return {"crc32": crc & 0xFFFFFFFF, "payload_size": size, "nodes": nodes, "flags": flags}


def tmp_output(path):
    resolved = path.resolve()
    assert Path(__file__).resolve().parent / "state" in resolved.parents, "Output must stay under Maintainer state"
    return resolved


def build_candidate(source, output, target_crc, reference=None, packer="none", report_path=None):
    source, output = source.resolve(), tmp_output(output)
    assert source != output and not output.exists(), "Use a fresh output path"
    report_path = tmp_output(report_path or output.with_suffix(output.suffix + ".audit.json"))
    assert report_path != source and report_path != output and not report_path.exists()
    source_sha = sha(source.read_bytes())
    env, bundle = load_bundle(source)
    before = metadata(bundle)
    hashes_before = raw_object_hashes(env)
    externals_before = externals(env)
    ranges_before = streamed_ranges(env)
    resource_names = [name for name in bundle.files if name.endswith(".resS")]
    assert len(resource_names) == 1, "Require exactly one .resS node"
    name = resource_names[0]
    assert list(bundle.files)[-1] == name, "CRC tail must be in the last existing node"
    resource = bundle.files[name]
    original_resource = bytes(resource.bytes)
    original_resource_size = len(original_resource)
    selected = resource_ranges(ranges_before, name, original_resource_size)
    max_end = max(row["offset"] + row["size"] for row in selected)
    assert max_end <= original_resource_size
    original_flags = resource.flags
    # Refuse any unnoticed serialized-node rewrite before changing the resource tail.
    for node in before["nodes"]:
        file = bundle.files[node["path"]]
        payload = file.bytes if isinstance(file, EndianBinaryReader) else file.save()
        assert sha(payload) == node["sha256"]
        assert len(payload) == node["size"]
    assert before["nodes"][-1]["offset"] + original_resource_size == bundle.audit_payload_size
    source_manual = manual_unityfs_crc(source)
    assert source_manual["crc32"] == bundle.audit_crc32
    assert source_manual["payload_size"] == bundle.audit_payload_size
    assert source_manual["nodes"] == [{k: v for k, v in n.items() if k != "sha256"} for n in before["nodes"]]

    reference_report = None
    if reference is not None:
        reference = reference.resolve()
        reference_sha = sha(reference.read_bytes())
        original_env, original_bundle = load_bundle(reference)
        assert original_bundle.audit_crc32 == target_crc, "Target differs from the reference's real CRC"
        assert list(original_bundle.files) == list(bundle.files)
        assert bytes(original_bundle.files[name].bytes) == original_resource
        original_ranges = streamed_ranges(original_env)
        assert original_ranges == ranges_before
        texture_ids = {o.path_id for o in original_env.objects if o.type.name == "Texture2D"}
        original_hashes = raw_object_hashes(original_env)
        assert all(original_hashes[i] == hashes_before[i] for i in texture_ids)
        reference_report = {
            "path": str(reference),
            "sha256": reference_sha,
            "crc32": f"{original_bundle.audit_crc32:08x}",
            "resource_bytes_equal_to_candidate_input": True,
            "all_stream_declarations_equal_to_candidate_input": True,
            "texture_object_count": len(texture_ids),
            "all_texture_object_raw_bytes_equal_to_candidate_input": True,
            "input_changed_object_ids_vs_reference": [i for i in original_hashes if original_hashes[i] != hashes_before[i]],
        }
        del original_env, original_bundle

    tail, zero_crc = solve_four_byte_suffix(bundle.audit_crc32, target_crc)
    replacement = EndianBinaryReader(original_resource + tail)
    replacement.flags = original_flags
    bundle.files[name] = replacement
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bundle.save(packer=packer))
    verified_env, verified_bundle = load_bundle(output)
    verified_resource = bytes(verified_bundle.files[name].bytes)
    after = metadata(verified_bundle)
    assert verified_resource == original_resource + tail
    assert raw_object_hashes(verified_env) == hashes_before
    assert externals(verified_env) == externals_before
    assert streamed_ranges(verified_env) == ranges_before
    assert before["signature"] == after["signature"]
    assert before["format_version"] == after["format_version"]
    assert before["version_player"] == after["version_player"]
    assert before["version_engine"] == after["version_engine"]
    assert [n["path"] for n in before["nodes"]] == [n["path"] for n in after["nodes"]]
    for old, new in zip(before["nodes"], after["nodes"]):
        assert old["path"] == new["path"] and old["offset"] == new["offset"] and old["flags"] == new["flags"]
        if old["path"] == name:
            assert new["size"] == old["size"] + 4
        else:
            assert old == new, "An existing non-resource node changed"
    assert verified_bundle.audit_payload_size == bundle.audit_payload_size + 4
    assert verified_bundle.audit_crc32 == target_crc
    manual = manual_unityfs_crc(output)
    assert manual["crc32"] == target_crc
    assert manual["payload_size"] == verified_bundle.audit_payload_size
    assert manual["nodes"] == [{k: v for k, v in n.items() if k != "sha256"} for n in after["nodes"]]
    range_reports = []
    for row in selected:
        start, size = row["offset"], row["size"]
        content = original_resource[start : start + size]
        assert verified_resource[start : start + size] == content
        range_reports.append({**row, "content_sha256": sha(content), "output_bytes_equal": True})
    assert all(row["offset"] + row["size"] <= original_resource_size for row in selected)
    assert sha(source.read_bytes()) == source_sha
    if reference is not None:
        assert sha(reference.read_bytes()) == reference_report["sha256"]
    result = {
        "method": "GF(2) four-byte CRC compensation appended to an unreferenced existing-resource tail",
        "source": str(source),
        "source_sha256": source_sha,
        "output": str(output),
        "output_sha256": sha(output.read_bytes()),
        "output_sha256_base64": base64.b64encode(hashlib.sha256(output.read_bytes()).digest()).decode(),
        "output_size": output.stat().st_size,
        "packer": packer,
        "source_uncompressed_crc32": f"{bundle.audit_crc32:08x}",
        "crc32_with_zero_suffix": f"{zero_crc:08x}",
        "target_and_output_uncompressed_crc32": f"{target_crc:08x}",
        "unitypy_read_fs_crc_verified": True,
        "independent_block_parser_crc_verified": True,
        "gf2_rank": 32,
        "resource_node": name,
        "resource_size_before": original_resource_size,
        "resource_size_after": len(verified_resource),
        "compensation_hex": tail.hex(),
        "compensation_interval": [original_resource_size, original_resource_size + 4],
        "max_declared_resource_range_end": max_end,
        "compensation_bytes_unreferenced": True,
        "all_existing_resource_bytes_unchanged": True,
        "all_existing_non_resource_nodes_byte_equal": True,
        "all_object_raw_bytes_equal_to_input": True,
        "object_count": len(hashes_before),
        "stream_declarations_unchanged": True,
        "external_dependencies_unchanged": True,
        "bundle_names_order_flags_and_node_offsets_preserved": True,
        "nodes_before": before["nodes"],
        "nodes_after": after["nodes"],
        "referenced_ranges": range_reports,
        "original_reference": reference_report,
        "source_and_original_reference_unchanged": True,
        "game_or_backup_files_written": False,
        "scope": "Temporary candidate build and static readback only. Real loader, update behavior, SHA checks and runtime rendering are unverified. The CRC equality is intentional compensation, not cryptographic integrity or restored original bytes.",
    }
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result, report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-crc", required=True)
    parser.add_argument("--reference-original", type=Path)
    parser.add_argument("--packer", choices=("none", "original", "lz4"), default="none")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    target = int(args.target_crc, 16)
    assert 0 <= target <= 0xFFFFFFFF
    result, report = build_candidate(args.input, args.output, target, args.reference_original, args.packer, args.report)
    print(json.dumps({k: result[k] for k in ("source", "output", "output_sha256", "output_size", "source_uncompressed_crc32", "target_and_output_uncompressed_crc32", "compensation_hex", "object_count", "compensation_bytes_unreferenced")}, ensure_ascii=False))
    print(str(report))


if __name__ == "__main__":
    main()
