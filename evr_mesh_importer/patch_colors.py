import os

def patch_decode():
    path = r'j:\EchoVR-Tools-Launcher\evr-mesh-importer\evr_mesh_importer\decode.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 1. _extract_submesh
    content = content.replace(
        'uvs = []\n    bone_data = []\n    has_valid_bones = False\n',
        'uvs = []\n    bone_data = []\n    colors = []\n    has_valid_bones = False\n'
    )
    content = content.replace(
        '            bone_data.append((indices, weights))',
        '            bone_data.append((indices, weights))\n            w0 = struct.unpack_from("<4B", data, s0_off)\n            w1 = struct.unpack_from("<4B", data, s0_off + 4)\n            colors.append(((w0[0]/255.0, w0[1]/255.0, w0[2]/255.0, w0[3]/255.0), (w1[0]/255.0, w1[1]/255.0, w1[2]/255.0, w1[3]/255.0)))'
    )
    content = content.replace(
        '        uvs = [(0.0, 0.0)] * Nv\n        bone_data = [((0,0,0,0), (0,0,0,0))] * Nv',
        '        uvs = [(0.0, 0.0)] * Nv\n        bone_data = [((0,0,0,0), (0,0,0,0))] * Nv\n        colors = [((0.0,0.0,0.0,0.0), (0.0,0.0,0.0,0.0))] * Nv'
    )
    content = content.replace(
        '    return verts, faces, uvs, bone_data',
        '    return verts, faces, uvs, bone_data, colors'
    )

    # 2. _extract_primary_described_cimr_mesh
    content = content.replace(
        '            uvs = []\n            bone_data = []\n            has_valid_bones = False',
        '            uvs = []\n            bone_data = []\n            colors = []\n            has_valid_bones = False'
    )
    content = content.replace(
        '                    bone_data.append((indices, weights))\n                    if sum(weights) > 50:\n                        has_valid_bones = True',
        '                    bone_data.append((indices, weights))\n                    w0 = struct.unpack_from("<4B", gpu_data, s0_off)\n                    w1 = struct.unpack_from("<4B", gpu_data, s0_off + 4)\n                    colors.append(((w0[0]/255.0, w0[1]/255.0, w0[2]/255.0, w0[3]/255.0), (w1[0]/255.0, w1[1]/255.0, w1[2]/255.0, w1[3]/255.0)))\n                    if sum(weights) > 50:\n                        has_valid_bones = True'
    )
    content = content.replace(
        '                uvs = [(0.0, 0.0)] * vertex_count\n                bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count',
        '                uvs = [(0.0, 0.0)] * vertex_count\n                bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count\n                colors = [((0.0,0.0,0.0,0.0), (0.0,0.0,0.0,0.0))] * vertex_count'
    )
    content = content.replace(
        '            candidate = (len(faces), vertex_count, -pos_off, verts, faces, uvs, bone_data)',
        '            candidate = (len(faces), vertex_count, -pos_off, verts, faces, uvs, bone_data, colors)'
    )

    # 3. _extract_cgml_ranges
    content = content.replace(
        '    uvs = []\n    bone_data = []\n    has_valid_bones = False',
        '    uvs = []\n    bone_data = []\n    colors = []\n    has_valid_bones = False'
    )
    content = content.replace(
        '            bone_data.append((indices, weights))\n            if sum(weights) > 50:\n                has_valid_bones = True',
        '            bone_data.append((indices, weights))\n            w0 = struct.unpack_from("<4B", gpu_data, s0_off)\n            w1 = struct.unpack_from("<4B", gpu_data, s0_off + 4)\n            colors.append(((w0[0]/255.0, w0[1]/255.0, w0[2]/255.0, w0[3]/255.0), (w1[0]/255.0, w1[1]/255.0, w1[2]/255.0, w1[3]/255.0)))\n            if sum(weights) > 50:\n                has_valid_bones = True'
    )
    content = content.replace(
        '        uvs = [(0.0, 0.0)] * vertex_count\n        bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count',
        '        uvs = [(0.0, 0.0)] * vertex_count\n        bone_data = [((0,0,0,0), (0,0,0,0))] * vertex_count\n        colors = [((0.0,0.0,0.0,0.0), (0.0,0.0,0.0,0.0))] * vertex_count'
    )
    content = content.replace(
        '    return verts, faces, uvs, bone_data',
        '    return verts, faces, uvs, bone_data, colors'
    )

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def patch_init():
    path = r'j:\EchoVR-Tools-Launcher\evr-mesh-importer\evr_mesh_importer\__init__.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Import
    content = content.replace(
        '            bone_data = sub[3] if len(sub) > 3 else None\n            if not verts or not faces: continue',
        '            bone_data = sub[3] if len(sub) > 3 else None\n            colors = sub[4] if len(sub) > 4 else None\n            if not verts or not faces: continue'
    )
    
    color_inject = """            if not self.use_smooth: _apply_flat_shading(obj)
            
            if colors and obj.data:
                if hasattr(obj.data, "color_attributes"):
                    color_layer0 = obj.data.color_attributes.new(name="word0", type='BYTE_COLOR', domain='CORNER')
                    color_layer1 = obj.data.color_attributes.new(name="word1", type='BYTE_COLOR', domain='CORNER')
                    for poly in obj.data.polygons:
                        for loop_idx in poly.loop_indices:
                            v_idx = obj.data.loops[loop_idx].vertex_index
                            if v_idx < len(colors):
                                color_layer0.data[loop_idx].color = colors[v_idx][0]
                                color_layer1.data[loop_idx].color = colors[v_idx][1]
"""
    content = content.replace('            if not self.use_smooth: _apply_flat_shading(obj)\n', color_inject)

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

def patch_encode():
    path = r'j:\EchoVR-Tools-Launcher\evr-mesh-importer\evr_mesh_importer\encode.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # pack_stream0_dynamic
    content = content.replace(
        'def _pack_stream0_dynamic(vertex_count, stride, uvs=None, bone_data=None, orig_word1=0xFFFF0000, orig_word0=0x00000000, orig_stream0=None):',
        'def _pack_stream0_dynamic(vertex_count, stride, uvs=None, bone_data=None, colors=None, orig_word1=0xFFFF0000, orig_word0=0x00000000, orig_stream0=None):'
    )
    content = content.replace(
        '    if stride == 16:\n        return _pack_stream0_s16(vertex_count, uvs=uvs, bone_data=bone_data)\n    elif stride == 20:\n        return _pack_stream0_s20_white(vertex_count, uvs=uvs, bone_data=bone_data)',
        '    if stride == 16:\n        return _pack_stream0_s16(vertex_count, uvs=uvs, bone_data=bone_data, colors=colors)\n    elif stride == 20:\n        return _pack_stream0_s20_white(vertex_count, uvs=uvs, bone_data=bone_data, colors=colors)'
    )
    
    word_logic = """        if bone_data and i < len(bone_data) and sum(bone_data[i][1]) > 0:
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
            if colors and i < len(colors):
                c0, c1 = colors[i]
                word0 = (int(c0[3]*255+0.5) << 24) | (int(c0[2]*255+0.5) << 16) | (int(c0[1]*255+0.5) << 8) | int(c0[0]*255+0.5)
                word1 = (int(c1[3]*255+0.5) << 24) | (int(c1[2]*255+0.5) << 16) | (int(c1[1]*255+0.5) << 8) | int(c1[0]*255+0.5)
            else:
                word0 = orig_word0 if stride >= 24 else 0x00000000
                word1 = orig_word1"""
    
    # Needs careful replacement for bone_data logic in _pack_stream0_dynamic
    import re
    content = re.sub(r'        if bone_data and i < len\(bone_data\):.*?            word1 = orig_word1', word_logic, content, flags=re.DOTALL)
    
    # Do the same for _pack_stream0_s16 and _pack_stream0_s20_white
    content = content.replace('def _pack_stream0_s16(vertex_count, uvs=None, bone_data=None):', 'def _pack_stream0_s16(vertex_count, uvs=None, bone_data=None, colors=None):')
    content = content.replace('def _pack_stream0_s20_white(vertex_count, uvs=None, bone_data=None):', 'def _pack_stream0_s20_white(vertex_count, uvs=None, bone_data=None, colors=None):')
    
    s16_logic = """        if bone_data and i < len(bone_data) and sum(bone_data[i][1]) > 0:
            indices, weights = bone_data[i]
            b0, b1, b2, b3 = weights
            word0 = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
            word1 = 0x00000000
        else:
            if colors and i < len(colors):
                c0, c1 = colors[i]
                word0 = (int(c0[3]*255+0.5) << 24) | (int(c0[2]*255+0.5) << 16) | (int(c0[1]*255+0.5) << 8) | int(c0[0]*255+0.5)
                word1 = (int(c1[3]*255+0.5) << 24) | (int(c1[2]*255+0.5) << 16) | (int(c1[1]*255+0.5) << 8) | int(c1[0]*255+0.5)
            else:
                word0 = 0x00000000
                word1 = 0x00000000"""
    content = re.sub(r'        if bone_data and i < len\(bone_data\):.*?            word1 = 0x00000000', s16_logic, content, flags=re.DOTALL)
    
    # Mesh from blender object
    content = content.replace('uv_layer = bm.loops.layers.uv.active', 'uv_layer = bm.loops.layers.uv.active\n    color_layer0 = bm.loops.layers.color.get("word0")\n    color_layer1 = bm.loops.layers.color.get("word1")')
    
    color_ext_notsplit = """        uvs = []
        bone_data = []
        colors = []
        if uv_layer:
            for v in bm.verts:
                uv = (0.0, 0.0)
                if v.link_loops:
                    loop = v.link_loops[0]
                    uv = (loop[uv_layer].uv.x, loop[uv_layer].uv.y)
                    c0 = loop[color_layer0] if color_layer0 else (0.0, 0.0, 0.0, 0.0)
                    c1 = loop[color_layer1] if color_layer1 else (0.0, 0.0, 0.0, 0.0)
                else:
                    c0, c1 = (0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)
                uvs.append(uv)
                colors.append((c0, c1))
                bone_data.append(extract_bone_data(v.index))
        else:
            uvs = [(0.0, 0.0)] * len(verts)
            colors = [((0.0,0.0,0.0,0.0), (0.0,0.0,0.0,0.0))] * len(verts)
            bone_data = [extract_bone_data(v.index) for v in bm.verts]

        bm.free()
        if len(verts) > 65535:
            raise VertexLimitError(len(verts), mesh_name=obj.name)
        return verts, faces, uvs, bone_data, colors"""
    
    content = re.sub(r'        uvs = \[\]\n        bone_data = \[\]\n        if uv_layer:.*?return verts, faces, uvs, bone_data', color_ext_notsplit, content, flags=re.DOTALL)
    
    color_ext_split1 = """    vert_maps = [dict() for _ in range(n_mats)]
    vert_lists = [[] for _ in range(n_mats)]
    face_lists = [[] for _ in range(n_mats)]
    uv_lists = [[] for _ in range(n_mats)]
    bone_lists = [[] for _ in range(n_mats)]
    color_lists = [[] for _ in range(n_mats)]"""
    content = content.replace('    vert_maps = [dict() for _ in range(n_mats)]\n    vert_lists = [[] for _ in range(n_mats)]\n    face_lists = [[] for _ in range(n_mats)]\n    uv_lists = [[] for _ in range(n_mats)]\n    bone_lists = [[] for _ in range(n_mats)]', color_ext_split1)
    
    color_ext_split2 = """        vm = vert_maps[mat_idx]
        vl = vert_lists[mat_idx]
        fl = face_lists[mat_idx]
        ul = uv_lists[mat_idx]
        bl = bone_lists[mat_idx]
        cl = color_lists[mat_idx]

        tri = []
        for loop in face.loops:
            v = loop.vert
            uv = (loop[uv_layer].uv.x, loop[uv_layer].uv.y) if uv_layer else (0.0, 0.0)
            c0 = loop[color_layer0] if color_layer0 else (0.0, 0.0, 0.0, 0.0)
            c1 = loop[color_layer1] if color_layer1 else (0.0, 0.0, 0.0, 0.0)
            key = (round(v.co.x, 4), round(v.co.y, 4), round(v.co.z, 4), round(uv[0], 4), round(uv[1], 4), c0, c1)
            if key not in vm:
                vm[key] = len(vl)
                vl.append((v.co.x, v.co.y, v.co.z))
                ul.append(uv)
                bl.append(extract_bone_data(v.index))
                cl.append((c0, c1))
            tri.append(vm[key])
        fl.append(tuple(tri))"""
    content = re.sub(r'        vm = vert_maps\[mat_idx\].*?        fl.append\(tuple\(tri\)\)', color_ext_split2, content, flags=re.DOTALL)
    
    color_ext_split3 = """        if vert_lists[i] and face_lists[i]:
            if len(vert_lists[i]) > 65535:
                raise VertexLimitError(len(vert_lists[i]), mesh_name=f"Material slot {i} of '{obj.name}'")
            result.append((vert_lists[i], face_lists[i], uv_lists[i], bone_lists[i], color_lists[i]))"""
    content = content.replace('        if vert_lists[i] and face_lists[i]:\n            if len(vert_lists[i]) > 65535:\n                raise VertexLimitError(len(vert_lists[i]), mesh_name=f"Material slot {i} of \'{obj.name}\'")\n            result.append((vert_lists[i], face_lists[i], uv_lists[i], bone_lists[i]))', color_ext_split3)
    
    # Finally, add colors to encode_* function signatures
    content = content.replace('encode_heuristic_s16(verts, faces, uvs=None, bone_data=None, compute_normals=True):', 'encode_heuristic_s16(verts, faces, uvs=None, bone_data=None, colors=None, compute_normals=True):')
    content = content.replace('s0 = _pack_stream0_s16(len(verts), uvs=uvs, bone_data=bone_data)', 's0 = _pack_stream0_s16(len(verts), uvs=uvs, bone_data=bone_data, colors=colors)')
    
    content = content.replace('encode_heuristic_s20(verts, faces, uvs=None, bone_data=None, compute_normals=True):', 'encode_heuristic_s20(verts, faces, uvs=None, bone_data=None, colors=None, compute_normals=True):')
    content = content.replace('s0 = _pack_stream0_s20_white(len(verts), uvs=uvs, bone_data=bone_data)', 's0 = _pack_stream0_s20_white(len(verts), uvs=uvs, bone_data=bone_data, colors=colors)')

    content = content.replace('encode_primary_described(verts, faces, uvs=None, bone_data=None, stream0_stride=16, compute_normals=True):', 'encode_primary_described(verts, faces, uvs=None, bone_data=None, colors=None, stream0_stride=16, compute_normals=True):')
    content = content.replace('s0 = _pack_stream0_s16(nv, uvs=uvs)', 's0 = _pack_stream0_s16(nv, uvs=uvs, colors=colors, bone_data=bone_data)')
    content = content.replace('s0 = _pack_stream0_s20_white(nv, uvs=uvs)', 's0 = _pack_stream0_s20_white(nv, uvs=uvs, colors=colors, bone_data=bone_data)')
    
    content = content.replace('encode_primary_described_full_replace(\n        original_gpu_bytes,\n        original_primary_bytes,\n        verts,\n        faces,\n        uvs=None,\n        bone_data=None,\n', 'encode_primary_described_full_replace(\n        original_gpu_bytes,\n        original_primary_bytes,\n        verts,\n        faces,\n        uvs=None,\n        bone_data=None,\n        colors=None,\n')
    content = content.replace('s0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs, bone_data=bone_data)', 's0 = _pack_stream0_dynamic(nv, stream0_stride, uvs=uvs, bone_data=bone_data, colors=colors)')
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == "__main__":
    patch_decode()
    patch_encode()
    patch_init()
    print("Patched!")
