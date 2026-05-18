"""
EVR Raw Mesh Importer — encode module
Writes replacement GPU binaries in the layouts Echo VR expects.

Stream-1 is 28 bytes per vertex. The layout when XYZ is at offset 0:
  [0 -11]  float32 x3     — position XYZ
  [12-13]  snorm16 x2     — packed normal XY  (derived from face normals)
  [14-15]  snorm16 x2     — packed normal Z + sign, or tangent sign
  [16-23]  snorm16 x4     — tangent XYZW (W = handedness, ±1)
  [24-25]  float16 / u16  — UV.u
  [26-27]  float16 / u16  — UV.v

The game almost certainly reads normals and tangents for lighting. We compute
smooth vertex normals from face topology and pack them into snorm16. Tangents
are approximated as a frame aligned to the normal. UVs are zeroed — replacing
UV mapping requires a more involved pipeline.

https://github.com/Dualgame/evr-mesh-importer
"""

import struct
import math


# ============================================================
# Math helpers
# ============================================================

def _normalize(v):
    x, y, z = v
    d = math.sqrt(x*x + y*y + z*z)
    if d < 1e-12:
        return (0.0, 0.0, 1.0)
    return (x/d, y/d, z/d)


def _cross(a, b):
    ax, ay, az = a
    bx, by, bz = b
    return (ay*bz - az*by, az*bx - ax*bz, ax*by - ay*bx)


def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]


def _face_normal(verts, face):
    """Compute the unit normal of a triangle."""
    v0, v1, v2 = [verts[i] for i in face]
    e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
    e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
    return _normalize(_cross(e1, e2))


def _float_to_snorm16(f):
    """Clamp float in [-1, 1] to signed 16-bit integer."""
    f = max(-1.0, min(1.0, f))
    v = int(round(f * 32767.0))
    return max(-32768, min(32767, v))


def _float_to_float16(f):
    """Pack a float32 to float16 bits (as uint16)."""
    # Use struct for correct IEEE half conversion
    return struct.unpack('<H', struct.pack('<e', f))[0]


# ============================================================
# Normal / tangent computation
# ============================================================

def _compute_smooth_normals(verts, faces):
    """
    Per-vertex smooth normals by area-weighted face normal accumulation.
    Returns list of (nx, ny, nz) unit vectors, one per vertex.
    """
    normals = [[0.0, 0.0, 0.0] for _ in verts]
    for face in faces:
        fn = _face_normal(verts, face)
        # Weight by triangle area (proportional to cross product magnitude,
        # already captured via non-normalized normal before _normalize)
        v0, v1, v2 = [verts[i] for i in face]
        e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
        e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
        cx = e1[1]*e2[2] - e1[2]*e2[1]
        cy = e1[2]*e2[0] - e1[0]*e2[2]
        cz = e1[0]*e2[1] - e1[1]*e2[0]
        area = math.sqrt(cx*cx + cy*cy + cz*cz)  # 2× triangle area
        for idx in face:
            normals[idx][0] += fn[0] * area
            normals[idx][1] += fn[1] * area
            normals[idx][2] += fn[2] * area
    return [_normalize(tuple(n)) for n in normals]


def _make_tangent_frame(normal):
    """
    Build an arbitrary but consistent tangent (T) and bitangent (B) from a normal.
    Returns (tangent_xyz, handedness) where handedness is +1.0 or -1.0.
    The tangent is perpendicular to the normal and non-degenerate.
    """
    nx, ny, nz = normal
    # Choose the axis least aligned with the normal to avoid degeneracy
    if abs(nx) <= abs(ny) and abs(nx) <= abs(nz):
        ref = (1.0, 0.0, 0.0)
    elif abs(ny) <= abs(nz):
        ref = (0.0, 1.0, 0.0)
    else:
        ref = (0.0, 0.0, 1.0)

    # T = normalize(ref - (ref·N)N)
    d = _dot(ref, normal)
    t = _normalize((ref[0] - d*nx, ref[1] - d*ny, ref[2] - d*nz))
    # Handedness from cross product sign
    b = _cross(normal, t)
    handedness = 1.0 if _dot(b, b) > 0 else -1.0
    return t, handedness


# ============================================================
# Stream packers
# ============================================================

def _pack_stream0_s16(vertex_count, uvs=None):
    """
    Stream-0 stride-16: most common CIMR pattern.
    Records are 0xFFFFFFFF 0x00000000 [UV.u:float32] [UV.v:float32]
    """
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        out += struct.pack('<IIff', 0xFFFFFFFF, 0x00000000, u, v)
    return bytes(out)


def _pack_stream0_s20_white(vertex_count, uvs=None):
    """
    Stream-0 stride-20: vertex-colored variant, all-white (0xFFFFFFFF RGBA).
    Layout: 0xFF000000 0xFFFFFFFF [UV.u:float32] [UV.v:float32] 0x00000000
    """
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        out += struct.pack('<IIffI', 0xFF000000, 0xFFFFFFFF, u, v, 0)
    return bytes(out)


def _pack_stream0_s28_ff(vertex_count):
    """
    Stream-0 stride-28: for dual-28 layout (both streams same stride).
    First two dwords = 0xFFFFFFFF, rest zeroed.
    Detected by _find_prefix_pair_run.
    """
    record = struct.pack('<II', 0xFFFFFFFF, 0xFFFFFFFF) + b'\x00' * 20
    return record * vertex_count


def _pack_stream0_dynamic(vertex_count, stride, uvs=None):
    """
    Dynamically pack stream-0 to match the required stride perfectly.
    Supports standard 16, 20, and extended strides (e.g. 44 for props).
    """
    if stride == 16:
        return _pack_stream0_s16(vertex_count, uvs=uvs)
    elif stride == 20:
        return _pack_stream0_s20_white(vertex_count, uvs=uvs)

    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        # Start with standard double-color prefix and UVs
        record = bytearray(struct.pack('<IIff', 0xFF000000, 0xFF000000, u, v))
        # If there's space, repeat UVs (common in 44-stride props)
        if stride >= 24:
            record += struct.pack('<ff', u, v)
        # Pad the rest of the record to match stride
        if len(record) < stride:
            record += b'\x00' * (stride - len(record))
        elif len(record) > stride:
            record = record[:stride]
        out += record
    return bytes(out)


def _pack_stream1_with_normals(verts, normals):
    """
    Stream-1 stride-28 per vertex, XYZ at offset 0:
      [ 0-11] float32 x3  — position XYZ
      [12-13] snorm16     — normal.x
      [14-15] snorm16     — normal.y
      [16-17] snorm16     — normal.z
      [18-19] snorm16     — 0 (padding / tangent sign placeholder)
      [20-21] snorm16     — tangent.x
      [22-23] snorm16     — tangent.y
      [24-25] snorm16     — tangent.z
      [26-27] snorm16     — tangent handedness (+1 or -1, packed as ±32767)

    UVs are omitted (zeroed) since we don't have a UV map.
    Total: 12 + 2+2+2+2 + 2+2+2+2 = 28 bytes ✓
    """
    out = bytearray()
    for i, ((x, y, z), (nx, ny, nz)) in enumerate(zip(verts, normals)):
        tangent, handedness = _make_tangent_frame((nx, ny, nz))
        tx, ty, tz = tangent

        out += struct.pack('<fff', x, y, z)
        out += struct.pack('<hhhh',
            _float_to_snorm16(nx),
            _float_to_snorm16(ny),
            _float_to_snorm16(nz),
            0,   # padding
        )
        out += struct.pack('<hhhh',
            _float_to_snorm16(tx),
            _float_to_snorm16(ty),
            _float_to_snorm16(tz),
            _float_to_snorm16(handedness),
        )
    return bytes(out)


def _pack_stream1_zeroed(verts):
    """
    Fallback stream-1 with normals zeroed. Use only if normal computation
    fails or is not needed (e.g. testing geometry first).
    """
    out = bytearray()
    for x, y, z in verts:
        out += struct.pack('<fff', x, y, z)
        out += b'\x00' * 16
    return bytes(out)


def _pack_index_buffer_u16(faces):
    """Triangle list as uint16 words. Pads to 4-byte alignment."""
    out = bytearray()
    for i0, i1, i2 in faces:
        out += struct.pack('<HHH', i0, i1, i2)
    if len(out) % 4:
        out += b'\x00' * (4 - len(out) % 4)
    return bytes(out)


def _pack_index_buffer_u32(faces):
    """Triangle list as uint32 words (for CGML kind-4 path)."""
    out = bytearray()
    for i0, i1, i2 in faces:
        out += struct.pack('<III', i0, i1, i2)
    return bytes(out)


# ============================================================
# Validation helpers
# ============================================================

def _validate_mesh(verts, faces, label="mesh", allow_degenerate=False):
    """Raise ValueError with a clear message if the mesh is invalid."""
    nv = len(verts)
    if nv == 0:
        raise ValueError(f"{label}: no vertices")
    if not faces:
        raise ValueError(f"{label}: no faces")
    if nv > 65535:
        raise ValueError(
            f"{label}: {nv} vertices exceeds uint16 limit (65535). "
            "Decimate the mesh before exporting."
        )
    bad = [(fi, f) for fi, f in enumerate(faces) if any(i >= nv for i in f)]
    if bad:
        raise ValueError(f"{label}: face {bad[0][0]} references index >= {nv}")
    degenerate = sum(1 for f in faces if f[0]==f[1] or f[1]==f[2] or f[0]==f[2])
    if degenerate == len(faces) and not allow_degenerate:
        raise ValueError(f"{label}: all {len(faces)} faces are degenerate")


# ============================================================
# Public encoders
# ============================================================

def encode_heuristic_s16(verts, faces, uvs=None, compute_normals=True):
    """
    Encode a mesh using the heuristic stride-16 stream-0 layout.

    This is the safest encode target — the widest family of CIMR props
    uses this layout, and the heuristic decoder finds it via _find_runs.

    Layout:
      [stream-0] Nv × 16 bytes  (0xFFFFFFFF seed, detected by heuristic)
      [stream-1] Nv × 28 bytes  (XYZ at offset 0, normals packed as snorm16)
      [index buffer] Nt × 6 bytes  (uint16 triangle list)

    Args:
        verts:           [(x, y, z), ...]
        faces:           [(i0, i1, i2), ...]  — must all be < len(verts)
        uvs:             optional [(u, v), ...] UV coordinates
        compute_normals: if True, compute smooth normals from topology.
                         if False, stream-1 normals are zeroed (safe for
                         geometry testing but will look flat/wrong in-game).

    Returns:
        bytes — the complete GPU binary to write over the original file.
    """
    _validate_mesh(verts, faces, "heuristic_s16")
    s0 = _pack_stream0_s16(len(verts), uvs=uvs)
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib


def encode_heuristic_s20(verts, faces, uvs=None, compute_normals=True):
    """
    Encode using the heuristic stride-20 stream-0 layout (vertex-colored family).

    Same as encode_heuristic_s16 but stream-0 uses stride-20 white records.
    Use this when the original file decoded via the vertex-colored heuristic path.

    Returns:
        bytes — the complete GPU binary.
    """
    _validate_mesh(verts, faces, "heuristic_s20")
    s0 = _pack_stream0_s20_white(len(verts), uvs=uvs)
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib


def encode_heuristic_dual28(verts, faces, compute_normals=True):
    """
    Encode using the dual-28 layout (both streams stride-28).

    Use when the original file decoded via _extract_dual28_prefixed_mesh or
    _extract_multi28_suffix_mesh. Stream-0 uses 0xFFFFFFFF 0xFFFFFFFF prefix.

    Note: the decoder requires at least 16 vertices (_find_prefix_pair_run
    min_records=16). Meshes smaller than 16 verts will fail to decode.

    Returns:
        bytes — the complete GPU binary.
    """
    _validate_mesh(verts, faces, "heuristic_dual28")
    s0 = _pack_stream0_s28_ff(len(verts))
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib


def encode_primary_described(verts, faces, uvs=None, stream0_stride=16, compute_normals=True):
    """
    Encode a mesh in the primary_described layout and produce a matching
    Primary binary. Both files must be written for the game to load correctly.

    Layout (base_offset=0):
      GPU:     [stream-0: Nv*s0] [stream-1: Nv*28] [index buffer]
      Primary: [0x0B descriptor block: 16 × uint32]

    Args:
        verts:           [(x, y, z), ...]
        faces:           [(i0, i1, i2), ...]
        uvs:             optional [(u, v), ...] UV coordinates
        stream0_stride:  16 or 20 — must match the original file's s0_stride.
        compute_normals: pack smooth normals into stream-1.

    Returns:
        (gpu_data: bytes, primary_data: bytes)
    """
    _validate_mesh(verts, faces, "primary_described")
    assert stream0_stride in (16, 20), "stream0_stride must be 16 or 20"

    nv = len(verts)
    nt = len(faces)

    if stream0_stride == 16:
        s0 = _pack_stream0_s16(nv, uvs=uvs)
    else:
        s0 = _pack_stream0_s20_white(nv, uvs=uvs)

    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)

    ib = _pack_index_buffer_u16(faces)
    gpu_data = s0 + s1 + ib

    # Build matching Primary descriptor (block type 0x0B, 16 dwords)
    stream0_size = nv * stream0_stride
    index_offset = stream0_size + nv * 28   # immediately after stream-1

    primary_data = struct.pack('<16I',
        0x0B,            # [0]  block type sentinel
        0,               # [1]  padding
        0,               # [2]  base_offset (0 = linear layout)
        0,               # [3]  padding
        stream0_size,    # [4]  stream0_size (tells decoder where s1 starts)
        0,               # [5]  unused
        0,               # [6]  unused
        nv,              # [7]  vertex_count  ← checked: must equal [8] and [11]
        nv,              # [8]  vertex_count copy
        0,               # [9]  unused
        0,               # [10] unused
        nv,              # [11] vertex_count copy
        index_offset,    # [12] index_offset in GPU binary
        nt * 3,          # [13] index_words (number of uint16 indices)
        2,               # [14] range_kind = 2 (the only kind primary_described accepts)
        0,               # [15] unused
    )

    return gpu_data, primary_data


def encode_cgml(submesh_list, compute_normals=True):
    """
    Encode a multi-submesh CGML (map geometry) file.

    Each submesh is encoded as an independent heuristic_s16 block concatenated
    in sequence. The Primary metadata for CGML files is complex and varies per
    map — this encoder targets maps whose Primary uses the compact path
    (_decode_compact_cgml), which reads base offsets from fixed Primary offsets.

    For a full CGML replacement you also need to patch the Primary file.
    See encode_cgml_primary() for that.

    Args:
        submesh_list:    [(verts, faces, uvs), ...] or [(verts, faces), ...]
        compute_normals: pack smooth normals.

    Returns:
        bytes — concatenated GPU binary for all submeshes.
    """
    out = bytearray()
    for i, sub in enumerate(submesh_list):
        if len(sub) == 3:
            verts, faces, uvs = sub
        else:
            verts, faces = sub
            uvs = None
        _validate_mesh(verts, faces, f"cgml submesh {i}")
        out += encode_heuristic_s16(verts, faces, uvs=uvs, compute_normals=compute_normals)
    return bytes(out)


# ============================================================
# Blender mesh extraction
# ============================================================

def mesh_from_blender_object(obj, apply_transforms=True, split_by_material=False):
    """
    Extract (verts, faces) from a Blender mesh object.

    Must be called from within a Blender operator context.

    Args:
        obj:               Blender Object with type == 'MESH'
        apply_transforms:  apply world-space matrix before export
        split_by_material: if True, return [(verts, faces), ...] split per
                           material slot instead of a single merged mesh.
                           Useful for CGML multi-submesh export.

    Returns:
        (verts, faces)  when split_by_material=False
        [(verts, faces), ...]  when split_by_material=True

    Raises:
        ValueError if vertex count exceeds 65535 (uint16 limit).
    """
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    if apply_transforms:
        bm.transform(obj.matrix_world)

    # Triangulate in place — required for triangle-list IB
    bmesh.ops.triangulate(bm, faces=bm.faces[:])

    # Try to get active UV layer
    uv_layer = bm.loops.layers.uv.active

    if not split_by_material:
        verts = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
        faces = [(f.verts[0].index, f.verts[1].index, f.verts[2].index)
                 for f in bm.faces]
        
        uvs = []
        if uv_layer:
            for v in bm.verts:
                uv = (0.0, 0.0)
                if v.link_loops:
                    loop = v.link_loops[0]
                    uv = (loop[uv_layer].uv.x, loop[uv_layer].uv.y)
                uvs.append(uv)
        else:
            uvs = [(0.0, 0.0)] * len(verts)

        bm.free()
        if len(verts) > 65535:
            raise ValueError(
                f"Mesh '{obj.name}' has {len(verts)} vertices. "
                "Decimate below 65536 before exporting."
            )
        return verts, faces, uvs

    # Split by material index
    n_mats = max(len(obj.material_slots), 1)
    vert_maps = [dict() for _ in range(n_mats)]
    vert_lists = [[] for _ in range(n_mats)]
    face_lists = [[] for _ in range(n_mats)]
    uv_lists = [[] for _ in range(n_mats)]

    for face in bm.faces:
        mat_idx = min(face.material_index, n_mats - 1)
        vm = vert_maps[mat_idx]
        vl = vert_lists[mat_idx]
        fl = face_lists[mat_idx]
        ul = uv_lists[mat_idx]

        tri = []
        for loop in face.loops:
            v = loop.vert
            key = v.index
            if key not in vm:
                vm[key] = len(vl)
                vl.append((v.co.x, v.co.y, v.co.z))
                uv = (loop[uv_layer].uv.x, loop[uv_layer].uv.y) if uv_layer else (0.0, 0.0)
                ul.append(uv)
            tri.append(vm[key])
        fl.append(tuple(tri))

    bm.free()

    result = []
    for i in range(n_mats):
        if vert_lists[i] and face_lists[i]:
            if len(vert_lists[i]) > 65535:
                raise ValueError(
                    f"Material slot {i} of '{obj.name}' has "
                    f"{len(vert_lists[i])} vertices. Decimate below 65536."
                )
            result.append((vert_lists[i], face_lists[i], uv_lists[i]))
    return result


def encode_primary_described_full_replace(
        original_gpu_bytes,
        original_primary_bytes,
        verts,
        faces,
        uvs=None,
        stream0_stride=None,
        compute_normals=True):
    """
    Full mesh replacement for primary_described files.

    The encoder's standalone encode_primary_described() writes only a 64-byte
    Primary (16 × uint32). The real Primary binary is larger — the game reads
    additional sections (bounding box, LOD tables, collision, etc.) from the
    same Primary stream after the descriptor block. Writing a truncated Primary
    causes "Offset is past the end of the stream" in cmemstream.cpp.

    This function instead:
      1. Builds the new GPU binary normally (s0 + s1 + ib).
      2. Takes the ORIGINAL Primary binary as a template.
      3. Patches ONLY the six descriptor fields the decoder uses
         (stream0_size, vertex_count ×3, index_offset, index_count).
      4. Returns the new GPU bytes and the patched full-length Primary bytes.

    Both returned binaries must be written back to disk. The Primary will be
    the same size as the original — all trailing game data is preserved.

    Args:
        original_gpu_bytes:     bytes — original GPU file (used only for reference,
                                not written back; the new GPU is built from scratch).
        original_primary_bytes: bytes — original Primary file (template).
        verts:                  [(x, y, z), ...]
        faces:                  [(i0, i1, i2), ...]
        uvs:                    optional [(u, v), ...] UV coordinates
        stream0_stride:         optional 16 or 20 (auto-detected if None).
        compute_normals:        pack smooth normals into stream-1.

    Returns:
        (new_gpu_bytes: bytes, patched_primary_bytes: bytes)
    """
    import struct

    _validate_mesh(verts, faces, "primary_described_full_replace")

    # Require the original Primary to be at least 64 bytes (the descriptor block).
    if len(original_primary_bytes) < 64:
        raise ValueError(
            f"Original Primary is only {len(original_primary_bytes)} bytes. "
            "Need at least 64 bytes (the 16-dword descriptor block)."
        )

    # Find the maximum original vertex count across all blocks in the template,
    # and automatically detect stream0_stride from the first valid block!
    max_orig_vc = 0
    detected_stride = None
    n_meta = len(original_primary_bytes)
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', original_primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', original_primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', original_primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                if vc > max_orig_vc:
                    max_orig_vc = vc
                if detected_stride is None:
                    s0_sz = struct.unpack_from('<I', original_primary_bytes, off + 4*4)[0]
                    if s0_sz % vc == 0:
                        cand = s0_sz // vc
                        if 12 <= cand <= 64:
                            detected_stride = cand

    # Default/fallback for stride
    if stream0_stride is None:
        stream0_stride = detected_stride if detected_stride is not None else 16

    # If the custom mesh has fewer vertices than expected by the shared index buffer,
    # we automatically pad the vertex buffer to satisfy shared LOD index buffer safety checks.
    real_nv = len(verts)
    if max_orig_vc > real_nv:
        padding_count = max_orig_vc - real_nv
        last_v = verts[-1] if verts else (0.0, 0.0, 0.0)
        verts = list(verts) + [last_v] * padding_count
        if uvs:
            last_uv = uvs[-1] if uvs else (0.0, 0.0)
            uvs = list(uvs) + [last_uv] * padding_count

    nv = len(verts)
    nt = len(faces)

    # Build new GPU binary (same as standalone encoder).
    s0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs)

    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)

    ib = _pack_index_buffer_u16(faces)
    new_gpu = s0 + s1 + ib

    # Pad the GPU binary with trailing zeros to match the exact original file size,
    # satisfying the game's manifest/archive index metadata and preventing stream out-of-bounds.
    orig_gpu_size = len(original_gpu_bytes)
    if len(new_gpu) < orig_gpu_size:
        padding = orig_gpu_size - len(new_gpu)
        new_gpu = new_gpu + b'\x00' * padding

    stream0_size  = nv * stream0_stride
    index_offset  = stream0_size + nv * 28   # s1 immediately follows s0
    index_count   = nt * 3                   # uint16 indices

    # Patch descriptor fields in ALL blocks in the original Primary template.
    # For linear blocks (rk == 2) we patch real index offsets and counts.
    # For cross-reference blocks (rk != 2) we update stream0_size and vertex_count but preserve their cross-ref index IDs.
    patched_primary = bytearray(original_primary_bytes)
    n_meta = len(patched_primary)
    block_count = 0
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', patched_primary, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', patched_primary, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', patched_primary, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', patched_primary, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                rk = struct.unpack_from('<I', patched_primary, off + 14*4)[0]
                if rk == 2:
                    # Linear block: update everything to new real values
                    for field_index, value in [
                        (2,  0),              # stream0 starts at byte 0
                        (4,  stream0_size),
                        (7,  nv),
                        (8,  nv),
                        (11, nv),
                        (12, index_offset),
                        (13, index_count),
                    ]:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)
                else:
                    # Cross-reference block: update stream0_size and vertex_count, keeping index_offset/index_count IDs
                    for field_index, value in [
                        (2,  0),              # stream0 starts at byte 0
                        (4,  stream0_size),
                        (7,  nv),
                        (8,  nv),
                        (11, nv),
                    ]:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)
                block_count += 1

    return new_gpu, bytes(patched_primary)


def encode_primary_described_multi_submesh_replace(
        original_gpu_bytes,
        original_primary_bytes,
        submesh_list,
        stream0_stride=None,
        compute_normals=True):
    """
    Multi-submesh replacement for environment prop models (CGMeshListResource).
    
    This function:
      1. Builds independent concatenated GPU submesh binaries (s0 + s1 + ib) for each submesh.
      2. Maps and patches each submesh's offset, count, and size individually in the Primary template.
      3. Safely preserves all cross-reference LOD index buffer IDs and shadow geometry blocks.
      4. Pads the final merged GPU binary to match the original size.
    """
    import struct

    n_meta = len(original_primary_bytes)

    # 1. Parse all descriptor, block, and structured array locations BEFORE patching
    # to avoid in-place corruption during parsing of contiguous/nested fields.
    ob_blocks = []
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', original_primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', original_primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', original_primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                s0_sz = struct.unpack_from('<I', original_primary_bytes, off + 4*4)[0]
                rk = struct.unpack_from('<I', original_primary_bytes, off + 14*4)[0]
                if rk == 2:
                    ib_cnt = struct.unpack_from('<I', original_primary_bytes, off + 13*4)[0]
                    fc = ib_cnt // 3
                else:
                    fc = vc
                ob_blocks.append((off, vc, s0_sz, fc))

    if not ob_blocks:
        raise ValueError("No valid rendering blocks found in the original Primary template.")

    # Structured arrays at the start of CGMeshListResource Primary files
    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    arrays = []
    off = 0
    for stride in sizes:
        if off + 4 <= n_meta:
            count = struct.unpack_from("<I", original_primary_bytes, off)[0]
            base = off + 4
            arrays.append((base, count, stride))
            off = base + count * stride
    tail_offset = off

    # 0xFFFFFF0C descriptors scanned in CGMeshListResource fallback decoding path
    descriptor_offs = []
    for doff in range(0, n_meta - 0x40, 4):
        vals = struct.unpack_from("<14I", original_primary_bytes, doff)
        if vals[0] == 0xFFFFFF0C and vals[1] == 0xFFFFFFFF:
            if vals[2] in (0x0B, 0x0D) and vals[3] == 0:
                vertex_count = vals[9]
                if vertex_count > 0 and vals[10] == vertex_count:
                    descriptor_offs.append(doff)

    # stream_records scanned in CGMeshListResource fallback decoding path
    stream_record_offs = []
    for soff in range(0, n_meta - 0x20, 0x08):
        vals = struct.unpack_from("<8I", original_primary_bytes, soff)
        if vals[0] == 4 and vals[2] and vals[4] and vals[5] in (0x2008, 0x2048):
            stream_record_offs.append(soff)

    # 2. Determine stride
    if stream0_stride is None:
        detected_stride = None
        for off_ob, vc, s0_sz, fc in ob_blocks:
            if s0_sz % vc == 0:
                cand = s0_sz // vc
                if 12 <= cand <= 64:
                    detected_stride = cand
                    break
        stream0_stride = detected_stride if detected_stride is not None else 16

    # 3. Match Blender submeshes to the expected blocks.
    # If the user has fewer submeshes, we pad extra submeshes with clean dummy placeholder points.
    processed_subs = []
    for idx in range(len(ob_blocks)):
        off_ob, orig_vc, s0_sz, orig_fc = ob_blocks[idx]
        if idx < len(submesh_list):
            sub = submesh_list[idx]
            if len(sub) == 3:
                verts, faces, uvs = sub
            else:
                verts, faces = sub
                uvs = None
                
            # If the custom submesh has fewer vertices than expected by the LOD template,
            # we pad it to ensure safety checks are satisfied.
            if orig_vc > len(verts):
                padding_count = orig_vc - len(verts)
                last_v = verts[-1] if verts else (0.0, 0.0, 0.0)
                verts = list(verts) + [last_v] * padding_count
                if uvs:
                    last_uv = uvs[-1] if uvs else (0.0, 0.0)
                    uvs = list(uvs) + [last_uv] * padding_count
        else:
            # Smart Dummy Padding! Completely collapses extra original submeshes into
            # invisible points at the origin that exactly match expected counts.
            # Using all-zero degenerate triangles which the GPU silently discards at rendering time!
            verts = [(0.0, 0.0, 0.0)] * orig_vc
            faces = [(0, 0, 0)] * orig_fc
            uvs = [(0.0, 0.0)] * orig_vc
            
        processed_subs.append((verts, faces, uvs))

    new_gpu = bytearray()
    submesh_offsets = []
    current_offset = 0

    for idx, (verts, faces, uvs) in enumerate(processed_subs):
        allow_degen = (idx >= len(submesh_list))
        _validate_mesh(verts, faces, f"multi_submesh_replace submesh {idx}", allow_degenerate=allow_degen)
        nv = len(verts)
        nt = len(faces)

        s0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs)
        if compute_normals:
            normals = _compute_smooth_normals(verts, faces)
            s1 = _pack_stream1_with_normals(verts, normals)
        else:
            s1 = _pack_stream1_zeroed(verts)
        ib = _pack_index_buffer_u16(faces)

        sub_binary = s0 + s1 + ib

        s0_start = current_offset
        s0_size = len(s0)
        s1_size = len(s1)
        ib_offset = s0_start + s0_size + s1_size
        ib_count = nt * 3

        submesh_offsets.append((s0_start, s0_size, nv, ib_offset, ib_count))

        new_gpu += sub_binary
        current_offset += len(sub_binary)

    # Pad GPU binary to original size
    orig_gpu_size = len(original_gpu_bytes)
    if len(new_gpu) < orig_gpu_size:
        padding = orig_gpu_size - len(new_gpu)
        new_gpu += b'\x00' * padding

    # 4. Patch Primary
    patched_primary = bytearray(original_primary_bytes)

    # Patch 0x0B sentinel blocks
    for block_index, (off_ob, vc, s0_sz, fc) in enumerate(ob_blocks):
        if block_index < len(submesh_offsets):
            s0_start, s0_size, nv, ib_offset, ib_count = submesh_offsets[block_index]

            # Force ALL submeshes to be Linear rendering blocks (rk = 2)!
            # This ensures perfect DirectX buffer layout alignment (stride-16/24) and index buffers!
            target_rk = 2
            struct.pack_into('<I', patched_primary, off_ob + 14*4, target_rk)

            fields = [
                (2, s0_start),
                (4, s0_size),
                (7, nv),
                (8, nv),
                (11, nv),
                (12, ib_offset),
                (13, ib_count),
            ]

            for field_index, value in fields:
                struct.pack_into('<I', patched_primary, off_ob + field_index * 4, value)

    # Patch structured arrays (if CGMeshListResource Primary structure matches exactly)
    if len(arrays) == 10:
        array1_base, array1_count, _ = arrays[1]
        array2_base, array2_count, _ = arrays[2]
        array5_base, array5_count, _ = arrays[5]

        # Patch Array 1: Stream record vertex counts (including LODs/Shadows)
        # Scan all Array 1 records, read original vertex count, and patch with new vertex count.
        orig_block_vertex_counts = [block[1] for block in ob_blocks]
        for r_idx in range(array1_count):
            rec_off = array1_base + r_idx * 0x70
            orig_vc = struct.unpack_from('<I', original_primary_bytes, rec_off + 0x48)[0]
            for block_idx, orig_block_vc in enumerate(orig_block_vertex_counts):
                if orig_vc == orig_block_vc:
                    if block_idx < len(submesh_offsets):
                        new_nv = submesh_offsets[block_idx][2]
                        struct.pack_into('<I', patched_primary, rec_off + 0x48, new_nv)
                        break

        for block_index in range(min(len(submesh_offsets), array2_count)):
            s0_start, s0_size, nv, ib_offset, ib_count = submesh_offsets[block_index]

            # Array 2: Main mesh metadata record
            rec_off = array2_base + block_index * 0x150
            struct.pack_into('<I', patched_primary, rec_off + 0x128, s0_start)
            struct.pack_into('<I', patched_primary, rec_off + 0x130, s0_size)
            struct.pack_into('<I', patched_primary, rec_off + 0x13c, nv)
            struct.pack_into('<I', patched_primary, rec_off + 0x140, nv)
            struct.pack_into('<I', patched_primary, rec_off + 0x14c, nv)

            # Array 5: Index buffer range metadata record
            if block_index < array5_count:
                rec_off = array5_base + block_index * 0x10
                struct.pack_into('<I', patched_primary, rec_off + 0x00, ib_offset)
                struct.pack_into('<I', patched_primary, rec_off + 0x04, ib_count * 2) # Size in bytes (16-bit indices)
                struct.pack_into('<I', patched_primary, rec_off + 0x08, 2)            # Force Linear range kind
                struct.pack_into('<I', patched_primary, rec_off + 0x0c, 0)            # Force range extra to 0

    # Patch 0xFFFFFF0C descriptors (fallback path)
    for block_index, doff in enumerate(descriptor_offs):
        if block_index < len(submesh_offsets):
            s0_start, s0_size, nv, ib_offset, ib_count = submesh_offsets[block_index]
            struct.pack_into('<I', patched_primary, doff + 4*4, s0_start)
            struct.pack_into('<I', patched_primary, doff + 6*4, s0_size)
            struct.pack_into('<I', patched_primary, doff + 9*4, nv)
            struct.pack_into('<I', patched_primary, doff + 10*4, nv)
            struct.pack_into('<I', patched_primary, doff + 13*4, nv)

    # Patch stream_records (fallback path)
    for block_index, soff in enumerate(stream_record_offs):
        if block_index < len(submesh_offsets):
            s0_start, s0_size, nv, ib_offset, ib_count = submesh_offsets[block_index]
            struct.pack_into('<I', patched_primary, soff + 2*4, nv)
            struct.pack_into('<I', patched_primary, soff + 4*4, ib_count)

    # Patch tail (CGMeshListResource files only)
    if len(arrays) == 10 and tail_offset + 16 <= n_meta:
        struct.pack_into('<I', patched_primary, tail_offset + 4, current_offset)
        struct.pack_into('<Q', patched_primary, tail_offset + 8, len(new_gpu))

    return bytes(new_gpu), bytes(patched_primary)


def patch_primary_described_positions(original_gpu_bytes, original_primary_bytes,
                                       scale_x=1.0, scale_y=1.0, scale_z=1.0):
    """
    Scale vertex positions in-place without rebuilding the GPU binary.

    For primary_described files, the game's resource manager reads buffer
    offsets from the Primary file. If you rebuild the GPU binary from scratch
    (via encode_primary_described), any file that has LOD data, shadow geometry,
    or secondary vertex buffers after the main mesh will cause
    'Offset is past end of stream' because the rebuilt binary is shorter.

    This function instead:
      1. Reads stream0_size and index_offset from the ORIGINAL Primary binary
         to locate stream-1 (which contains the XYZ float32 positions)
      2. Multiplies each vertex X/Y/Z by the given scale factors in-place.
      3. Returns the modified GPU bytes at the EXACT same length as the original

    The Primary binary is returned UNCHANGED because no offsets, counts,
    or bounding-box fields need to change for a scale (the game
    computes bounds at runtime from the vertex data).

    Args:
        original_gpu_bytes:     bytes — the original GPU file contents
        original_primary_bytes: bytes — the original Primary file contents
        scale_x, scale_y, scale_z: float scale multipliers per axis

    Returns:
        (patched_gpu_bytes, original_primary_bytes)
    """
    import struct

    if len(original_primary_bytes) < 64:
        raise ValueError(
            f"Primary binary too short ({len(original_primary_bytes)} bytes); "
            "expected at least 64 bytes (16 × uint32 descriptor)."
        )

    # Search for first 0x0B block to read stream0_size and vertex_count
    stream0_size = None
    vertex_count = None
    
    n_meta = len(original_primary_bytes)
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', original_primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', original_primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', original_primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                fields = struct.unpack_from('<16I', original_primary_bytes, off)
                stream0_size = fields[4]
                vertex_count = fields[7]
                break

    if stream0_size is None or vertex_count is None:
        # Fallback to absolute start of file
        fields = struct.unpack_from('<16I', original_primary_bytes, 0)
        stream0_size = fields[4]
        vertex_count = fields[7]

    # Validate that stream-1 fits inside the GPU binary
    stream1_start = stream0_size
    stream1_stride = 28         # always 28 in primary_described
    stream1_end = stream1_start + vertex_count * stream1_stride

    gpu = len(original_gpu_bytes)
    if stream1_end > gpu:
        raise ValueError(
            f"Primary descriptor says stream-1 ends at byte {stream1_end} "
            f"but GPU binary is only {gpu} bytes. "
            f"(stream0_size={stream0_size}, vertex_count={vertex_count})"
        )

    # Patch positions in stream-1 (XYZ float32 at bytes 0-11 of each record)
    patched = bytearray(original_gpu_bytes)
    for i in range(vertex_count):
        base = stream1_start + i * stream1_stride
        vx, vy, vz = struct.unpack_from('<fff', patched, base)
            
        struct.pack_into('<fff', patched, base,
                         vx * scale_x,
                         vy * scale_y,
                         vz * scale_z)

    return bytes(patched), original_primary_bytes
