"""
EVR Raw Mesh Importer — encode module
Writes replacement GPU binaries in the layouts Echo VR expects.
"""

import struct
import math

class VertexLimitError(Exception):
    def __init__(self, current_verts, max_verts=65535, mesh_name="Unknown"):
        self.current_verts = current_verts
        self.max_verts = max_verts
        self.mesh_name = mesh_name
        super().__init__(f"Mesh '{mesh_name}' exceeds vertex/size limit: {current_verts} > {max_verts}")

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
    v0, v1, v2 = [verts[i] for i in face]
    e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
    e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
    return _normalize(_cross(e1, e2))

def _float_to_snorm16(f):
    f = max(-1.0, min(1.0, f))
    v = int(round(f * 32767.0))
    return max(-32768, min(32767, v))

def _float_to_float16(f):
    return struct.unpack('<H', struct.pack('<e', f))[0]

# ============================================================
# Normal / tangent computation
# ============================================================

def _compute_smooth_normals(verts, faces):
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

def _pack_stream0_s16(vertex_count, uvs=None, bone_data=None):
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        
        if bone_data and i < len(bone_data):
            indices, weights = bone_data[i]
            b0, b1, b2, b3 = weights
            word0 = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
            word1 = 0x00000000
        else:
            word0 = 0x00000000
            word1 = 0x00000000
            
        out += struct.pack('<IIff', word0, word1, u, v)
    return bytes(out)

def _pack_stream0_s20_white(vertex_count, uvs=None, bone_data=None):
    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        
        if bone_data and i < len(bone_data):
            indices, weights = bone_data[i]
            b0, b1, b2, b3 = weights
            word0 = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
            word1 = 0x00000000
        else:
            word0 = 0x00000000
            word1 = 0x00000000
            
        out += struct.pack('<IIffI', word0, word1, u, v, 0)
    return bytes(out)

def _pack_stream0_s28_ff(vertex_count):
    # SANITIZED
    record = struct.pack('<II', 0x00000000, 0x00000000) + b'\x00' * 20
    return record * vertex_count

def _pack_stream0_dynamic(vertex_count, stride, uvs=None, bone_data=None, orig_word1=0xFFFF0000, orig_word0=0x00000000, orig_stream0=None):
    if stride == 16:
        return _pack_stream0_s16(vertex_count, uvs=uvs, bone_data=bone_data)
    elif stride == 20:
        return _pack_stream0_s20_white(vertex_count, uvs=uvs, bone_data=bone_data)

    out = bytearray()
    for i in range(vertex_count):
        u, v = uvs[i] if uvs else (0.0, 0.0)
        
        orig_record = None
        if orig_stream0 and len(orig_stream0) >= (i+1)*stride:
            orig_record = bytearray(orig_stream0[i*stride:(i+1)*stride])
        
        if bone_data and i < len(bone_data):
            indices, weights = bone_data[i]
            if stride < 24:
                b0, b1, b2, b3 = weights
                word0 = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
                if orig_word1 != 0xFFFF0000 and orig_word1 != 0xFFFFFFFF:
                    i0, i1, i2, i3 = indices
                    word1 = (i3 << 24) | (i2 << 16) | (i1 << 8) | i0
                else:
                    word1 = orig_word1
            else:
                word0 = orig_word0
                word1 = orig_word1
        else:
            word0 = orig_word0 if stride >= 24 else 0x00000000
            word1 = orig_word1
            
        if orig_record:
            record = orig_record
            struct.pack_into('<IIff', record, 0, word0, word1, u, v)
        else:
            record = bytearray(struct.pack('<IIff', word0, word1, u, v))
            while len(record) + 8 <= stride and len(record) < 40:
                record += struct.pack('<ff', u, v)
            pad_len = stride - len(record)
            if pad_len > 0:
                record += b'\x00' * pad_len
            
        # For CGML models with stride >= 24, bone indices and weights are packed at the very end
        if stride >= 24:
            idx_off = stride - 8
            wt_off = stride - 4
            
            if bone_data and i < len(bone_data):
                indices, weights = bone_data[i]
            else:
                indices, weights = (0, 0, 0, 0), (0, 0, 0, 255)
                
            struct.pack_into('<4B', record, idx_off, *indices)
            struct.pack_into('<4B', record, wt_off, *weights)
            
        if len(record) > stride:
            record = record[:stride]
            
        out += record
    return bytes(out)

def _pack_index_buffer_u16(faces):
    out = bytearray()
    for i0, i1, i2 in faces:
        out += struct.pack('<HHH', i0, i1, i2)
    if len(out) % 4:
        out += b'\x00' * (4 - len(out) % 4)
    return bytes(out)

def _pack_index_buffer_u32(faces):
    out = bytearray()
    for i0, i1, i2 in faces:
        out += struct.pack('<III', i0, i1, i2)
    return bytes(out)

def _pack_10_10_10_2(x, y, z, w=0):
    def to_snorm10(f):
        f = max(-1.0, min(1.0, f))
        v = int(round(f * 511.0))
        return max(-512, min(511, v)) & 0x3FF
        
    def to_snorm2(f):
        if f > 0.5: return 1
        if f < -0.5: return 3
        return 0

    ix = to_snorm10(x)
    iy = to_snorm10(y)
    iz = to_snorm10(z)
    iw = to_snorm2(w) & 0x3
    
    packed = (iw << 30) | (iz << 20) | (iy << 10) | ix
    return struct.pack('<I', packed)

def _pack_stream1_with_normals(verts, normals, tangents=None, bone_data=None, orig_stream1=None, s1_stride=28, force_snorm16=False):
    out = bytearray()
    for i, ((x, y, z), (nx, ny, nz)) in enumerate(zip(verts, normals)):
        orig_record = None
        if orig_stream1 and len(orig_stream1) >= (i+1)*s1_stride:
            orig_record = bytearray(orig_stream1[i*s1_stride : (i+1)*s1_stride])
            
        if orig_record:
            record = orig_record
            struct.pack_into('<fff', record, 0, x, y, z)
            n_packed = struct.pack('<hhhh', _float_to_snorm16(nx), _float_to_snorm16(ny), _float_to_snorm16(nz), 0)
            record[12:20] = n_packed
        else:
            record = bytearray()
            record += struct.pack('<fff', x, y, z)
            
            if tangents and i < len(tangents):
                tx, ty, tz, handedness = tangents[i]
            else:
                tangent, handedness = _make_tangent_frame((nx, ny, nz))
                tx, ty, tz = tangent
            
            if (not force_snorm16) and bone_data and i < len(bone_data):
                record += _pack_10_10_10_2(nx, ny, nz, 0)
                record += _pack_10_10_10_2(tx, ty, tz, handedness)
                indices, weights = bone_data[i]
                record += struct.pack('<4B', *indices)
                record += _pack_10_10_10_2(0, 1, 0, 1)
            else:
                record += struct.pack('<hhhh', _float_to_snorm16(nx), _float_to_snorm16(ny), _float_to_snorm16(nz), 0)
                record += struct.pack('<hhhh', _float_to_snorm16(tx), _float_to_snorm16(ty), _float_to_snorm16(tz), _float_to_snorm16(handedness))
                
            if len(record) < s1_stride:
                record += b'\x00' * (s1_stride - len(record))
            elif len(record) > s1_stride:
                record = record[:s1_stride]
                
        out += record
    return bytes(out)

def _pack_stream1_zeroed(verts, bone_data=None, orig_stream1=None, s1_stride=28):
    out = bytearray()
    for i, (x, y, z) in enumerate(verts):
        orig_record = None
        if orig_stream1 and len(orig_stream1) >= (i+1)*s1_stride:
            orig_record = bytearray(orig_stream1[i*s1_stride : (i+1)*s1_stride])
            
        if orig_record:
            record = orig_record
            struct.pack_into('<fff', record, 0, x, y, z)
        else:
            record = bytearray()
            record += struct.pack('<fff', x, y, z)
            
            if bone_data and i < len(bone_data):
                record += _pack_10_10_10_2(0, 0, 1, 0)
                record += _pack_10_10_10_2(1, 0, 0, 1)
                indices, weights = bone_data[i]
                record += struct.pack('<4B', *indices)
                record += _pack_10_10_10_2(0, 1, 0, 1)
            else:
                record += struct.pack('<hhhh', 0, 0, 32767, 0)
                record += struct.pack('<hhhh', 32767, 0, 0, 32767)
                
            if len(record) < s1_stride:
                record += b'\x00' * (s1_stride - len(record))
            elif len(record) > s1_stride:
                record = record[:s1_stride]
                
        out += record
    return bytes(out)

def encode_heuristic_s16(verts, faces, uvs=None, bone_data=None, compute_normals=True):
    _validate_mesh(verts, faces, "heuristic_s16")
    s0 = _pack_stream0_s16(len(verts), uvs=uvs, bone_data=bone_data)
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals, bone_data=bone_data)
    else:
        s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib

def encode_heuristic_s20(verts, faces, uvs=None, bone_data=None, compute_normals=True):
    _validate_mesh(verts, faces, "heuristic_s20")
    s0 = _pack_stream0_s20_white(len(verts), uvs=uvs, bone_data=bone_data)
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals, bone_data=bone_data)
    else:
        s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib

def encode_heuristic_dual28(verts, faces, bone_data=None, compute_normals=True):
    _validate_mesh(verts, faces, "heuristic_dual28")
    s0 = _pack_stream0_s28_ff(len(verts))
    if compute_normals:
        normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals, bone_data=bone_data)
    else:
        s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)
    ib = _pack_index_buffer_u16(faces)
    return s0 + s1 + ib

def encode_primary_described(verts, faces, uvs=None, bone_data=None, stream0_stride=16, compute_normals=True):
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
        s1 = _pack_stream1_with_normals(verts, normals, bone_data=bone_data)
    else:
        s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)

    ib = _pack_index_buffer_u16(faces)
    gpu_data = s0 + s1 + ib

    stream0_size = nv * stream0_stride
    index_offset = stream0_size + nv * 28   

    primary_data = struct.pack('<16I',
        0x0B, 0, 0, 0, stream0_size, 0, 0, nv,
        nv, 0, 0, nv, index_offset, nt * 3, 2, 0
    )

    return gpu_data, primary_data

def encode_cgml(submesh_list, compute_normals=True):
    out = bytearray()
    for i, sub in enumerate(submesh_list):
        if len(sub) == 6:
            verts, faces, uvs, bone_data, normals, tangents = sub
        elif len(sub) == 4:
            verts, faces, uvs, bone_data = sub
        elif len(sub) == 3:
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
    import bpy
    import mathutils

    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()

    if apply_transforms:
        mesh.transform(eval_obj.matrix_world)

    # We must triangulate to calculate tangents correctly.
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(mesh)
    bm.free()

    if not mesh.uv_layers:
        # Create a dummy UV layer if missing so calc_tangents doesn't fail
        mesh.uv_layers.new(name="UVMap")

    for poly in mesh.polygons:
        poly.use_smooth = True

    try:
        mesh.calc_normals_split()
    except AttributeError:
        pass # Blender 4.1+ calculates them automatically
        
    mesh.calc_tangents()

    uv_layer = mesh.uv_layers.active.data

    # Extract vertex groups for bone data
    vertex_groups = obj.vertex_groups
    vg_map = {}
    for vg in vertex_groups:
        if vg.name.startswith('Bone_'):
            try:
                bone_idx = int(vg.name[5:])
                vg_map[vg.index] = bone_idx
            except ValueError:
                pass

    def extract_bone_data(vert_index):
        if not vg_map:
            return (0,0,0,0), (0,0,0,0)
        
        groups = eval_obj.data.vertices[vert_index].groups
        weights = []
        for g in groups:
            if g.group in vg_map:
                weights.append((vg_map[g.group], g.weight))
                
        weights.sort(key=lambda x: x[1], reverse=True)
        weights = weights[:4]
        
        if not weights:
            return (0,0,0,0), (0,0,0,0)
            
        total_weight = sum(w for i, w in weights)
        if total_weight <= 0:
            return (0,0,0,0), (0,0,0,0)
            
        norm_weights = [int(round((w / total_weight) * 255)) for i, w in weights]
        indices = [i for i, w in weights]
        
        diff = 255 - sum(norm_weights)
        if diff != 0 and norm_weights:
            norm_weights[0] += diff
            
        while len(norm_weights) < 4:
            norm_weights.append(0)
            indices.append(0)
            
        return tuple(indices), tuple(norm_weights)

    n_mats = max(len(mesh.materials), 1) if split_by_material else 1

    vert_maps = [dict() for _ in range(n_mats)]
    vert_lists = [[] for _ in range(n_mats)]
    face_lists = [[] for _ in range(n_mats)]
    uv_lists = [[] for _ in range(n_mats)]
    bone_lists = [[] for _ in range(n_mats)]
    normal_lists = [[] for _ in range(n_mats)]
    tangent_lists = [[] for _ in range(n_mats)]

    for poly in mesh.polygons:
        mat_idx = min(poly.material_index, n_mats - 1) if split_by_material else 0
        
        vm = vert_maps[mat_idx]
        vl = vert_lists[mat_idx]
        fl = face_lists[mat_idx]
        ul = uv_lists[mat_idx]
        bl = bone_lists[mat_idx]
        nl = normal_lists[mat_idx]
        tl = tangent_lists[mat_idx]

        tri = []
        for loop_idx in poly.loop_indices:
            loop = mesh.loops[loop_idx]
            v_idx = loop.vertex_index
            v = mesh.vertices[v_idx]
            
            u, v_c = uv_layer[loop_idx].uv[0], 1.0 - uv_layer[loop_idx].uv[1]
            nx, ny, nz = loop.normal[0], loop.normal[1], loop.normal[2]
            tx, ty, tz = loop.tangent[0], loop.tangent[1], loop.tangent[2]
            
            # The game engine's shader reads the W component of the tangent as an occlusion/mask multiplier.
            # If we pass -1.0 (Blender's bitangent sign for mirrored UVs), the shader multiplies the pixel by -1.0, 
            # creating massive pitch-black holes. We MUST force it to 1.0.
            tsign = 1.0
            
            # Key now includes UVs, Normals, and Tangent Sign to split along seams and mirrored UV bounds without fracturing smooth faces.
            key = (v_idx, round(u, 4), round(v_c, 4), round(nx, 4), round(ny, 4), round(nz, 4), tsign)
            
            if key not in vm:
                vm[key] = len(vl)
                vl.append((v.co.x, v.co.y, v.co.z))
                ul.append((u, v_c))
                bl.append(extract_bone_data(v_idx))
                nl.append((nx, ny, nz))
                tl.append((tx, ty, tz, tsign))
                
            tri.append(vm[key])
            
        fl.append(tuple(tri))

    eval_obj.to_mesh_clear()

    if not split_by_material:
        if len(vert_lists[0]) > 65535:
            raise ValueError(f"Mesh '{obj.name}' has {len(vert_lists[0])} vertices. Decimate below 65536.")
        return vert_lists[0], face_lists[0], uv_lists[0], bone_lists[0], normal_lists[0], tangent_lists[0]

    result = []
    for i in range(n_mats):
        if vert_lists[i] and face_lists[i]:
            if len(vert_lists[i]) > 65535:
                raise ValueError(f"Material slot {i} has {len(vert_lists[i])} vertices. Decimate below 65536.")
            result.append((vert_lists[i], face_lists[i], uv_lists[i], bone_lists[i], normal_lists[i], tangent_lists[i]))
    return result

def encode_primary_described_full_replace(
        original_gpu_bytes,
        original_primary_bytes,
        verts,
        faces,
        uvs=None,
        bone_data=None,
        normals=None,
        tangents=None,
        stream0_stride=None,
        compute_normals=True):
    import struct

    _validate_mesh(verts, faces, "primary_described_full_replace")

    if len(original_primary_bytes) < 64:
        raise ValueError("Original Primary is too small.")

    max_orig_vc = 0
    orig_s0_sz = 0
    detected_stride = None
    n_meta = len(original_primary_bytes)
    for off in range(0, n_meta - 60, 4):
        if struct.unpack_from('<I', original_primary_bytes, off)[0] == 0x0B:
            vc = struct.unpack_from('<I', original_primary_bytes, off + 7*4)[0]
            if vc > max_orig_vc:
                max_orig_vc = vc
                orig_s0_sz = struct.unpack_from('<I', original_primary_bytes, off + 4*4)[0]
                break
                
    if stream0_stride is None:
        stream0_stride = orig_s0_sz // max_orig_vc if max_orig_vc > 0 else 16

    real_nv = len(verts)

    if max_orig_vc > real_nv:
        padding_count = max_orig_vc - real_nv
        last_v = verts[-1] if verts else (0.0, 0.0, 0.0)
        verts = list(verts) + [last_v] * padding_count
        if uvs:
            last_uv = uvs[-1] if uvs else (0.0, 0.0)
            uvs = list(uvs) + [last_uv] * padding_count
        if normals:
            last_n = normals[-1] if normals else (0.0, 1.0, 0.0)
            normals = list(normals) + [last_n] * padding_count
        if tangents:
            last_t = tangents[-1] if tangents else (1.0, 0.0, 0.0, 1.0)
            tangents = list(tangents) + [last_t] * padding_count

    nv = len(verts)
    nt = len(faces)
    
    s0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs, bone_data=bone_data)
    if compute_normals:
        if not normals:
            normals = _compute_smooth_normals(verts, faces)
        s1 = _pack_stream1_with_normals(verts, normals, tangents=tangents, bone_data=bone_data)
    else:
        s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)

    ib = _pack_index_buffer_u16(faces)
    new_gpu = bytearray(s0 + s1 + ib)

    # Pad to original size
    orig_gpu_size = len(original_gpu_bytes)
    if len(new_gpu) < orig_gpu_size:
        new_gpu += b'\x00' * (orig_gpu_size - len(new_gpu))
    
    stream0_size  = nv * stream0_stride
    index_offset  = stream0_size + nv * 28
    index_count   = nt * 3

    patched_primary = bytearray(original_primary_bytes)
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', patched_primary, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', patched_primary, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', patched_primary, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', patched_primary, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                orig_ioff = struct.unpack_from('<I', patched_primary, off + 12*4)[0]
                for field_index, value in [(2,0), (4,stream0_size), (7,nv), (8,nv), (11,nv)]:
                    struct.pack_into('<I', patched_primary, off + field_index * 4, value)
                if orig_ioff < orig_gpu_size:
                    for field_index, value in [(12,index_offset), (13,index_count)]:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)

    return bytes(new_gpu), bytes(patched_primary)

def encode_primary_described_multi_submesh_replace(
        original_gpu_bytes,
        original_primary_bytes,
        submesh_list,
        stream0_stride=None,
        compute_normals=True):
    import struct

    expected_blocks = []
    n_meta = len(original_primary_bytes)
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
                expected_blocks.append((off, vc, s0_sz, fc))

    if not expected_blocks:
        raise ValueError("No valid rendering blocks found in the original Primary template.")

    if stream0_stride is None:
        detected_stride = None
        for off, vc, s0_sz, fc in expected_blocks:
            if s0_sz % vc == 0:
                cand = s0_sz // vc
                if 12 <= cand <= 64:
                    detected_stride = cand
                    break
        stream0_stride = detected_stride if detected_stride is not None else 16

    processed_subs = []
    for idx in range(len(expected_blocks)):
        off, orig_vc, s0_sz, orig_fc = expected_blocks[idx]
        if idx < len(submesh_list):
            sub = submesh_list[idx]
            if len(sub) == 6:
                verts, faces, uvs, bone_data, normals, tangents = sub
            elif len(sub) == 4:
                verts, faces, uvs, bone_data = sub
                normals = None
                tangents = None
            elif len(sub) == 3:
                verts, faces, uvs = sub
                bone_data = None
                normals = None
                tangents = None
            else:
                verts, faces = sub
                uvs = None
                bone_data = None
                normals = None
                tangents = None
                
            if orig_vc > len(verts):
                padding_count = orig_vc - len(verts)
                last_v = verts[-1] if verts else (0.0, 0.0, 0.0)
                verts = list(verts) + [last_v] * padding_count
                if uvs:
                    last_uv = uvs[-1] if uvs else (0.0, 0.0)
                    uvs = list(uvs) + [last_uv] * padding_count
                if normals:
                    last_n = normals[-1] if normals else (0.0, 1.0, 0.0)
                    normals = list(normals) + [last_n] * padding_count
                if tangents:
                    last_t = tangents[-1] if tangents else (1.0, 0.0, 0.0, 1.0)
                    tangents = list(tangents) + [last_t] * padding_count
        else:
            verts = [(0.0, 0.0, 0.0)] * orig_vc
            faces = [(0, 0, 0)] * orig_fc
            uvs = [(0.0, 0.0)] * orig_vc
            normals = None
            tangents = None
            
        processed_subs.append((verts, faces, uvs, bone_data, normals, tangents))

    new_gpu = bytearray()
    submesh_offsets = []
    current_offset = 0

    for idx, (verts, faces, uvs, bone_data, normals, tangents) in enumerate(processed_subs):
        allow_degen = (idx >= len(submesh_list))
        _validate_mesh(verts, faces, f"multi_submesh_replace submesh {idx}", allow_degenerate=allow_degen)
        nv = len(verts)
        nt = len(faces)

        s0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs, bone_data=bone_data)
        if compute_normals:
            if normals is None:
                normals = _compute_smooth_normals(verts, faces)
            s1 = _pack_stream1_with_normals(verts, normals, tangents=tangents, bone_data=bone_data)
        else:
            s1 = _pack_stream1_zeroed(verts, bone_data=bone_data)
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

    orig_gpu_size = len(original_gpu_bytes)
    if len(new_gpu) < orig_gpu_size:
        padding = orig_gpu_size - len(new_gpu)
        new_gpu += b'\x00' * padding

    patched_primary = bytearray(original_primary_bytes)
    block_index = 0
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', patched_primary, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', patched_primary, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', patched_primary, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', patched_primary, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                if block_index < len(submesh_offsets):
                    s0_start, s0_size, nv, ib_offset, ib_count = submesh_offsets[block_index]

                    target_rk = 2
                    struct.pack_into('<I', patched_primary, off + 14*4, target_rk)

                    orig_ioff = struct.unpack_from('<I', patched_primary, off + 12*4)[0]
                    fields = [
                        (2, s0_start),
                        (4, s0_size),
                        (7, nv),
                        (8, nv),
                        (11, nv),
                    ]

                    for field_index, value in fields:
                        struct.pack_into('<I', patched_primary, off + field_index * 4, value)
                        
                    if orig_ioff < orig_gpu_size:
                        for field_index, value in [(12, ib_offset), (13, ib_count)]:
                            struct.pack_into('<I', patched_primary, off + field_index * 4, value)

                    block_index += 1

    return bytes(new_gpu), bytes(patched_primary)

def patch_primary_described_positions(original_gpu_bytes, original_primary_bytes,
                                       scale_x=1.0, scale_y=1.0, scale_z=1.0):
    import struct

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
                         vx * scale_x,
                         vy * scale_y,
                         vz * scale_z)

    return bytes(patched), original_primary_bytes

def encode_cgml_primary_replace(original_gpu_bytes, original_primary_bytes, submesh_list, stream0_stride=None, compute_normals=False):
    import struct
    import math

    n_meta = len(original_primary_bytes)
    
    # 1. Parse structured arrays to find offsets
    arrays = []
    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    off = 0
    for stride in sizes:
        if off + 4 > len(original_primary_bytes):
            break
        count = struct.unpack_from('<I', original_primary_bytes, off)[0]
        base = off + 4
        end = base + count * stride
        if end > len(original_primary_bytes):
            break
        arrays.append((base, count, stride))
        off = end

    if len(arrays) != 10:
        raise ValueError("Could not find CGMeshListResource array structure.")

    array0_base, array0_count, _ = arrays[0]
    array1_base, array1_count, _ = arrays[1]
    array2_base, array2_count, _ = arrays[2]
    array5_base, array5_count, _ = arrays[5]

    # Find expected submesh blocks from Array2
    expected_blocks = []
    for i in range(array2_count):
        rec_off = array2_base + i * 0x150
        s0_sz = struct.unpack_from('<I', original_primary_bytes, rec_off + 0x130)[0]
        vc = struct.unpack_from('<I', original_primary_bytes, rec_off + 0x13c)[0]
        if s0_sz > 0 and vc > 0:
            expected_blocks.append((rec_off, vc, s0_sz))

    if not expected_blocks:
        raise ValueError("No valid rendering blocks found in Array2.")

    # Map LODs to base submeshes by comparing bounding boxes in Array0
    lod_map = {}
    base_submeshes = []
    for i in range(len(expected_blocks)):
        if i < array0_count:
            a0_off = array0_base + i * 0x98
            min_x, min_y, min_z, max_x, max_y, max_z = struct.unpack_from("<ffffff", original_primary_bytes, a0_off + 0x3c)
            cx = (min_x + max_x) * 0.5
            cy = (min_y + max_y) * 0.5
            cz = (min_z + max_z) * 0.5
            diag = math.sqrt((max_x - min_x)**2 + (max_y - min_y)**2 + (max_z - min_z)**2)
            
            is_lod = False
            for base_idx, bcx, bcy, bcz, bdiag in base_submeshes:
                if abs(cx - bcx) < 0.1 and abs(cy - bcy) < 0.1 and abs(cz - bcz) < 0.1 and abs(diag - bdiag) < 0.1:
                    lod_map[i] = base_idx
                    is_lod = True
                    break
            if not is_lod:
                lod_map[i] = i
                base_submeshes.append((i, cx, cy, cz, diag))
        else:
            lod_map[i] = i

    new_gpu = bytearray()
    submesh_meta = {}
    current_offset = 0

    patched_primary = bytearray(original_primary_bytes)

    for i, (rec_off, orig_vc, orig_s0_sz) in enumerate(expected_blocks):
        base_idx = lod_map[i]
        
        if base_idx == i:
            # Base submesh - generate unique geometry
            if base_idx < len(submesh_list):
                sub = submesh_list[base_idx]
                if len(sub) == 6:
                    verts, faces, uvs, bone_data, normals, tangents = sub
                elif len(sub) == 4:
                    verts, faces, uvs, bone_data = sub
                    normals = None
                    tangents = None
                elif len(sub) == 3:
                    verts, faces, uvs = sub
                    bone_data = None
                    normals = None
                    tangents = None
                else:
                    verts, faces = sub[0], sub[1]
                    uvs = None
                    bone_data = None
                    normals = None
                    tangents = None
                if len(verts) > 65535: raise ValueError("Submesh exceeds 65535 vertices.")
                if uvs and len(uvs) < len(verts):
                    uvs = list(uvs) + [(0.0, 0.0)] * (len(verts) - len(uvs))
            else:
                verts, faces, uvs, bone_data, normals, tangents = [(0.0, 0.0, 0.0)] * 3, [(0, 1, 2)], [(0.0, 0.0)] * 3, None, None, None

            nv = len(verts)
            nt = len(faces)

            # Read original word0 and word1 to detect static meshes and bone index locations
            orig_s0_start = struct.unpack_from('<I', original_primary_bytes, rec_off + 0x128)[0]
            s0_stride = orig_s0_sz // orig_vc if orig_vc > 0 else 52
            
            orig_word0 = 0xFFFFFFFF
            orig_word1 = 0xFFFF0000
            orig_stream0 = None
            orig_stream1 = None
            s1_stride = 28
            if orig_s0_start + s0_stride * orig_vc <= len(original_gpu_bytes):
                orig_word0, orig_word1 = struct.unpack_from('<II', original_gpu_bytes, orig_s0_start)
                orig_stream0 = original_gpu_bytes[orig_s0_start : orig_s0_start + s0_stride * orig_vc]
                
                orig_s1_start = orig_s0_start + orig_s0_sz
                if orig_s1_start + s1_stride * orig_vc <= len(original_gpu_bytes):
                    orig_stream1 = original_gpu_bytes[orig_s1_start : orig_s1_start + s1_stride * orig_vc]
            
            if base_idx < len(submesh_list):
                # Never merge original stream data with a Blender mesh, the vertex order is completely different!
                orig_stream0 = None
                orig_stream1 = None
            
            s0 = _pack_stream0_dynamic(nv, s0_stride, uvs=uvs, bone_data=bone_data, orig_word1=orig_word1, orig_word0=orig_word0, orig_stream0=orig_stream0)
            
            is_static = (orig_word1 == 0xFFFF0000)
            
            if compute_normals:
                if normals is None:
                    normals = _compute_smooth_normals(verts, faces)
                s1 = _pack_stream1_with_normals(verts, normals, tangents=tangents, bone_data=bone_data, orig_stream1=orig_stream1, s1_stride=s1_stride, force_snorm16=is_static)
            else:
                s1 = _pack_stream1_zeroed(verts, bone_data=bone_data, orig_stream1=orig_stream1, s1_stride=s1_stride)
            ib = _pack_index_buffer_u16(faces)

            # Pad each submesh to 4-byte alignment
            sub_binary = s0 + s1 + ib
            rem = len(sub_binary) % 4
            if rem != 0: sub_binary += b"\x00" * (4 - rem)

            s0_start = current_offset
            s0_size = len(s0)
            s1_size = len(s1)
            ib_offset = s0_start + s0_size + s1_size
            ib_count = nt * 3

            new_gpu += sub_binary
            current_offset += len(sub_binary)
            
            submesh_meta[i] = (s0_start, s0_size, ib_offset, ib_count, nv, verts)
        else:
            # LOD - alias the base submesh geometry!
            s0_start, s0_size, ib_offset, ib_count, nv, verts = submesh_meta[base_idx]

        # Patch Array 2
        struct.pack_into("<I", patched_primary, rec_off + 0x128, s0_start)
        struct.pack_into("<I", patched_primary, rec_off + 0x130, s0_size)
        struct.pack_into("<I", patched_primary, rec_off + 0x13c, nv)
        struct.pack_into("<I", patched_primary, rec_off + 0x140, nv)
        struct.pack_into("<I", patched_primary, rec_off + 0x14c, nv)

        # Patch Array 5
        if i < array5_count:
            range_rec_off = array5_base + i * 0x10
            orig_ic = struct.unpack_from("<I", patched_primary, range_rec_off + 4)[0]
            
            struct.pack_into("<I", patched_primary, range_rec_off, ib_offset)
            struct.pack_into("<I", patched_primary, range_rec_off + 4, ib_count)
            
            # Patch Array 1 using orig_vc and orig_ic matching
            for a1_idx in range(array1_count):
                stream_rec_off = array1_base + a1_idx * 0x70
                a1_vc = struct.unpack_from("<I", patched_primary, stream_rec_off + 0x48)[0]
                a1_ic = struct.unpack_from("<I", patched_primary, stream_rec_off + 0x50)[0]
                if a1_vc == orig_vc and a1_ic == orig_ic:
                    struct.pack_into("<I", patched_primary, stream_rec_off + 0x48, nv)
                    struct.pack_into("<I", patched_primary, stream_rec_off + 0x50, ib_count)

        # Patch Array 0 bounds
        if i < array0_count:
            a0_off = array0_base + i * 0x98
            xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_z, max_z = min(zs), max(zs)
            struct.pack_into("<ffffff", patched_primary, a0_off + 0x3c, min_x, min_y, min_z, max_x, max_y, max_z)

    # Pad GPU binary to exact original size
    orig_gpu_size = len(original_gpu_bytes)
    if len(new_gpu) < orig_gpu_size:
        new_gpu += b"\x00" * (orig_gpu_size - len(new_gpu))
    elif len(new_gpu) > orig_gpu_size:
        raise VertexLimitError(len(new_gpu), max_verts=orig_gpu_size, mesh_name="GPU Size Limit")

    return bytes(new_gpu), bytes(patched_primary)


