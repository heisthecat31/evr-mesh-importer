"""
EVR Raw Mesh Importer — decode module
Pure Python, no bpy dependency.
https://github.com/Dualgame/evr-mesh-importer
"""

import struct
import math


def _find_runs(data, stride):
    """Yield (start_offset, vertex_count) for every run of >=4 identical
    8-byte seeds repeating at *stride*.  Overlapping runs are deduplicated."""
    n = len(data)
    used_starts = set()
    for start in range(0, n - stride * 4 + 1, 4):
        if start in used_starts:
            continue
        seed = data[start:start + 8]
        if seed == bytes(8):
            continue
        run = [start]
        pos = start + stride
        while pos + 8 <= n and data[pos:pos + 8] == seed:
            run.append(pos)
            pos += stride
        if len(run) < 4:
            continue
        for r in run:
            used_starts.add(r)
        yield start, len(run)


def _find_marker4_runs(data, stride, marker=b"\xff\xff\xff\xff"):
    """Yield (start_offset, vertex_count) for runs where the first 4 bytes of
    each record match *marker* at the given stride."""
    n = len(data)
    recs = n // stride
    cur_start = 0
    cur_len = 0
    for i in range(recs):
        off = i * stride
        if data[off:off + 4] == marker:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
        else:
            if cur_len >= 4:
                yield cur_start * stride, cur_len
            cur_len = 0
    if cur_len >= 4:
        yield cur_start * stride, cur_len


def _find_vertex_colored_runs(data, stride=20):
    """Yield (start_offset, vertex_count) for vertex-colored stride-20 runs.
    Bytes[3:7] are constant 0xffffffff while the RGB prefix varies per vertex."""
    n = len(data)
    recs = n // stride
    FF4 = b'\xff\xff\xff\xff'
    best_start = 0
    best_len = 0
    cur_start = 0
    cur_len = 0
    for i in range(recs):
        if data[i * stride + 3:i * stride + 7] == FF4:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_start = cur_start
                best_len = cur_len
        else:
            cur_len = 0
    if best_len >= 4:
        yield best_start * stride, best_len


def _find_prefix_pair_run(data, stride, prefix_pairs, min_records=16):
    """Return the length of the leading record run whose first two dwords
    match one of *prefix_pairs*."""
    recs = len(data) // stride
    count = 0
    for i in range(recs):
        pair = struct.unpack_from("<II", data, i * stride)
        if pair not in prefix_pairs:
            break
        count += 1
    return count if count >= min_records else 0


def _extract_submesh(data, s0_start, Nv, s0_stride):
    """Given a validated stream-0 run, extract verts, faces + UVs.
    Returns (verts, faces, uvs) or None if stream-1 XYZ is invalid."""
    n = len(data)
    s1_start = s0_start + Nv * s0_stride
    s1_end = s1_start + Nv * 28
    if s1_end > n:
        return None

    verts = []
    for j in range(Nv):
        off = s1_start + j * 28
        x, y, z = struct.unpack_from("<fff", data, off)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            return None
        if abs(x) > 50000 or abs(y) > 50000 or abs(z) > 50000:
            return None
        verts.append((x, y, z))

    ib_start = s1_end
    max_ib = min(ib_start + Nv * 8 * 2, n)
    idx_count = 0
    for ib_off in range(ib_start, max_ib, 2):
        idx = struct.unpack_from("<H", data, ib_off)[0]
        if idx >= Nv:
            break
        idx_count += 1
    idx_count -= idx_count % 3
    faces = []
    for k in range(0, idx_count, 3):
        i0, i1, i2 = struct.unpack_from("<HHH", data, ib_start + k * 2)
        faces.append((i0, i1, i2))

    uvs = []
    bone_data = []
    has_valid_bones = False

    if s0_stride >= 16:
        for j in range(Nv):
            s0_off = s0_start + j * s0_stride
            u, v = struct.unpack_from("<ff", data, s0_off + 8)
            if not (math.isfinite(u) and math.isfinite(v)):
                u, v = 0.0, 0.0
            uvs.append((u, v))

            # Extract Bone Weights (Stream 0: bytes 0-3) and Bone Indices (Stream 1: bytes 20-23)
            weights = struct.unpack_from("<4B", data, s0_off)
            indices = struct.unpack_from("<4B", data, s1_start + j * 28 + 20)
            bone_data.append((indices, weights))

            if sum(weights) > 50:
                has_valid_bones = True
    else:
        uvs = [(0.0, 0.0)] * Nv
        bone_data = [((0,0,0,0), (0,0,0,0))] * Nv

    if not has_valid_bones:
        bone_data = None

    return verts, faces, uvs, bone_data


# ============================================================
# Primary-assisted CIMR paths
# ============================================================

def _extract_primary_described_cimr_mesh(gpu_data, primary_data):
    """Decode CIMR GPU chunk using an exact Primary descriptor (range_kind=2).
    Returns [(verts, faces)] or []."""
    if not primary_data:
        return []

    meta = primary_data
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    max_block_vc = 0
    for _off in range(0, n_meta - 0x40 + 1, 4):
        if u32(_off) != 0x0B:
            continue
        _vc = u32(_off + 7 * 4)
        if _vc and u32(_off + 8 * 4) == _vc and u32(_off + 11 * 4) == _vc:
            if _vc > max_block_vc:
                max_block_vc = _vc

    best = None
    for off in range(0, n_meta - 0x40 + 1, 4):
        if u32(off) != 0x0B:
            continue

        vals = [u32(off + i * 4) for i in range(16)]
        base_offset = vals[2]
        stream0_size = vals[4]
        vertex_count = vals[7]
        index_offset = vals[12]
        index_words = vals[13]
        range_kind = vals[14]

        if vertex_count == 0 or vals[8] != vertex_count or vals[11] != vertex_count:
            continue
        if range_kind != 2 or index_words < 3 or index_words % 3 != 0:
            continue
        if stream0_size not in (vertex_count * 16, vertex_count * 20):
            continue
        if base_offset != 0 and vertex_count < max_block_vc:
            continue

        if base_offset == 0:
            pos_start = stream0_size
            index_range_valid = index_offset == pos_start + vertex_count * 28
        else:
            pos_start = base_offset + stream0_size
            index_range_valid = (index_offset < base_offset and
                                 index_offset + index_words * 2 <= base_offset)
        if not index_range_valid:
            continue

        pos_end = pos_start + vertex_count * 28
        if pos_end > len(gpu_data) or index_offset + index_words * 2 > len(gpu_data):
            continue

        idxs = struct.unpack_from(f"<{index_words}H", gpu_data, index_offset)
        if any(idx >= vertex_count for idx in idxs):
            continue
        if max(idxs) + 1 < vertex_count // 2:
            continue
        if base_offset != 0 and len(set(idxs)) < vertex_count:
            continue

        for pos_off in (0, 4, 8, 12, 16):
            verts = []
            valid = True
            for j in range(vertex_count):
                xyz_off = pos_start + j * 28 + pos_off
                if xyz_off + 12 > len(gpu_data):
                    valid = False
                    break
                x, y, z = struct.unpack_from("<fff", gpu_data, xyz_off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    valid = False
                    break
                if max(abs(x), abs(y), abs(z)) > 50000:
                    valid = False
                    break
                verts.append((x, y, z))
            if not valid:
                continue

            spans = [max(v[a] for v in verts) - min(v[a] for v in verts)
                     for a in range(3)]
            if all(s < 0.01 for s in spans):
                continue

            faces = []
            for k in range(0, len(idxs), 3):
                i0, i1, i2 = idxs[k:k + 3]
                if i0 == i1 or i1 == i2 or i0 == i2:
                    continue
                faces.append((i0, i1, i2))
            if len(faces) < 24:
                continue

            # Extract UVs from Stream-0
            s0_start = base_offset
            s0_stride = stream0_size // vertex_count
            uvs = []
            bone_data = []
            has_valid_bones = False
            
            if s0_stride >= 16:
                for j in range(vertex_count):
                    s0_off = s0_start + j * s0_stride
                    u, v = struct.unpack_from("<ff", gpu_data, s0_off + 8)
                    if not (math.isfinite(u) and math.isfinite(v)):
                        u, v = 0.0, 0.0
                    uvs.append((u, v))
                    
                    weights = struct.unpack_from("<4B", gpu_data, s0_off)
                    indices = struct.unpack_from("<4B", gpu_data, pos_start + j * 28 + pos_off + 20)
                    bone_data.append((indices, weights))
                    if sum(weights) > 50:
                        has_valid_bones = True
            else:
                uvs = [(0.0, 0.0)] * vertex_count
                bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count

            if not has_valid_bones:
                bone_data = None
            else:
                print('Found valid bones!')

            candidate = (len(faces), vertex_count, -pos_off, verts, faces, uvs, bone_data)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
            break

    if best is None:
        return []
    return [(best[3], best[4], best[5], best[6])]


def _extract_hero_cimr_mesh(gpu_data, primary_data):
    """Decode CIMR GPU payloads using extended Primary descriptor rules.
    Handles stride-28, multi-LOD (range_kind != 2), and small meshes.
    Returns [(verts, faces)] or []."""
    if not primary_data:
        return []

    meta = primary_data
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    best = None

    for off in range(0, n_meta - 0x40 + 1, 4):
        if u32(off) != 0x0B:
            continue

        vals = [u32(off + i * 4) for i in range(16)]
        base_offset = vals[2]
        stream0_size = vals[4]
        vertex_count = vals[7]

        if vertex_count == 0 or vals[8] != vertex_count or vals[11] != vertex_count:
            continue
        if stream0_size not in (vertex_count * 16, vertex_count * 20, vertex_count * 28):
            continue

        pos_start = (base_offset + stream0_size) if base_offset else stream0_size
        pos_end = pos_start + vertex_count * 28
        if pos_end > len(gpu_data):
            continue

        remaining = len(gpu_data) - pos_end
        max_scan = min(remaining // 2, vertex_count * 8)
        n_valid = 0
        for k in range(max_scan):
            idx = struct.unpack_from("<H", gpu_data, pos_end + k * 2)[0]
            if idx >= vertex_count:
                break
            n_valid += 1
        n_valid -= n_valid % 3
        if n_valid < 3:
            continue

        idxs = struct.unpack_from(f"<{n_valid}H", gpu_data, pos_end)

        for pos_off in (0, 4, 8, 12, 16):
            verts = []
            valid = True
            for j in range(vertex_count):
                xyz_off = pos_start + j * 28 + pos_off
                if xyz_off + 12 > len(gpu_data):
                    valid = False
                    break
                x, y, z = struct.unpack_from("<fff", gpu_data, xyz_off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    valid = False
                    break
                if max(abs(x), abs(y), abs(z)) > 50000:
                    valid = False
                    break
                verts.append((x, y, z))
            if not valid:
                continue

            spans = [max(v[a] for v in verts) - min(v[a] for v in verts)
                     for a in range(3)]
            if all(s < 0.01 for s in spans):
                continue

            faces = []
            for k in range(0, len(idxs), 3):
                i0, i1, i2 = idxs[k:k + 3]
                if i0 == i1 or i1 == i2 or i0 == i2:
                    continue
                faces.append((i0, i1, i2))
            # Extract UVs from Stream-0
            s0_start = base_offset
            s0_stride = stream0_size // vertex_count
            uvs = []
            if s0_stride >= 16:
                for j in range(vertex_count):
                    s0_off = s0_start + j * s0_stride
                    u, v = struct.unpack_from("<ff", gpu_data, s0_off + 8)
                    if not (math.isfinite(u) and math.isfinite(v)):
                        u, v = 0.0, 0.0
                    uvs.append((u, v))
            else:
                uvs = [(0.0, 0.0)] * vertex_count

            candidate = (vertex_count, len(faces), -pos_off, verts, faces, uvs)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
            break

    if best is None:
        return []
    return [(best[3], best[4], best[5])]


def _extract_crossref_ib_cimr_mesh(gpu_data, primary_data):
    """Recover the family of CIMRs where a non-zero-base rkind=2 block carries
    the authoritative IB but its ioff points into the region owned by a
    base_off=0 sentinel block.

    Structure (all 11 of the previously hash-confirmed cases follow this):
      - Block A  (base_off=0,    rkind=50855947): primary mesh geometry.
                 pos_end == rkind-2 block's ioff exactly.
      - Block B  (base_off!=0,   rkind=2):        LOD cross-reference descriptor.
                 Its ioff points inside Block A's region; its declared vc is
                 slightly larger than Block A's effective vertex count so
                 primary_described rejects it via the subset-IB guard.

    hero_cimr forward-scans from Block B's pos_end (past Block A's IB) and
    finds a smaller trailing sub-range IB, producing a lower-LOD mesh.
    This function instead pairs Block B's explicit ioff/iwords with Block A's
    geometry to produce the full primary mesh.
    """
    if not primary_data:
        return []

    meta = primary_data
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    blocks = []
    for off in range(0, n_meta - 0x40 + 1, 4):
        if u32(off) != 0x0B:
            continue
        vals = [u32(off + i * 4) for i in range(16)]
        vc = vals[7]
        if not (vc and vals[8] == vc and vals[11] == vc):
            continue
        base = vals[2]; s0 = vals[4]; rkind = vals[14]
        ioff = vals[12]; iwords = vals[13]
        if s0 not in (vc * 16, vc * 20, vc * 28):
            continue
        pos_start = (base + s0) if base else s0
        pos_end = pos_start + vc * 28
        blocks.append((base, vc, s0, rkind, ioff, iwords, pos_start, pos_end))

    best = None
    for (base2, vc2, s0_2, rkind2, ioff2, iwords2, ps2, pe2) in blocks:
        if base2 == 0 or rkind2 != 2:
            continue
        if iwords2 < 3 or iwords2 % 3 != 0:
            continue
        if ioff2 + iwords2 * 2 > len(gpu_data):
            continue

        idxs2 = struct.unpack_from(f"<{iwords2}H", gpu_data, ioff2)
        if any(idx >= vc2 for idx in idxs2):
            continue
        unique_count = len(set(idxs2))
        if unique_count >= vc2:
            continue

        effective_vc = max(idxs2) + 1

        for (base1, vc1, s0_1, rkind1, _, _, ps1, pe1) in blocks:
            if base1 != 0:
                continue
            if rkind1 != 50855947:
                continue
            if pe1 != ioff2:
                continue
            if vc1 != effective_vc:
                continue

            for pos_off in (0, 4, 8, 12, 16):
                verts = []
                valid = True
                for j in range(effective_vc):
                    xyz_off = ps1 + j * 28 + pos_off
                    if xyz_off + 12 > len(gpu_data):
                        valid = False; break
                    x, y, z = struct.unpack_from("<fff", gpu_data, xyz_off)
                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                        valid = False; break
                    if max(abs(x), abs(y), abs(z)) > 50000:
                        valid = False; break
                    verts.append((x, y, z))
                if not valid:
                    continue

                spans = [max(v[a] for v in verts) - min(v[a] for v in verts)
                         for a in range(3)]
                if all(s < 0.01 for s in spans):
                    continue

                faces = []
                for k in range(0, len(idxs2), 3):
                    i0, i1, i2 = idxs2[k:k + 3]
                    if i0 == i1 or i1 == i2 or i0 == i2:
                        continue
                    faces.append((i0, i1, i2))

                # Extract UVs from Stream-0
                s0_stride = s0_1 // vc1
                uvs = []
                if s0_stride >= 16:
                    for j in range(effective_vc):
                        s0_off = j * s0_stride
                        u, v = struct.unpack_from("<ff", gpu_data, s0_off + 8)
                        if not (math.isfinite(u) and math.isfinite(v)):
                            u, v = 0.0, 0.0
                        uvs.append((u, v))
                else:
                    uvs = [(0.0, 0.0)] * effective_vc

                last_v = verts[effective_vc - 1]
                if all(abs(c) < 1e-38 for c in last_v):
                    null_idx = effective_vc - 1
                    faces = [f for f in faces if null_idx not in f]
                    verts = verts[:null_idx]
                    uvs = uvs[:null_idx]

                if not faces:
                    continue

                candidate = (len(faces), effective_vc, -pos_off, verts, faces, uvs)
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
                break

    if best is None:
        return []
    return [(best[3], best[4], best[5])]


# ============================================================
# Primary-assisted CGML paths
# ============================================================

def _extract_cgml_ranges(gpu_data, base_offset, stream0_size, vertex_count,
                         index_count, pos_stride, index_offset=None, index_stride=2):
    pos_start = base_offset + stream0_size
    ib_start = index_offset if index_offset is not None else pos_start + vertex_count * pos_stride
    if pos_start + vertex_count * pos_stride > len(gpu_data):
        return None
    if ib_start + index_count * index_stride > len(gpu_data):
        return None
    verts = []
    for j in range(vertex_count):
        x, y, z = struct.unpack_from("<fff", gpu_data, pos_start + j * pos_stride)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            return None
        if abs(x) > 50000 or abs(y) > 50000 or abs(z) > 50000:
            return None
        verts.append((x, y, z))

    faces = []
    if index_stride == 4:
        for k in range(0, index_count - index_count % 3, 3):
            i0, i1, i2 = struct.unpack_from("<III", gpu_data, ib_start + k * 4)
            if i0 >= vertex_count or i1 >= vertex_count or i2 >= vertex_count:
                return None
            faces.append((i0, i1, i2))
    else:
        for k in range(0, index_count - index_count % 3, 3):
            i0, i1, i2 = struct.unpack_from("<HHH", gpu_data, ib_start + k * 2)
            if i0 >= vertex_count or i1 >= vertex_count or i2 >= vertex_count:
                return None
            faces.append((i0, i1, i2))
    if not faces:
        return None

    s0_stride = stream0_size // vertex_count if vertex_count else 16
    uvs = []
    bone_data = []
    has_valid_bones = False
    
    if s0_stride >= 16:
        for j in range(vertex_count):
            s0_off = base_offset + j * s0_stride
            u, v = struct.unpack_from("<ff", gpu_data, s0_off + 8)
            if not (math.isfinite(u) and math.isfinite(v)):
                u, v = 0.0, 0.0
            uvs.append((u, v))
            
            if s0_stride >= 24:
                indices = struct.unpack_from("<4B", gpu_data, s0_off + s0_stride - 8)
                weights = struct.unpack_from("<4B", gpu_data, s0_off + s0_stride - 4)
            elif s0_stride == 20:
                weights = struct.unpack_from("<4B", gpu_data, s0_off)
                indices = struct.unpack_from("<4B", gpu_data, s0_off + 4)
            else:
                weights = struct.unpack_from("<4B", gpu_data, s0_off)
                indices = struct.unpack_from("<4B", gpu_data, ib_start - vertex_count * 28 + j * 28 + 16)
            
            bone_data.append((indices, weights))
            if sum(weights) > 50:
                has_valid_bones = True
    else:
        uvs = [(0.0, 0.0)] * vertex_count
        bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count

    if not has_valid_bones:
        bone_data = None

    return verts, faces, uvs, bone_data


def _decode_compact_cgml(meta, gpu_data):
    """Compact-Primary path (~784 byte Primary). Returns [(verts, faces)] or []."""
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    if len(meta) < 0x30C:
        return []
    gpu_size_stored = u32(0x308)
    if gpu_size_stored != len(gpu_data):
        return []
    stream0_size = u32(0x2b4)
    nv = u32(0x2c0)
    ib_start_gpu = u32(0x2e0)
    ib_bytes = u32(0x2e4)
    if nv == 0 or stream0_size == 0 or ib_bytes == 0:
        return []
    if stream0_size % nv != 0:
        return []
    s0_stride = stream0_size // nv
    if s0_stride not in (12, 16, 20, 24, 28, 32):
        return []
    if ib_bytes % 2 != 0:
        return []
    n_idx = ib_bytes // 2
    pos_start = stream0_size
    if ib_start_gpu <= pos_start:
        return []
    pos_span = ib_start_gpu - pos_start
    if pos_span % nv != 0:
        return []
    pos_stride = pos_span // nv
    if pos_stride not in (12, 16, 20, 24, 28, 32):
        return []
    result = _extract_cgml_ranges(gpu_data, 0, stream0_size, nv, n_idx, pos_stride, ib_start_gpu)
    if result is None:
        return []
    verts, faces, _ = result
    if not faces:
        return []
    return [(verts, faces)]


def _decode_zero_tail_kind4_u32_cgml(meta, gpu_data):
    """Zero-tail kind-4 u32 index CGML path. Returns [(verts, faces)] or []."""
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    arrays = []
    off = 0
    for stride in sizes:
        if off + 4 > len(meta):
            return []
        count = u32(off)
        base = off + 4
        end = base + count * stride
        if end > len(meta):
            return []
        arrays.append((base, count, stride))
        off = end
    if len(meta) - off < 16:
        return []
    tail_a, mesh_data_end, gpu_size = struct.unpack_from("<IIQ", meta, off)
    if tail_a != 0 or mesh_data_end != 0 or gpu_size != len(gpu_data):
        return []
    array2_base, array2_count, _ = arrays[2]
    array5_base, array5_count, _ = arrays[5]
    if array2_count != 1 or array5_count != 1:
        return []
    rec_off = array2_base
    base_offset = struct.unpack_from("<I", meta, rec_off + 0x128)[0]
    stream0_size = struct.unpack_from("<I", meta, rec_off + 0x130)[0]
    vertex_count = struct.unpack_from("<I", meta, rec_off + 0x13C)[0]
    vertex_count_2 = struct.unpack_from("<I", meta, rec_off + 0x140)[0]
    index_offset, index_count, range_kind, range_extra = struct.unpack_from(
        "<IIII", meta, array5_base)
    if vertex_count == 0 or vertex_count != vertex_count_2:
        return []
    if stream0_size != vertex_count * 20:
        return []
    pos_start = base_offset + stream0_size
    pos_end = pos_start + vertex_count * 28
    if pos_end != index_offset or pos_end > len(gpu_data):
        return []
    if range_kind != 4 or range_extra != 0:
        return []
    if index_count < 3 or index_offset + index_count * 4 > len(gpu_data):
        return []
    verts = []
    for j in range(vertex_count):
        x, y, z = struct.unpack_from("<fff", gpu_data, pos_start + j * 28)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            return []
        if max(abs(x), abs(y), abs(z)) > 50000:
            return []
        verts.append((x, y, z))
    idxs = struct.unpack_from(f"<{index_count}I", gpu_data, index_offset)
    faces = []
    for k in range(0, len(idxs) - len(idxs) % 3, 3):
        i0, i1, i2 = idxs[k:k + 3]
        if i0 >= vertex_count or i1 >= vertex_count or i2 >= vertex_count:
            return []
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        faces.append((i0, i1, i2))
    if not faces:
        return []
    return [(verts, faces)]


def _extract_metadata_meshes(gpu_data, primary_data):
    """Use Primary/CGMeshListResource metadata to extract split GPU streams.
    Returns [(verts, faces), ...] or []."""
    if not primary_data:
        return []

    meta = primary_data
    n_meta = len(meta)

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    arrays = []
    off = 0
    for stride in sizes:
        if off + 4 > len(meta):
            arrays = []
            break
        count = u32(off)
        base = off + 4
        end = base + count * stride
        if end > len(meta):
            arrays = []
            break
        arrays.append((base, count, stride))
        off = end

    submeshes = []

    if arrays and len(meta) - off >= 16:
        tail_a, mesh_data_end, gpu_size = struct.unpack_from("<IIQ", meta, off)
        if gpu_size == len(gpu_data) and (mesh_data_end <= len(gpu_data)):
            array1_base, array1_count, _ = arrays[1]
            array2_base, array2_count, _ = arrays[2]
            array5_base, array5_count, _ = arrays[5]
            if array2_count == array5_count:
                for i in range(array2_count):
                    rec_off = array2_base + i * 0x150
                    range_rec_off = array5_base + i * 0x10

                    base_offset = struct.unpack_from("<I", meta, rec_off + 0x128)[0]
                    stream0_size = struct.unpack_from("<I", meta, rec_off + 0x130)[0]
                    vertex_count = struct.unpack_from("<I", meta, rec_off + 0x13c)[0]
                    vertex_count_2 = struct.unpack_from("<I", meta, rec_off + 0x140)[0]
                    index_offset, index_size, range_kind, range_extra = struct.unpack_from(
                        "<IIII", meta, range_rec_off)

                    if vertex_count == 0 or vertex_count_2 != vertex_count:
                        continue
                    # Cross-check with array1 stream record when arrays are parallel
                    if array1_count == array2_count and i < array1_count:
                        stream_rec_off = array1_base + i * 0x70
                        stream_vertex_count = struct.unpack_from("<I", meta, stream_rec_off + 0x48)[0]
                        if stream_vertex_count != vertex_count:
                            continue
                    if range_kind not in (2, 4) or range_extra != 0 or index_size == 0:
                        continue

                    index_stride = 4 if range_kind == 4 else 2
                    index_count = index_size
                    result = _extract_cgml_ranges(
                        gpu_data, base_offset, stream0_size, vertex_count,
                        index_count, 28, index_offset, index_stride)
                    if result is not None:
                        submeshes.append(result)

    if not submeshes:
        stream_records = []
        for soff in range(0, len(meta) - 0x20, 0x08):
            vals = [u32(soff + i * 4) for i in range(8)]
            if vals[0] == 4 and vals[2] and vals[4] and vals[5] in (0x2008, 0x2048):
                stream_records.append((vals[2], vals[4], vals[5], soff))

        descriptors = []
        for doff in range(0, len(meta) - 0x40, 4):
            vals = [u32(doff + i * 4) for i in range(14)]
            if vals[0] != 0xFFFFFF0C or vals[1] != 0xFFFFFFFF:
                continue
            if vals[2] not in (0x0B, 0x0D) or vals[3] != 0:
                continue
            vertex_count = vals[9]
            if vertex_count == 0 or vals[10] != vertex_count:
                continue
            descriptors.append((doff, vals[4], vals[6], vertex_count))

        used_streams = [False] * len(stream_records)
        for _doff, base_offset, stream0_size, descriptor_vcount in descriptors:
            vertex_count = descriptor_vcount
            stream_index = None
            for si, (record_vcount, _ic, _fmt, _ro) in enumerate(stream_records):
                if not used_streams[si] and record_vcount == descriptor_vcount:
                    stream_index = si
                    break
            if stream_index is None:
                continue
            _rvc, index_count, _fmt, _ro = stream_records[stream_index]
            result = _extract_cgml_ranges(gpu_data, base_offset, stream0_size,
                                          vertex_count, index_count, 28)
            if result is None:
                continue
            used_streams[stream_index] = True
            submeshes.append(result)

    if not submeshes:
        submeshes.extend(_decode_scan_metadata_fallback(meta, gpu_data))
    if not submeshes:
        submeshes.extend(_decode_zero_tail_kind4_u32_cgml(meta, gpu_data))
    if not submeshes:
        submeshes.extend(_decode_compact_cgml(meta, gpu_data))
    if not submeshes:
        submeshes.extend(_decode_summer_heuristics(meta, gpu_data))

    return submeshes


def _decode_scan_metadata_fallback(meta, gpu_data):
    """Fallback decoder using signature scan and sequential pairing.
    Returns [(verts, faces, uvs), ...] or []."""
    n_meta = len(meta)
    if n_meta < 64:
        return []

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    # 1. Find descriptors
    descriptors = []
    for off in range(0, n_meta - 64 + 1, 4):
        if u32(off) == 0xffffff0c and u32(off + 4) == 0xffffffff:
            kind = u32(off + 8)
            if kind in (11, 13) and u32(off + 12) == 0:
                base_offset = u32(off + 16)
                stream0_size = u32(off + 24)
                vertex_count = u32(off + 36)
                if vertex_count > 0 and u32(off + 40) == vertex_count:
                    descriptors.append({
                        'doff': off,
                        'base_offset': base_offset,
                        'stream0_size': stream0_size,
                        'vertex_count': vertex_count
                    })

    # Sort descriptors by offset in file (original order)
    descriptors = sorted(descriptors, key=lambda d: d['doff'])

    # 2. Find index records
    index_records = []
    for off in range(0, n_meta - 16 + 1, 4):
        idx_off, idx_count, r_kind, r_extra = struct.unpack_from("<IIII", meta, off)
        if r_kind in (2, 4) and r_extra == 0 and idx_count >= 3 and idx_off >= 1024 and idx_off % 2 == 0:
            idx_stride = 2 if r_kind == 2 else 4
            if idx_off + idx_count * idx_stride <= len(gpu_data):
                index_records.append({
                    'ioff': off,
                    'idx_off': idx_off,
                    'idx_count': idx_count,
                    'idx_stride': idx_stride
                })

    # Sort index records by offset in file
    index_records = sorted(index_records, key=lambda ir: ir['ioff'])

    if not descriptors or not index_records:
        return []

    submeshes = []
    # Pair them by order
    for idx in range(min(len(descriptors), len(index_records))):
        desc = descriptors[idx]
        irec = index_records[idx]

        vertex_count = desc['vertex_count']
        index_count = irec['idx_count']
        index_offset = irec['idx_off']
        index_stride = irec['idx_stride']
        base_offset = desc['base_offset']
        stream0_size = desc['stream0_size']

        result = _extract_cgml_ranges(
            gpu_data, base_offset, stream0_size, vertex_count,
            index_count, 28, index_offset, index_stride
        )
        if result is not None:
            submeshes.append(result)

    return submeshes


def _decode_summer_heuristics(meta, gpu_data):
    n_meta = len(meta)
    if n_meta < 16:
        return []

    def u32(off):
        if off < 0 or off + 4 > n_meta:
            return 0
        return struct.unpack_from("<I", meta, off)[0]

    # 1. Collect submesh index candidates
    submesh_candidates = []
    for off in range(0, n_meta - 16, 4):
        idx_off, idx_count, r_kind, r_extra = struct.unpack_from("<IIII", meta, off)
        if r_kind in (2, 4) and r_extra == 0 and idx_count >= 3 and idx_off >= 1024 and idx_off % 2 == 0:
            idx_stride = 2 if r_kind == 2 else 4
            if idx_off + idx_count * idx_stride <= len(gpu_data):
                submesh_candidates.append((idx_off, idx_count, idx_stride))

    if not submesh_candidates:
        return []

    # Keep GPU/index-buffer order. Material mappings follow resource order on
    # multi-part models; LOD trimming is handled later by the importer.
    submesh_candidates = sorted(submesh_candidates, key=lambda x: x[0])

    # 2. Collect potential vertex counts
    vcount_candidates = []
    for off in range(0, n_meta - 4, 4):
        val = u32(off)
        if 10 <= val <= 65535:
            vcount_candidates.append(val)
    vcount_candidates = sorted(list(set(vcount_candidates)))

    # 3. Collect potential vertex offsets from metadata values
    vo_candidates = [0]
    for off in range(0, n_meta - 4, 4):
        val = u32(off)
        if val < len(gpu_data) and val % 4 == 0:
            vo_candidates.append(val)
    vo_candidates = sorted(list(set(vo_candidates)))

    # Finest submesh (LOD 0) info
    idx_off0, idx_count0, idx_stride0 = submesh_candidates[0]
    idxs0 = struct.unpack_from(f"<{idx_count0}H" if idx_stride0 == 2 else f"<{idx_count0}I", gpu_data, idx_off0)
    valid_idxs0 = [x for x in idxs0 if x != 65535 and x != 4294967295]
    if not valid_idxs0:
        return []
    min_vc0 = max(valid_idxs0) + 1

    vc0_candidates = [v for v in vcount_candidates if v >= min_vc0]
    if min_vc0 not in vc0_candidates:
        vc0_candidates.append(min_vc0)
    vc0_candidates = sorted(list(set(vc0_candidates)))

    valid_strides = (112, 108, 80, 64, 56, 48, 44, 40, 36, 32, 28, 24, 20, 16, 12)

    def build_faces(idxs):
        faces = []
        current_face = []
        for idx in idxs:
            if idx == 65535 or idx == 4294967295:
                current_face = []
                continue
            current_face.append(idx)
            if len(current_face) == 3:
                faces.append(tuple(current_face))
                current_face = []
        return faces

    def extract_uvs(vertex_offset, vertex_stride, vertex_count, pos_off):
        uv_off = 20 if pos_off == 0 and vertex_stride >= 28 else None
        if uv_off is None or uv_off + 8 > vertex_stride:
            return [(0.0, 0.0)] * vertex_count

        uvs = []
        valid_count = 0
        for i in range(vertex_count):
            off = vertex_offset + i * vertex_stride + uv_off
            u, v = struct.unpack_from("<ff", gpu_data, off)
            if math.isfinite(u) and math.isfinite(v) and -16.0 <= u <= 16.0 and -16.0 <= v <= 16.0:
                valid_count += 1
                uvs.append((u, v))
            else:
                uvs.append((0.0, 0.0))
        if valid_count < max(3, vertex_count // 4):
            return [(0.0, 0.0)] * vertex_count
        return uvs

    def score_preceding_layout(idx_off, idx_count, idx_stride, vertex_count, vertex_stride, pos_off):
        vertex_offset = idx_off - vertex_count * vertex_stride
        if vertex_offset < 0 or vertex_offset + vertex_count * vertex_stride != idx_off:
            return None

        idxs = struct.unpack_from(
            f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I",
            gpu_data, idx_off)
        valid_idxs = [x for x in idxs if x != 65535 and x != 4294967295]
        if not valid_idxs or max(valid_idxs) >= vertex_count:
            return None

        indexed_verts = []
        for idx in valid_idxs:
            off = vertex_offset + idx * vertex_stride + pos_off
            if off + 12 > len(gpu_data):
                return None
            x, y, z = struct.unpack_from("<fff", gpu_data, off)
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                return None
            if max(abs(x), abs(y), abs(z)) > 10000.0:
                return None
            indexed_verts.append((x, y, z))

        spans = [max(v[a] for v in indexed_verts) - min(v[a] for v in indexed_verts)
                 for a in range(3)]
        if sum(1 for s in spans if s > 0.01) < 2:
            return None
        if max(spans) > 1000.0:
            return None

        # Prefer conventional XYZ-at-record-start layouts, exact vertex counts,
        # and wider records when several interpretations are otherwise valid.
        min_vc = max(valid_idxs) + 1
        score = (pos_off, abs(vertex_count - min_vc), -vertex_stride)
        return score

    # Strong layout: each index buffer is immediately preceded by its matching
    # vertex stream. Some Summer CGInstancedModelResource files mix strides
    # across LODs, so the older global-stride search can borrow the wrong stream.
    preceding_layout = []
    for idx_off, idx_count, idx_stride in submesh_candidates:
        idxs = struct.unpack_from(
            f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I",
            gpu_data, idx_off)
        valid_idxs = [x for x in idxs if x != 65535 and x != 4294967295]
        if not valid_idxs:
            preceding_layout = []
            break
        min_vc = max(valid_idxs) + 1
        candidate_vcounts = [v for v in vcount_candidates if min_vc <= v <= min_vc + 512]
        if min_vc not in candidate_vcounts:
            candidate_vcounts.append(min_vc)

        best = None
        for vc in sorted(set(candidate_vcounts)):
            for vs in valid_strides:
                if idx_off - vc * vs < 0:
                    continue
                for p_off in (0, 4, 8, 12, 16, 20, 24, 28, 32, 40):
                    if p_off + 12 > vs:
                        continue
                    score = score_preceding_layout(idx_off, idx_count, idx_stride, vc, vs, p_off)
                    if score is None:
                        continue
                    candidate = (score, idx_off - vc * vs, vs, vc, p_off,
                                 idx_off, idx_count, idx_stride)
                    if best is None or candidate[0] < best[0]:
                        best = candidate
        if best is None:
            preceding_layout = []
            break
        _score, vo, vs, vc, p_off, idx_off, idx_count, idx_stride = best
        preceding_layout.append((vo, vs, vc, p_off, idx_off, idx_count, idx_stride))

    if preceding_layout:
        submeshes = []
        for vo, vs, vc, p_off, idx_off, idx_count, idx_stride in preceding_layout:
            verts = []
            for i in range(vc):
                off = vo + i * vs + p_off
                x, y, z = struct.unpack_from("<fff", gpu_data, off)
                verts.append((x, y, z))
            uvs = extract_uvs(vo, vs, vc, p_off)

            sub_idxs = struct.unpack_from(
                f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I",
                gpu_data, idx_off)
            submeshes.append((verts, build_faces(sub_idxs), uvs))
        return submeshes

    found_layout = None

    # Method A: Try to deduce stride using index_offset - vertex_offset formula
    for vo0 in vo_candidates:
        for vc0 in vc0_candidates:
            stride_size = idx_off0 - vo0
            if stride_size <= 0:
                continue
            if stride_size % vc0 == 0:
                vs = stride_size // vc0
                if vs in valid_strides:
                    for p_off in (0, 12, 16, 20, 24, 28, 32, 40):
                        if p_off + 12 > vs:
                            continue
                        all_ok = True
                        decoded_submeshes = []
                        for idx_off, idx_count, idx_stride in submesh_candidates:
                            sub_idxs = struct.unpack_from(f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I", gpu_data, idx_off)
                            sub_valid = [x for x in sub_idxs if x != 65535 and x != 4294967295]
                            if not sub_valid:
                                all_ok = False
                                break
                            sub_min_vc = max(sub_valid) + 1
                            
                            sub_vo = None
                            sub_vc = None
                            for vo in vo_candidates:
                                for vc in vcount_candidates:
                                    if vc >= sub_min_vc and vo + vc * vs <= len(gpu_data):
                                        ok = True
                                        for idx in sub_valid:
                                            off = vo + idx * vs + p_off
                                            x, y, z = struct.unpack_from("<fff", gpu_data, off)
                                            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                                                ok = False
                                                break
                                            if max(abs(x), abs(y), abs(z)) > 10000.0:
                                                ok = False
                                                break
                                        if ok:
                                            sub_vo = vo
                                            sub_vc = vc
                                            break
                                if sub_vo is not None:
                                    break
                            if sub_vo is None:
                                all_ok = False
                                break
                            decoded_submeshes.append((sub_vo, vs, sub_vc, p_off, idx_off, idx_count, idx_stride))
                        if all_ok:
                            found_layout = decoded_submeshes
                            break
                if found_layout:
                    break
        if found_layout:
            break

    # Method B: Fallback - Systematic search over all strides (descending) and position offsets
    if not found_layout:
        for vs in valid_strides:
            for p_off in (0, 12, 16, 20, 24, 28, 32, 40):
                if p_off + 12 > vs:
                    continue
                all_ok = True
                decoded_submeshes = []
                for idx_off, idx_count, idx_stride in submesh_candidates:
                    sub_idxs = struct.unpack_from(f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I", gpu_data, idx_off)
                    sub_valid = [x for x in sub_idxs if x != 65535 and x != 4294967295]
                    if not sub_valid:
                        all_ok = False
                        break
                    sub_min_vc = max(sub_valid) + 1
                    
                    sub_vo = None
                    sub_vc = None
                    for vo in vo_candidates:
                        for vc in vcount_candidates:
                            if vc >= sub_min_vc and vo + vc * vs <= len(gpu_data):
                                ok = True
                                for idx in sub_valid:
                                    off = vo + idx * vs + p_off
                                    x, y, z = struct.unpack_from("<fff", gpu_data, off)
                                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                                        ok = False
                                        break
                                    if max(abs(x), abs(y), abs(z)) > 10000.0:
                                        ok = False
                                        break
                                if ok:
                                    sub_vo = vo
                                    sub_vc = vc
                                    break
                        if sub_vo is not None:
                            break
                    if sub_vo is None:
                        all_ok = False
                        break
                    decoded_submeshes.append((sub_vo, vs, sub_vc, p_off, idx_off, idx_count, idx_stride))
                if all_ok:
                    found_layout = decoded_submeshes
                    break
            if found_layout:
                break

    if not found_layout:
        return []

    # Reconstruct meshes for return
    submeshes = []
    for vo, vs, vc, p_off, idx_off, idx_count, idx_stride in found_layout:
        # Extract vertices
        verts = []
        for i in range(vc):
            off = vo + i * vs + p_off
            x, y, z = struct.unpack_from("<fff", gpu_data, off)
            verts.append((x, y, z))
        uvs = extract_uvs(vo, vs, vc, p_off)

        sub_idxs = struct.unpack_from(f"<{idx_count}H" if idx_stride == 2 else f"<{idx_count}I", gpu_data, idx_off)
        submeshes.append((verts, build_faces(sub_idxs), uvs))

    return submeshes



# ============================================================
# GPU-only heuristic paths
# ============================================================

def _extract_dual28_prefixed_mesh(data):
    """28+28+u16 layout: block-0 has fixed prefix pair, block-1 has XYZ."""
    Nv = _find_prefix_pair_run(
        data, stride=28,
        prefix_pairs={(0xFFFFFFFF, 0xFFFFFFFF), (0xFF000000, 0xFFFFFFFF)},
    )
    if Nv == 0:
        return None

    s1_start = Nv * 28
    ib_start = s1_start + Nv * 28
    if ib_start >= len(data):
        return None
    if (len(data) - ib_start) % 2 != 0:
        return None

    verts = []
    for j in range(Nv):
        off = s1_start + j * 28
        x, y, z = struct.unpack_from("<fff", data, off)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            return None
        if abs(x) > 50000 or abs(y) > 50000 or abs(z) > 50000:
            return None
        verts.append((x, y, z))

    idx_words = (len(data) - ib_start) // 2
    if idx_words < 3:
        return None

    idxs = struct.unpack_from(f"<{idx_words}H", data, ib_start)
    if any(idx >= Nv for idx in idxs):
        return None

    faces = []
    for k in range(0, idx_words - idx_words % 3, 3):
        i0, i1, i2 = idxs[k:k + 3]
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        faces.append((i0, i1, i2))
    if not faces:
        return None
    return [(verts, faces)]


def _extract_multi28_suffix_mesh(data):
    """Larger 28-byte-fronted CIMR family: checks for a strict inner
    28+28+u16 payload first, then falls back to the clean tail index suffix."""
    Nv = _find_prefix_pair_run(
        data, stride=28,
        prefix_pairs={(0xFFFFFFFF, 0xFFFFFFFF), (0xFF000000, 0xFFFFFFFF)},
    )
    if Nv == 0:
        return None

    block_size = Nv * 28
    full_blocks = len(data) // block_size
    if full_blocks < 4:
        return None

    s1_start = block_size
    s1_end = s1_start + block_size
    if s1_end > len(data):
        return None

    verts = []
    for j in range(Nv):
        off = s1_start + j * 28
        x, y, z = struct.unpack_from("<fff", data, off)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            return None
        if abs(x) > 50000 or abs(y) > 50000 or abs(z) > 50000:
            return None
        verts.append((x, y, z))

    tail = data[full_blocks * block_size:]
    if len(tail) < 192 or (len(tail) % 2) != 0:
        return None

    best_tail_dual28 = None
    for start in range(0, len(tail) - 84, 4):
        inner = _extract_dual28_prefixed_mesh(tail[start:])
        if not inner:
            continue
        verts2, faces2 = inner[0]
        if len(faces2) < 24:
            continue
        candidate = (len(faces2), len(verts2), verts2, faces2)
        if best_tail_dual28 is None or candidate[:2] > best_tail_dual28[:2]:
            best_tail_dual28 = candidate

    if best_tail_dual28 is not None:
        return [(best_tail_dual28[2], best_tail_dual28[3])]

    halfwords = struct.unpack(f"<{len(tail) // 2}H", tail)
    start_half = None
    for start in range(len(halfwords)):
        suffix = halfwords[start:]
        if len(suffix) < 96:
            break
        if all(idx < Nv or idx == 0xFFFF for idx in suffix):
            start_half = start
            break
    if start_half is None:
        return None

    suffix_bytes = len(tail) - start_half * 2
    if suffix_bytes > 1024:
        return None

    idxs = halfwords[start_half:]
    faces = []
    for k in range(0, len(idxs) - len(idxs) % 3, 3):
        i0, i1, i2 = idxs[k:k + 3]
        if 0xFFFF in (i0, i1, i2):
            continue
        if i0 >= Nv or i1 >= Nv or i2 >= Nv:
            return None
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        faces.append((i0, i1, i2))
    if not faces:
        return None
    return [(verts, faces)]


def _extract_small20_tail_mesh(data):
    """Decode the specific small 20-byte layout (5736-byte files only)."""
    stride = 20
    Nv = 110
    tail_start = 5280

    if len(data) != 5736:
        return None

    allowed_b = {0xFFFFFFFF, 0xFF000000, 0xFFCBCBCB, 0xFFCCCCCC}
    verts = []
    for j in range(Nv):
        off = j * stride
        a, b, x, y, packed = struct.unpack_from("<IIffI", data, off)
        if a != 0xFFFFFFFF or b not in allowed_b:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        if abs(x) > 10 or abs(y) > 10:
            return None
        hi = (packed >> 16) & 0xFFFF
        shi = hi - 0x10000 if hi >= 0x8000 else hi
        verts.append((x, y, shi / 32767.0))

    tail = data[tail_start:]
    if len(tail) != 456 or (len(tail) % 2) != 0:
        return None

    idxs = struct.unpack(f"<{len(tail) // 2}H", tail)
    if max(idxs) >= Nv:
        return None

    faces = []
    for k in range(0, len(idxs) - len(idxs) % 3, 3):
        i0, i1, i2 = idxs[k:k + 3]
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        faces.append((i0, i1, i2))
    if len(faces) < 24:
        return None
    return [(verts, faces)]


def _extract_large20_suffix_mesh(data):
    """Large 20-byte-fronted CIMR family: find final clean u16 suffix and
    match it to a compact 28-byte XYZ block just before the suffix."""
    stride = 20
    lead_nv = _find_prefix_pair_run(
        data, stride=stride,
        prefix_pairs={(0xFFFFFFFF, 0xFFFFFFFF)},
        min_records=128,
    )
    if lead_nv == 0:
        return None
    if len(data) < lead_nv * stride + 1024:
        return None

    halfword_count = len(data) // 2
    halfwords = struct.unpack(f"<{halfword_count}H", data[:halfword_count * 2])

    start_half = None
    idxs = None
    search_start = max(0, len(halfwords) - 4096)
    for start in range(search_start, len(halfwords)):
        suffix = halfwords[start:]
        if len(suffix) < 96:
            break
        if all(idx < lead_nv or idx == 0xFFFF for idx in suffix):
            start_half = start
            idxs = suffix
            break
    if idxs is None:
        return None

    if len(data) - start_half * 2 > 2048:
        return None

    used = sorted({idx for idx in idxs if idx != 0xFFFF})
    if len(used) < 128:
        return None
    if used != list(range(used[-1] + 1)):
        return None
    used_nv = used[-1] + 1

    def read_xyz_block(start, xyz_stride):
        verts = []
        for j in range(used_nv):
            off = start + j * xyz_stride
            if off + 12 > len(data):
                return None
            x, y, z = struct.unpack_from("<fff", data, off)
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                return None
            if max(abs(x), abs(y), abs(z)) > 1000:
                return None
            verts.append((x, y, z))
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        if max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) > 4.0:
            return None
        if sum(1 for span in (max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs))
               if span < 0.02) > 1:
            return None
        return verts

    verts = None
    tail_start = start_half * 2
    preferred_start = tail_start - used_nv * 28
    if preferred_start >= 0:
        verts = read_xyz_block(preferred_start, 28)

    if verts is None:
        search_min = max(0, preferred_start - 16384)
        for start in range(preferred_start, search_min - 1, -4):
            verts = read_xyz_block(start, 28)
            if verts is not None:
                break

    if verts is None:
        verts = []
        for j in range(used_nv):
            off = j * stride
            a, b, x, y, _packed = struct.unpack_from("<IIffI", data, off)
            if a != 0xFFFFFFFF or b != 0xFFFFFFFF:
                return None
            if not (math.isfinite(x) and math.isfinite(y)):
                return None
            if abs(x) > 64 or abs(y) > 64:
                return None
            verts.append((x, 0.0, -y))
        xs = [v[0] for v in verts]
        zs = [v[2] for v in verts]
        if (max(xs) - min(xs)) < 16 or (max(zs) - min(zs)) < 4:
            return None

    faces = []
    usable = len(idxs) - (len(idxs) % 3)
    for k in range(0, usable, 3):
        i0, i1, i2 = idxs[k:k + 3]
        if 0xFFFF in (i0, i1, i2):
            continue
        if i0 >= used_nv or i1 >= used_nv or i2 >= used_nv:
            return None
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        faces.append((i0, i1, i2))
    if len(faces) < 64:
        return None
    return [(verts, faces)]


def _extract_any_color_s16_mesh(data, min_run=40):
    """Stride-16 stream-0 (any vertex color) + stride-28 stream-1 + u16 IB."""
    n = len(data)
    best_result = None
    best_tris = 0

    runs = []
    i = 0
    while i < n - 15:
        cnt = 0
        j = i
        while j + 15 < n:
            x = struct.unpack_from('<f', data, j + 4)[0]
            y = struct.unpack_from('<f', data, j + 8)[0]
            if not (math.isfinite(x) and math.isfinite(y)
                    and abs(x) < 10000 and abs(y) < 10000):
                break
            cnt += 1
            j += 16
        if cnt >= min_run:
            runs.append((i, cnt))
            i = j
        else:
            i += 4

    for s0_start, run_nv in runs:
        for nv in (run_nv, run_nv - 1):
            if nv < 4:
                continue
            s1_start = s0_start + nv * 16
            ib_start = s1_start + nv * 28
            if ib_start >= n:
                continue

            n_ib = 0
            ib_cap = min(n, ib_start + nv * 8 * 2)
            for k in range(ib_start, ib_cap, 2):
                if struct.unpack_from('<H', data, k)[0] < nv:
                    n_ib += 1
                else:
                    break
            n_ib -= n_ib % 3
            tris = n_ib // 3
            if tris < 1 or tris <= best_tris:
                continue

            for pos_off in (0, 4, 8, 12):
                verts = []
                valid = True
                for j in range(nv):
                    off = s1_start + j * 28 + pos_off
                    if off + 12 > n:
                        valid = False
                        break
                    x, y, z = struct.unpack_from('<fff', data, off)
                    if not (math.isfinite(x) and math.isfinite(y)
                            and math.isfinite(z)):
                        valid = False
                        break
                    if max(abs(x), abs(y), abs(z)) > 50000:
                        valid = False
                        break
                    verts.append((x, y, z))
                if not valid or len(verts) != nv:
                    continue

                faces = []
                for k in range(0, n_ib, 3):
                    i0, i1, i2 = struct.unpack_from('<HHH', data, ib_start + k * 2)
                    if i0 != i1 and i1 != i2 and i0 != i2:
                        faces.append((i0, i1, i2))

                best_tris = tris
                best_result = (verts, faces)
                break

    return [best_result] if best_result else []


def _extract_leading_any_color_s16_mesh(data, min_run=40):
    """Leading-stream variant: stride-16 from offset 0, any vertex color."""
    n = len(data)
    run_nv = 0
    while run_nv * 16 + 15 < n:
        off = run_nv * 16
        x = struct.unpack_from("<f", data, off + 4)[0]
        y = struct.unpack_from("<f", data, off + 8)[0]
        if not (math.isfinite(x) and math.isfinite(y)):
            break
        if max(abs(x), abs(y)) > 10000:
            break
        run_nv += 1
    if run_nv < min_run:
        return []

    best = None
    for nv in range(run_nv, max(run_nv - 4, min_run - 1), -1):
        s1_start = nv * 16
        ib_start = s1_start + nv * 28
        if ib_start >= n:
            continue
        idx_count = 0
        ib_cap = min(n, ib_start + nv * 8 * 2)
        for ib_off in range(ib_start, ib_cap, 2):
            if struct.unpack_from("<H", data, ib_off)[0] >= nv:
                break
            idx_count += 1
        idx_count -= idx_count % 3
        if idx_count < 3:
            continue
        idxs = struct.unpack_from(f"<{idx_count}H", data, ib_start)
        faces = [(idxs[k], idxs[k+1], idxs[k+2]) for k in range(0, idx_count, 3)
                 if idxs[k] != idxs[k+1] and idxs[k+1] != idxs[k+2]
                 and idxs[k] != idxs[k+2]]
        if not faces:
            continue
        for pos_off in (0, 4, 8, 12, 16):
            verts = []
            valid = True
            for j in range(nv):
                off = s1_start + j * 28 + pos_off
                if off + 12 > n:
                    valid = False
                    break
                x, y, z = struct.unpack_from("<fff", data, off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    valid = False
                    break
                if max(abs(x), abs(y), abs(z)) > 50000:
                    valid = False
                    break
                verts.append((x, y, z))
            if not valid:
                continue
            candidate = (len(faces), len(verts), verts, faces)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
            break

    if best is None:
        return []
    return [(best[2], best[3])]


def _extract_any_color_s20_mesh(data, min_run=128):
    """4-byte header + stride-20 stream-0 (any color) + stride-28 stream-1 + u16 IB.
    Late fallback for visually verified missing CIMRs."""
    n = len(data)
    best = None

    s0_start = 4
    cnt = 0
    j = s0_start
    while j + 19 < n:
        x = struct.unpack_from("<f", data, j + 4)[0]
        y = struct.unpack_from("<f", data, j + 8)[0]
        if not (math.isfinite(x) and math.isfinite(y)):
            break
        if max(abs(x), abs(y)) > 10000:
            break
        cnt += 1
        j += 20
    if cnt < min_run:
        return []

    for nv in (cnt, cnt - 1):
        if nv < min_run:
            continue
        s1_start = s0_start + nv * 20
        ib_start = s1_start + nv * 28
        if ib_start >= n:
            continue

        idx_count = 0
        max_ib = min(n, ib_start + nv * 24 * 2)
        for ib_off in range(ib_start, max_ib, 2):
            if struct.unpack_from("<H", data, ib_off)[0] >= nv:
                break
            idx_count += 1
        idx_count -= idx_count % 3
        if idx_count < 192:
            continue

        idxs = struct.unpack_from(f"<{idx_count}H", data, ib_start)
        faces = []
        degenerate = 0
        for k in range(0, idx_count, 3):
            i0, i1, i2 = idxs[k:k + 3]
            if i0 == i1 or i1 == i2 or i0 == i2:
                degenerate += 1
                continue
            faces.append((i0, i1, i2))
        if len(faces) < 64 or degenerate > len(faces):
            continue

        for pos_off in (4, 8, 12, 16, 0):
            verts = []
            valid = True
            for j2 in range(nv):
                off = s1_start + j2 * 28 + pos_off
                if off + 12 > n:
                    valid = False
                    break
                x, y, z = struct.unpack_from("<fff", data, off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    valid = False
                    break
                if max(abs(x), abs(y), abs(z)) > 50000:
                    valid = False
                    break
                verts.append((x, y, z))
            if not valid:
                continue
            spans = [max(v[a] for v in verts) - min(v[a] for v in verts) for a in range(3)]
            if sum(1 for s in spans if s > 0.02) < 2:
                continue
            bbox_score = spans[0]*spans[1] + spans[0]*spans[2] + spans[1]*spans[2]
            score = (len(faces), bbox_score, nv)
            if best is None or score > best[0]:
                best = (score, verts, faces)

    if best is None:
        return []
    return [(best[1], best[2])]


# ============================================================
# Main extraction entry point
# ============================================================

def extract_mesh(gpu_filepath, primary_data=None, auto_find_primary=True):
    """Return (submeshes, path_label) for the given GPU binary.

    *primary_data* — raw bytes of the matching Primary file if already loaded.
    *auto_find_primary* — if True and primary_data is None, auto-discover via
        the _find_primary_data convention (import from primary module).

    submeshes: [(verts, faces), ...]
        verts = [(x, y, z), ...]
        faces = [(i0, i1, i2), ...]   (0-based triangle list)

    path_label: one of "primary_described", "crossref_ib", "hero", "cgml", "heuristic"
    """
    with open(gpu_filepath, "rb") as fh:
        data = fh.read()

    if primary_data is None and auto_find_primary:
        from .primary import _find_primary_data
        primary_data = _find_primary_data(gpu_filepath)

    if primary_data:
        result = _extract_primary_described_cimr_mesh(data, primary_data)
        if result:
            return result, "primary_described"

        result = _extract_metadata_meshes(data, primary_data)
        if result:
            return result, "cgml"

        crossref = _extract_crossref_ib_cimr_mesh(data, primary_data)
        hero = _extract_hero_cimr_mesh(data, primary_data)
        crossref_tris = sum(len(sub[1]) for sub in crossref)
        hero_tris = sum(len(sub[1]) for sub in hero)
        if crossref_tris > hero_tris and crossref:
            return crossref, "crossref_ib"
        if hero:
            return hero, "hero"

    result = _extract_dual28_prefixed_mesh(data)
    if result:
        return result, "heuristic"

    result = _extract_multi28_suffix_mesh(data)
    if result:
        return result, "heuristic"

    result = _extract_small20_tail_mesh(data)
    if result:
        return result, "heuristic"

    result = _extract_large20_suffix_mesh(data)
    if result:
        return result, "heuristic"

    n = len(data)
    submeshes = []
    covered_ranges = []

    def overlaps(s, e):
        return any(cs < e and s < ce for cs, ce in covered_ranges)

    for stride in (52, 36, 20, 16):
        for s0_start, Nv in _find_runs(data, stride):
            if stride in (36, 52) and Nv < 6:
                continue
            s0_end = s0_start + Nv * stride
            s1_end = s0_end + Nv * 28
            if s1_end > n or overlaps(s0_start, s1_end):
                continue
            sub = _extract_submesh(data, s0_start, Nv, stride)
            if sub is None or not sub[1]:
                continue
            covered_ranges.append((s0_start, s1_end))
            submeshes.append(sub)

    for s0_start, Nv in _find_marker4_runs(data, stride=16):
        s0_end = s0_start + Nv * 16
        s1_end = s0_end + Nv * 28
        if s1_end > n or overlaps(s0_start, s1_end):
            continue
        sub = _extract_submesh(data, s0_start, Nv, 16)
        if sub is None or not sub[1]:
            continue
        covered_ranges.append((s0_start, s1_end))
        submeshes.append(sub)

    for s0_start, Nv in _find_vertex_colored_runs(data, stride=20):
        s0_end = s0_start + Nv * 20
        s1_end = s0_end + Nv * 28
        if s1_end > n or overlaps(s0_start, s1_end):
            continue
        sub = _extract_submesh(data, s0_start, Nv, 20)
        if sub is None or not sub[1]:
            continue
        covered_ranges.append((s0_start, s1_end))
        submeshes.append(sub)

    any_s16 = _extract_any_color_s16_mesh(data)
    leading_s16 = _extract_leading_any_color_s16_mesh(data)
    best_s16 = (any_s16 if sum(len(sub[1]) for sub in any_s16) >=
                sum(len(sub[1]) for sub in leading_s16) else leading_s16)
    if best_s16:
        s16_tris = sum(len(sub[1]) for sub in best_s16)
        cur_tris = sum(len(sub[1]) for sub in submeshes)
        if s16_tris > cur_tris:
            submeshes = best_s16

    if not submeshes:
        submeshes.extend(_extract_any_color_s20_mesh(data))

    return submeshes, "heuristic"
