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

def _pack_stream0_s16(vertex_count):
    """
    Stream-0 stride-16: most common CIMR pattern.
    Records are 0xFFFFFFFF 0x00000000 0x00000000 0x00000000
    The heuristic path detects these via _find_runs (8-byte seed repeating).
    """
    record = struct.pack('<IIII', 0xFFFFFFFF, 0x00000000, 0x00000000, 0x00000000)
    return record * vertex_count


def _pack_stream0_s20_white(vertex_count):
    """
    Stream-0 stride-20: vertex-colored variant, all-white (0xFFFFFFFF RGBA).
    Bytes[3:7] = 0xFFFFFFFF triggers _find_vertex_colored_runs.
    Layout per record: [R:u8][G:u8][B:u8][A:u8=FF][FF:u8][FF:u8][FF:u8][FF:u8] + 12 zeroed
    We emit a clean white record: 0xFFFFFFFF 0xFFFFFFFF 0x00000000 0x00000000 0x00000000
    """
    record = struct.pack('<IIIII', 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0)
    return record * vertex_count


def _pack_stream0_s28_ff(vertex_count):
    """
    Stream-0 stride-28: for dual-28 layout (both streams same stride).
    First two dwords = 0xFFFFFFFF, rest zeroed.
    Detected by _find_prefix_pair_run.
    """
    record = struct.pack('<II', 0xFFFFFFFF, 0xFFFFFFFF) + b'\x00' * 20
    return record * vertex_count


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

def _validate_mesh(verts, faces, label="mesh"):
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
    if degenerate == len(faces):
        raise ValueError(f"{label}: all {len(faces)} faces are degenerate")


# ============================================================
# Public encoders
# ============================================================

def encode_heuristic_s16(verts, faces, compute_normals=True):
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
        compute_normals: if True, compute smooth normals from topology.
                         if False, stream-1 normals are zeroed (safe for
                         geometry testing but will look flat/wrong in-game).

    Returns:
        bytes — the complete GPU binary to write over the original file.
    """
    _validate_mesh(verts, faces, "heuristic_s16")
    s0 = _pack_stream0_s16(len(verts))
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib


def encode_heuristic_s20(verts, faces, compute_normals=True):
    """
    Encode using the heuristic stride-20 stream-0 layout (vertex-colored family).

    Same as encode_heuristic_s16 but stream-0 uses stride-20 white records.
    Use this when the original file decoded via the vertex-colored heuristic path.

    Returns:
        bytes — the complete GPU binary.
    """
    _validate_mesh(verts, faces, "heuristic_s20")
    s0 = _pack_stream0_s20_white(len(verts))
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


def encode_primary_described(verts, faces, stream0_stride=16, compute_normals=True):
    """
    Encode a mesh in the primary_described layout and produce a matching
    Primary binary. Both files must be written for the game to load correctly.

    Layout (base_offset=0):
      GPU:     [stream-0: Nv*s0] [stream-1: Nv*28] [index buffer]
      Primary: [0x0B descriptor block: 16 × uint32]

    Args:
        verts:           [(x, y, z), ...]
        faces:           [(i0, i1, i2), ...]
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
        s0 = _pack_stream0_s16(nv)
    else:
        s0 = _pack_stream0_s20_white(nv)

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
        submesh_list:    [(verts, faces), ...]
        compute_normals: pack smooth normals.

    Returns:
        bytes — concatenated GPU binary for all submeshes.
    """
    out = bytearray()
    for i, (verts, faces) in enumerate(submesh_list):
        _validate_mesh(verts, faces, f"cgml submesh {i}")
        out += encode_heuristic_s16(verts, faces, compute_normals=compute_normals)
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

    if not split_by_material:
        verts = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
        faces = [(f.verts[0].index, f.verts[1].index, f.verts[2].index)
                 for f in bm.faces]
        bm.free()
        if len(verts) > 65535:
            raise ValueError(
                f"Mesh '{obj.name}' has {len(verts)} vertices. "
                "Decimate below 65536 before exporting."
            )
        return verts, faces

    # Split by material index
    n_mats = max(len(obj.material_slots), 1)
    vert_maps = [dict() for _ in range(n_mats)]
    vert_lists = [[] for _ in range(n_mats)]
    face_lists = [[] for _ in range(n_mats)]

    for face in bm.faces:
        mat_idx = min(face.material_index, n_mats - 1)
        vm = vert_maps[mat_idx]
        vl = vert_lists[mat_idx]
        fl = face_lists[mat_idx]

        tri = []
        for v in face.verts:
            key = v.index
            if key not in vm:
                vm[key] = len(vl)
                vl.append((v.co.x, v.co.y, v.co.z))
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
            result.append((vert_lists[i], face_lists[i]))
    return result
