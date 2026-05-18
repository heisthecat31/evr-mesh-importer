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
        v0, v1, v2 = [verts[i] for i in face]
        e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
        e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
        cx = e1[1]*e2[2] - e1[2]*e2[1]
        cy = e1[2]*e2[0] - e1[0]*e2[2]
        cz = e1[0]*e2[1] - e1[1]*e2[0]
        area = math.sqrt(cx*cx + cy*cy + cz*cz)
        for idx in face:
            normals[idx][0] += fn[0] * area
            normals[idx][1] += fn[1] * area
            normals[idx][2] += fn[2] * area
    return [_normalize(tuple(n)) for n in normals]


def _make_tangent_frame(normal):
    """
    Build an arbitrary but consistent tangent (T) and bitangent (B) from a normal.
    Returns (tangent_xyz, handedness) where handedness is +1.0 or -1.0.
    """
    nx, ny, nz = normal
    if abs(nx) <= abs(ny) and abs(nx) <= abs(nz):
        ref = (1.0, 0.0, 0.0)
    elif abs(ny) <= abs(nz):
        ref = (0.0, 1.0, 0.0)
    else:
        ref = (0.0, 0.0, 1.0)

    d = _dot(ref, normal)
    t = _normalize((ref[0] - d*nx, ref[1] - d*ny, ref[2] - d*nz))
    b = _cross(normal, t)
    handedness = 1.0 if _dot(b, b) > 0 else -1.0
    return t, handedness


# ============================================================
# Stream packers
# ============================================================

def _pack_stream0_s16(vertex_count, uvs=None):
    """Stream-0 stride-16: 0xFFFFFFFF 0x00000000 UV.u UV.v"""
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        out += struct.pack('<IIff', 0xFFFFFFFF, 0x00000000, u, v)
    return bytes(out)


def _pack_stream0_s20_white(vertex_count, uvs=None):
    """Stream-0 stride-20: 0xFF000000 0xFFFFFFFF UV.u UV.v 0x00000000"""
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        out += struct.pack('<IIffI', 0xFF000000, 0xFFFFFFFF, u, v, 0)
    return bytes(out)


def _pack_stream0_s28_ff(vertex_count):
    """Stream-0 stride-28: 0xFFFFFFFF 0xFFFFFFFF + 20 zero bytes"""
    record = struct.pack('<II', 0xFFFFFFFF, 0xFFFFFFFF) + b'\x00' * 20
    return record * vertex_count


def _pack_stream0_s44(vertex_count, uvs=None):
    """Stream-0 stride-44: 0xFF000000 0xFF000000 UV UV UV UV + 20 zero bytes"""
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        out += struct.pack('<IIffff', 0xFF000000, 0xFF000000, u, v, u, v) + b'\x00' * 20
    return bytes(out)


def _pack_stream0_dynamic(vertex_count, stride, uvs=None):
    """Dynamically pack stream-0 to match the required stride."""
    if stride == 16:
        return _pack_stream0_s16(vertex_count, uvs=uvs)
    elif stride == 20:
        return _pack_stream0_s20_white(vertex_count, uvs=uvs)
    elif stride == 28:
        return _pack_stream0_s28_ff(vertex_count)
    elif stride == 44:
        return _pack_stream0_s44(vertex_count, uvs=uvs)

    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        record = bytearray(struct.pack('<IIff', 0xFF000000, 0xFF000000, u, v))
        if stride >= 24:
            record += struct.pack('<ff', u, v)
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
      [18-19] snorm16     — 0 (padding)
      [20-21] snorm16     — tangent.x
      [22-23] snorm16     — tangent.y
      [24-25] snorm16     — tangent.z
      [26-27] snorm16     — tangent handedness (±32767)
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
            0,
        )
        out += struct.pack('<hhhh',
            _float_to_snorm16(tx),
            _float_to_snorm16(ty),
            _float_to_snorm16(tz),
            _float_to_snorm16(handedness),
        )
    return bytes(out)


def _pack_stream1_zeroed(verts):
    """Fallback stream-1 with normals zeroed."""
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
    """Triangle list as uint32 words."""
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
    """Encode using heuristic stride-16 stream-0 layout."""
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
    """Encode using heuristic stride-20 stream-0 layout."""
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
    """Encode using dual-28 layout."""
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
    """Encode in primary_described layout with matching Primary binary."""
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

    stream0_size = nv * stream0_stride
    index_offset = stream0_size + nv * 28

    primary_data = struct.pack('<16I',
        0x0B, 0, 0, 0,
        stream0_size, 0, 0,
        nv, nv, 0, 0, nv,
        index_offset, nt * 3, 2, 0,
    )

    return gpu_data, primary_data


def encode_cgml(submesh_list, compute_normals=True):
    """Encode multi-submesh CGML as concatenated heuristic_s16 blocks."""
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
# CGML In-Place Replace — the core fix for CGMeshListResource
# ============================================================

def encode_cgml_inplace_replace(original_gpu_bytes, original_primary_bytes,
                                  submesh_list, stream0_stride=16,
                                  compute_normals=True):
    """
    CGML replacement: keep ALL original sizes, just replace the data.
    Stream-0: copy from original
    Stream-1: new positions, padded to original size
    Index buffer: new indices, padded to original size
    Primary: only update vertex_count fields, nothing else changes
    """
    n_meta = len(original_primary_bytes)

    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    arrays = []
    off = 0
    for stride in sizes:
        if off + 4 > n_meta:
            raise ValueError("Primary file too short for CGML 10-array layout.")
        count = struct.unpack_from("<I", original_primary_bytes, off)[0]
        if count > 1000:
            raise ValueError(f"CGML array count too large: {count}")
        base = off + 4
        end = base + count * stride
        if end > n_meta:
            raise ValueError("CGML array extends past end of Primary file.")
        arrays.append((base, count, stride))
        off = end

    tail_offset = off

    if len(arrays) != 10:
        raise ValueError("Primary file does not have the expected 10-array CGML layout.")

    array1_base, array1_count, _ = arrays[1]
    array2_base, array2_count, _ = arrays[2]
    array5_base, array5_count, _ = arrays[5]

    if array2_count == 0:
        raise ValueError("CGML Primary has zero submeshes (array2_count=0).")

    if tail_offset + 16 > n_meta:
        raise ValueError("CGML Primary tail missing.")
    tail_a, orig_mesh_data_end, orig_gpu_size = struct.unpack_from(
        "<IIQ", original_primary_bytes, tail_offset)

    if len(submesh_list) == 0:
        raise ValueError("No submeshes provided for CGML replacement.")
    
    first_sub = submesh_list[0]
    if len(first_sub) == 3:
        verts, faces, uvs = first_sub
    else:
        verts, faces = first_sub
        uvs = None
    
    _validate_mesh(verts, faces, "cgml_replacement")
    
    nv = len(verts)
    nt = len(faces)

    # Read ALL original offsets and sizes - we preserve everything
    orig_descriptors = []
    for i in range(array2_count):
        rec_off = array2_base + i * 0x150
        base_offset = struct.unpack_from("<I", original_primary_bytes, rec_off + 0x128)[0]
        s0_size = struct.unpack_from("<I", original_primary_bytes, rec_off + 0x130)[0]
        vc = struct.unpack_from("<I", original_primary_bytes, rec_off + 0x13c)[0]
        
        ib_off = 0
        ib_size = 0
        if i < array5_count:
            range_rec = array5_base + i * 0x10
            ib_off = struct.unpack_from("<I", original_primary_bytes, range_rec + 0x00)[0]
            ib_size = struct.unpack_from("<I", original_primary_bytes, range_rec + 0x04)[0]
        
        orig_descriptors.append({
            'base_offset': base_offset,
            's0_size': s0_size,
            'vc': vc,
            'ib_off': ib_off,
            'ib_size': ib_size,
        })

    # Use first slot's capacities
    first = orig_descriptors[0]
    if nv > first['vc']:
        raise ValueError(f"New vertex count {nv} exceeds original {first['vc']}.")

    # Detect stride
    if stream0_stride is None:
        if first['vc'] > 0 and first['s0_size'] % first['vc'] == 0:
            cand = first['s0_size'] // first['vc']
            if 12 <= cand <= 64:
                stream0_stride = cand
        if stream0_stride is None:
            stream0_stride = 16

    # --- Patch GPU in-place ---
    patched_gpu = bytearray(original_gpu_bytes)
    patched_primary = bytearray(original_primary_bytes)

    for i, desc in enumerate(orig_descriptors):
        # Stream-0: copy first nv records from original, pad to original size
        orig_s0 = original_gpu_bytes[desc['base_offset'] : desc['base_offset'] + desc['s0_size']]
        new_s0 = bytearray(orig_s0[:nv * stream0_stride])
        last_s0 = new_s0[-stream0_stride:] if new_s0 else orig_s0[:stream0_stride]
        while len(new_s0) < desc['s0_size']:
            new_s0 += last_s0
        new_s0 = new_s0[:desc['s0_size']]
        patched_gpu[desc['base_offset'] : desc['base_offset'] + desc['s0_size']] = new_s0

        # Stream-1: new positions, padded to fill space up to original IB
        s1_start = desc['base_offset'] + desc['s0_size']
        orig_s1_end = desc['ib_off'] if desc['ib_off'] > s1_start else s1_start + desc['vc'] * 28
        orig_s1_size = orig_s1_end - s1_start
        
        if compute_normals:
            normals = _compute_smooth_normals(verts, faces)
            s1 = _pack_stream1_with_normals(verts, normals)
        else:
            s1 = _pack_stream1_zeroed(verts)
        
        s1_padded = bytearray(s1)
        last_s1 = s1[-28:] if s1 else b'\x00' * 28
        while len(s1_padded) < orig_s1_size:
            s1_padded += last_s1
        s1_padded = s1_padded[:orig_s1_size]
        patched_gpu[s1_start : s1_start + orig_s1_size] = s1_padded

        # Index buffer: new indices, padded to original size
        ib = bytearray()
        for f in faces:
            ib += struct.pack('<HHH', f[0], f[1], f[2])
        
        ib_padded = bytearray(ib)
        while len(ib_padded) < desc['ib_size']:
            ib_padded += b'\x00\x00'
        ib_padded = ib_padded[:desc['ib_size']]
        patched_gpu[desc['ib_off'] : desc['ib_off'] + desc['ib_size']] = ib_padded

        # Update Primary Array 2 - only vertex counts
        rec_off = array2_base + i * 0x150
        struct.pack_into('<I', patched_primary, rec_off + 0x13c, nv)
        struct.pack_into('<I', patched_primary, rec_off + 0x140, nv)
        struct.pack_into('<I', patched_primary, rec_off + 0x14c, nv)

    # Update Array 1
    for r_idx in range(array1_count):
        rec_off = array1_base + r_idx * 0x70
        struct.pack_into('<I', patched_primary, rec_off + 0x48, nv)

    return bytes(patched_gpu), bytes(patched_primary)


# ============================================================
# CIMR In-Place Replace (primary_described, single-block)
# ============================================================

def encode_primary_described_full_replace(
        original_gpu_bytes,
        original_primary_bytes,
        verts,
        faces,
        uvs=None,
        stream0_stride=None,
        compute_normals=True):
    """
    Full mesh replacement for primary_described CIMR files using in-place swap.
    Preserves original file offsets, sizes, and capacities exactly.
    """
    _validate_mesh(verts, faces, "primary_described_full_replace")

    if len(original_primary_bytes) < 64:
        raise ValueError(
            f"Original Primary is only {len(original_primary_bytes)} bytes. "
            "Need at least 64 bytes (the 16-dword descriptor block)."
        )

    # Scan all valid rendering blocks in the template
    blocks = []
    n_meta = len(original_primary_bytes)
    detected_stride = None
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', original_primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', original_primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', original_primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                s0_start = struct.unpack_from('<I', original_primary_bytes, off + 2*4)[0]
                s0_size = struct.unpack_from('<I', original_primary_bytes, off + 4*4)[0]
                rk = struct.unpack_from('<I', original_primary_bytes, off + 14*4)[0]
                ib_offset = struct.unpack_from('<I', original_primary_bytes, off + 12*4)[0]
                ib_count = struct.unpack_from('<I', original_primary_bytes, off + 13*4)[0]
                blocks.append({
                    'off': off, 'vc': vc, 's0_start': s0_start, 's0_size': s0_size,
                    'rk': rk, 'ib_offset': ib_offset, 'ib_count': ib_count
                })
                if detected_stride is None and s0_size % vc == 0:
                    cand = s0_size // vc
                    if 12 <= cand <= 64:
                        detected_stride = cand

    if not blocks:
        raise ValueError("No valid rendering blocks found in the original Primary template.")

    lod0_block = max(blocks, key=lambda b: b['vc'])
    linear_block = next((b for b in blocks if b['rk'] == 2), lod0_block)

    if stream0_stride is None:
        stream0_stride = detected_stride if detected_stride is not None else 16

    new_nv = len(verts)
    new_ib_count = len(faces) * 3

    max_vc = lod0_block['vc']
    max_ib_count = linear_block['ib_count']

    if new_nv > max_vc:
        raise ValueError(
            f"Custom model has too many vertices ({new_nv} > {max_vc} max capacity). "
            "Simplify/decimate or swap with a larger base model."
        )
    if new_ib_count > max_ib_count:
        raise ValueError(
            f"Custom model has too many indices ({new_ib_count} > {max_ib_count} max capacity). "
            "Simplify/decimate or swap with a larger base model."
        )

    patched_gpu = bytearray(original_gpu_bytes)

    s0 = _pack_stream0_dynamic(new_nv, stream0_stride, uvs=uvs)
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals)
    else:
        s1 = _pack_stream1_zeroed(verts)

    orig_s0_size = lod0_block['s0_size']
    orig_s0_start = lod0_block['s0_start']
    orig_s1_size = max_vc * 28

    padded_s0 = bytearray(s0)
    if len(padded_s0) < orig_s0_size:
        last_record = s0[-stream0_stride:] if s0 else b'\x00' * stream0_stride
        while len(padded_s0) < orig_s0_size:
            padded_s0 += last_record
        padded_s0 = padded_s0[:orig_s0_size]
    patched_gpu[orig_s0_start : orig_s0_start + orig_s0_size] = padded_s0

    padded_s1 = bytearray(s1)
    if len(padded_s1) < orig_s1_size:
        last_record = s1[-28:] if s1 else b'\x00' * 28
        while len(padded_s1) < orig_s1_size:
            padded_s1 += last_record
        padded_s1 = padded_s1[:orig_s1_size]
    orig_s1_start = orig_s0_start + orig_s0_size
    patched_gpu[orig_s1_start : orig_s1_start + orig_s1_size] = padded_s1

    ib = bytearray()
    for f in faces:
        ib += struct.pack('<HHH', f[0], f[1], f[2])
    orig_ib_size = max_ib_count * 2
    orig_ib_offset = linear_block['ib_offset']
    padded_ib = bytearray(ib)
    if len(padded_ib) < orig_ib_size:
        padded_ib += original_gpu_bytes[orig_ib_offset + len(padded_ib) : orig_ib_offset + orig_ib_size]
    patched_gpu[orig_ib_offset : orig_ib_offset + orig_ib_size] = padded_ib

    index_count = new_ib_count

    patched_primary = bytearray(original_primary_bytes)
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', patched_primary, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', patched_primary, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', patched_primary, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', patched_primary, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0 and vc == lod0_block['vc']:
                rk = struct.unpack_from('<I', patched_primary, off + 14*4)[0]
                if rk == 2:
                    for field_index, value in [(7, new_nv), (8, new_nv), (11, new_nv), (13, index_count)]:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)
                else:
                    for field_index, value in [(7, new_nv), (8, new_nv), (11, new_nv)]:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)

    return bytes(patched_gpu), bytes(patched_primary)


# ============================================================
# CIMR Multi-Submesh In-Place Replace
# ============================================================

def encode_primary_described_multi_submesh_replace(
        original_gpu_bytes,
        original_primary_bytes,
        submesh_list,
        stream0_stride=None,
        compute_normals=True):
    """
    Multi-submesh CIMR replacement using 0x0B descriptor blocks.
    Preserves original file offsets, sizes, and capacities exactly.
    """
    n_meta = len(original_primary_bytes)

    ob_blocks = []
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', original_primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', original_primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', original_primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                s0_sz = struct.unpack_from('<I', original_primary_bytes, off + 4*4)[0]
                s0_start = struct.unpack_from('<I', original_primary_bytes, off + 2*4)[0]
                rk = struct.unpack_from('<I', original_primary_bytes, off + 14*4)[0]
                ib_offset = struct.unpack_from('<I', original_primary_bytes, off + 12*4)[0]
                ib_cnt = struct.unpack_from('<I', original_primary_bytes, off + 13*4)[0]
                fc = ib_cnt // 3 if rk == 2 else vc
                ob_blocks.append((off, vc, s0_sz, s0_start, ib_offset, ib_cnt, fc, rk))

    if not ob_blocks:
        raise ValueError("No valid rendering blocks found in the original Primary template.")

    if stream0_stride is None:
        detected_stride = None
        for _, vc, s0_sz, _, _, _, _, _ in ob_blocks:
            if s0_sz % vc == 0:
                cand = s0_sz // vc
                if 12 <= cand <= 64:
                    detected_stride = cand
                    break
        stream0_stride = detected_stride if detected_stride is not None else 16

    processed_subs = []
    for idx in range(len(ob_blocks)):
        _, orig_vc, s0_sz, _, _, _, orig_fc, _ = ob_blocks[idx]
        if idx < len(submesh_list):
            sub = submesh_list[idx]
            if len(sub) == 3:
                verts, faces, uvs = sub
            else:
                verts, faces = sub
                uvs = None
        else:
            verts = [(0.0, 0.0, 0.0)] * orig_vc
            faces = [(0, 0, 0)] * orig_fc
            uvs = [(0.0, 0.0)] * orig_vc
        processed_subs.append((verts, faces, uvs))

    patched_gpu = bytearray(original_gpu_bytes)
    patched_primary = bytearray(original_primary_bytes)

    submesh_offsets = []

    for block_idx, (verts, faces, uvs) in enumerate(processed_subs):
        allow_degen = (block_idx >= len(submesh_list))
        _validate_mesh(verts, faces, f"multi_submesh_replace submesh {block_idx}", allow_degenerate=allow_degen)

        new_nv = len(verts)
        new_ib_count = len(faces) * 3

        off_ob, orig_vc, orig_s0_size, orig_s0_start, orig_ib_offset, orig_ib_icount, orig_fc, rk = ob_blocks[block_idx]

        if not allow_degen:
            if new_nv > orig_vc:
                raise ValueError(
                    f"Custom Submesh {block_idx} has too many vertices ({new_nv} > {orig_vc} max capacity). "
                    "Simplify/decimate or swap with a larger base model."
                )
            if new_ib_count > orig_ib_icount:
                raise ValueError(
                    f"Custom Submesh {block_idx} has too many indices ({new_ib_count} > {orig_ib_icount} max capacity). "
                    "Simplify/decimate or swap with a larger base model."
                )

        s0 = _pack_stream0_dynamic(new_nv, stream0_stride, uvs=uvs)
        padded_s0 = bytearray(s0)
        if len(padded_s0) < orig_s0_size:
            last_record = s0[-stream0_stride:] if s0 else b'\x00' * stream0_stride
            while len(padded_s0) < orig_s0_size:
                padded_s0 += last_record
            padded_s0 = padded_s0[:orig_s0_size]
        patched_gpu[orig_s0_start : orig_s0_start + orig_s0_size] = padded_s0

        if compute_normals:
            normals = _compute_smooth_normals(verts, faces)
            s1 = _pack_stream1_with_normals(verts, normals)
        else:
            s1 = _pack_stream1_zeroed(verts)

        orig_s1_size = orig_vc * 28
        padded_s1 = bytearray(s1)
        if len(padded_s1) < orig_s1_size:
            last_record = s1[-28:] if s1 else b'\x00' * 28
            while len(padded_s1) < orig_s1_size:
                padded_s1 += last_record
            padded_s1 = padded_s1[:orig_s1_size]
        orig_s1_start = orig_s0_start + orig_s0_size
        patched_gpu[orig_s1_start : orig_s1_start + orig_s1_size] = padded_s1

        ib = bytearray()
        for f in faces:
            ib += struct.pack('<HHH', f[0], f[1], f[2])
        orig_ib_size = orig_ib_icount * 2
        padded_ib = bytearray(ib)
        if len(padded_ib) < orig_ib_size:
            padded_ib += b'\x00' * (orig_ib_size - len(padded_ib))
        patched_gpu[orig_ib_offset : orig_ib_offset + orig_ib_size] = padded_ib

        submesh_offsets.append((orig_s0_start, orig_s0_size, new_nv, orig_ib_offset, new_ib_count))

    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', patched_primary, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', patched_primary, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', patched_primary, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', patched_primary, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                for block_idx, (_, orig_vc, _, _, _, _, _, _) in enumerate(ob_blocks):
                    if orig_vc == vc and block_idx < len(submesh_offsets):
                        _, _, new_nv, _, new_ib_count = submesh_offsets[block_idx]
                        rk = struct.unpack_from('<I', patched_primary, off + 14*4)[0]
                        if rk == 2:
                            for fi, val in [(7, new_nv), (8, new_nv), (11, new_nv), (13, new_ib_count)]:
                                struct.pack_into('<I', patched_primary, off + fi * 4, val)
                        else:
                            for fi, val in [(7, new_nv), (8, new_nv), (11, new_nv)]:
                                struct.pack_into('<I', patched_primary, off + fi * 4, val)
                        break

    return bytes(patched_gpu), bytes(patched_primary)


# ============================================================
# Scale-only position patch
# ============================================================

def patch_primary_described_positions(original_gpu_bytes, original_primary_bytes,
                                       scale_x=1.0, scale_y=1.0, scale_z=1.0):
    """
    Scale vertex positions in-place without rebuilding the GPU binary.
    """
    if len(original_primary_bytes) < 64:
        raise ValueError(
            f"Primary binary too short ({len(original_primary_bytes)} bytes); "
            "expected at least 64 bytes (16 × uint32 descriptor)."
        )

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
        fields = struct.unpack_from('<16I', original_primary_bytes, 0)
        stream0_size = fields[4]
        vertex_count = fields[7]

    stream1_start = stream0_size
    stream1_stride = 28
    stream1_end = stream1_start + vertex_count * stream1_stride

    gpu = len(original_gpu_bytes)
    if stream1_end > gpu:
        raise ValueError(
            f"Primary descriptor says stream-1 ends at byte {stream1_end} "
            f"but GPU binary is only {gpu} bytes. "
            f"(stream0_size={stream0_size}, vertex_count={vertex_count})"
        )

    patched = bytearray(original_gpu_bytes)
    for i in range(vertex_count):
        base = stream1_start + i * stream1_stride
        vx, vy, vz = struct.unpack_from('<fff', patched, base)
        struct.pack_into('<fff', patched, base,
                         vx * scale_x, vy * scale_y, vz * scale_z)

    return bytes(patched), original_primary_bytes


# ============================================================
# Blender mesh extraction
# ============================================================

def mesh_from_blender_object(obj, apply_transforms=True, split_by_material=False):
    """
    Extract (verts, faces, uvs) from a Blender mesh object.
    """
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    if apply_transforms:
        bm.transform(obj.matrix_world)

    bmesh.ops.triangulate(bm, faces=bm.faces[:])
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