"""
EVR Raw Mesh Importer/Exporter — Blender add-on
Imports and exports raw GPU mesh binaries from Echo VR (Echo Arena).
https://github.com/Dualgame/evr-mesh-importer
"""

bl_info = {
    "name": "EVR Raw Mesh Importer",
    "author": "Dualgame",
    "version": (1, 1, 1),
    "blender": (3, 0, 0),
    "location": "File > Import/Export > EVR Raw Mesh",
    "description": "Import/export raw GPU mesh binaries from Echo VR (Echo Arena)",
    "doc_url": "https://github.com/Dualgame/evr-mesh-importer",
    "tracker_url": "https://github.com/Dualgame/evr-mesh-importer/issues",
    "category": "Import-Export",
}

import bpy
import os
import sys
import importlib
import struct

# Support reloading submodules to prevent import errors when upgrading the addon in Blender
_submodules = ["decode", "primary", "encode"]
for _sub in _submodules:
    _full_name = f"{__package__}.{_sub}" if __package__ else _sub
    if _full_name in sys.modules:
        importlib.reload(sys.modules[_full_name])

from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (
    StringProperty, BoolProperty, FloatProperty,
    CollectionProperty, EnumProperty,
)
from bpy.types import Operator

from .decode import extract_mesh
from .primary import _find_primary_data
from .textures import apply_textures_to_objects
from .encode import (
    encode_heuristic_s16,
    encode_heuristic_s20,
    encode_heuristic_dual28,
    encode_primary_described,
    encode_cgml,
    encode_cgml_inplace_replace,  # ADD THIS
    mesh_from_blender_object,
    encode_primary_described_full_replace,
    encode_primary_described_multi_submesh_replace,
    patch_primary_described_positions,
)

# ============================================================
# Helper: detect if a Primary file is CGMeshListResource
# ============================================================

def _is_cgml_primary(primary_bytes):
    """
    Detect whether a Primary binary belongs to a CGMeshListResource (CGML)
    as opposed to a CGInstancedModelResource (CIMR).
    
    CGML Primary files have a distinctive 10-array structured layout at the
    beginning, followed by a tail signature. CIMR files have raw 0x0B
    descriptor blocks scattered throughout.
    
    Returns True if the Primary appears to be CGML.
    """
    n_meta = len(primary_bytes)
    if n_meta < 0x310:
        return False
    
    # CGML files have a 10-array layout with these strides
    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    off = 0
    for stride in sizes:
        if off + 4 > n_meta:
            return False
        count = struct.unpack_from("<I", primary_bytes, off)[0]
        # Sanity: count should be reasonable
        if count > 1000:
            return False
        base = off + 4
        end = base + count * stride
        if end > n_meta:
            return False
        off = end
    
    # After the 10 arrays, there should be a tail with signature
    if off + 16 <= n_meta:
        tail_a, mesh_data_end, gpu_size = struct.unpack_from("<IIQ", primary_bytes, off)
        # tail_a is typically 0 or 5 for valid CGML
        if tail_a in (0, 5) and gpu_size > 0:
            return True
    
    return False


# ============================================================
# Helper: detect stream-0 stride from a Primary file
# ============================================================

def _detect_s0_stride_from_primary(primary_bytes):
    """
    Scan 0xFFFFFF0C descriptor blocks in a Primary file to determine the
    stream-0 stride used by the original mesh.
    
    Returns the stride (e.g. 16, 20) or None if undetectable.
    """
    n_meta = len(primary_bytes)
    for off in range(0, n_meta - 56, 4):
        vals = [struct.unpack_from("<I", primary_bytes, off + i*4)[0] for i in range(14)]
        if vals[0] == 0xFFFFFF0C and vals[1] == 0xFFFFFFFF:
            if vals[2] in (0x0B, 0x0D) and vals[3] == 0:
                vc = vals[9]
                vc2 = vals[10]
                if vc == vc2 and vc > 0:
                    s0_size = vals[6]
                    if s0_size % vc == 0:
                        stride = s0_size // vc
                        if stride in (12, 16, 20, 24, 28, 32, 44):
                            return stride
    return None
 
 
def _detect_cgml_stride(primary_bytes):
    """
    Detect stream-0 stride from a CGML Primary file's Array 2 descriptor.
    
    Returns the stride (e.g. 16, 20, 44) or None if undetectable.
    """
    n_meta = len(primary_bytes)
    sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
    off = 0
    arrays = []
    for stride in sizes:
        if off + 4 > n_meta:
            return None
        count = struct.unpack_from("<I", primary_bytes, off)[0]
        base = off + 4
        end = base + count * stride
        if end > n_meta:
            return None
        arrays.append((base, count, stride))
        off = end
        
    if len(arrays) == 10 and arrays[2][1] > 0:
        a2_base, a2_count, a2_stride = arrays[2]
        rec_off = a2_base
        s0_size = struct.unpack_from("<I", primary_bytes, rec_off + 0x130)[0]
        vc = struct.unpack_from("<I", primary_bytes, rec_off + 0x13c)[0]
        if vc > 0 and s0_size % vc == 0:
            stride = s0_size // vc
            if 12 <= stride <= 64:
                return stride
    return None


# ============================================================
# Helper: count rendering blocks in a CIMR Primary file
# ============================================================

def _count_cimr_blocks(primary_bytes):
    """Count the number of 0x0B rendering blocks in a CIMR Primary file."""
    n_meta = len(primary_bytes)
    count = 0
    for off in range(0, n_meta - 60, 4):
        val = struct.unpack_from('<I', primary_bytes, off)[0]
        if val == 0x0B:
            vc = struct.unpack_from('<I', primary_bytes, off + 7*4)[0]
            vc2 = struct.unpack_from('<I', primary_bytes, off + 8*4)[0]
            vc3 = struct.unpack_from('<I', primary_bytes, off + 11*4)[0]
            if vc == vc2 == vc3 and vc > 0:
                count += 1
    return count


# ============================================================
# Mesh building helper
# ============================================================

def _build_blender_mesh(verts, faces, name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def _apply_flat_shading(obj):
    for poly in obj.data.polygons:
        poly.use_smooth = False


# ============================================================
# Import operator
# ============================================================

class EVR_OT_ImportMesh(Operator, ImportHelper):
    """Import a raw EVR GPU mesh binary (CIMR, CGML, or heuristic)"""
    bl_idname = "import_mesh.evr_raw"
    bl_label = "EVR Raw Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(
        default="*",
        options={'HIDDEN'},
        maxlen=255,
    )

    files: CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    )

    directory: StringProperty(subtype='DIR_PATH')

    primary_file: StringProperty(
        name="Primary File (optional)",
        description=(
            "Path to the matching Primary binary for this GPU file. "
            "Leave blank to auto-discover or use GPU-only heuristics."
        ),
        default="",
        subtype='FILE_PATH',
    )

    auto_find_primary: BoolProperty(
        name="Auto-find Primary",
        description=(
            "Try to locate a matching Primary binary in the sibling Primary/ "
            "directory automatically (mirrors project directory layout)."
        ),
        default=True,
    )

    use_smooth: BoolProperty(
        name="Smooth Shading",
        description="Apply smooth shading to imported meshes",
        default=False,
    )

    scale: FloatProperty(
        name="Scale",
        description="Uniform scale factor applied to all vertices",
        default=1.0,
        min=0.0001,
        max=10000.0,
        soft_min=0.01,
        soft_max=100.0,
    )

    pcvr_extracted_dir: StringProperty(
        name="pcvr-extracted Folder",
        description=(
            "Optional. Path to the pcvr-extracted directory containing 'c2434c5a99e139ce'. "
            "Leave blank to auto-discover."
        ),
        default="",
        subtype='DIR_PATH',
    )

    texture_cache_dir: StringProperty(
        name="Texture Cache Folder",
        description=(
            "Optional. Path to the texture_cache directory containing high-res PNGs. "
            "Leave blank to auto-discover."
        ),
        default="",
        subtype='DIR_PATH',
    )

    import_lods: BoolProperty(
        name="Import LODs",
        description="Import lower-detail Level of Detail (LOD) meshes overlaying the primary mesh",
        default=False,
    )


    @classmethod
    def poll(cls, context):
        if bpy.app.background:
            return True
        return context.area is not None

    def execute(self, context):
        if self.files:
            paths = [os.path.join(self.directory, f.name) for f in self.files]
        else:
            paths = [self.filepath]

        primary_data = None
        if self.primary_file.strip():
            ppath = bpy.path.abspath(self.primary_file)
            if os.path.isfile(ppath):
                with open(ppath, "rb") as fh:
                    primary_data = fh.read()
            else:
                self.report({'WARNING'}, f"Primary file not found: {ppath}")

        all_roots = []
        for gpu_path in paths:
            root = self._import_single(context, gpu_path, primary_data)
            if root:
                all_roots.append(root)

        for obj in context.selected_objects:
            obj.select_set(False)
        for root in all_roots:
            root.select_set(True)
        if all_roots:
            context.view_layer.objects.active = all_roots[-1]

        if not all_roots:
            return {'CANCELLED'}
        return {'FINISHED'}

    def _import_single(self, context, gpu_path, primary_data=None):
        if not os.path.isfile(gpu_path):
            self.report({'ERROR'}, f"File not found: {gpu_path}")
            return None

        try:
            submeshes, path_label = extract_mesh(
                gpu_path,
                primary_data=primary_data,
                auto_find_primary=(primary_data is None and self.auto_find_primary),
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Decode failed: {exc}")
            return None

        if not submeshes:
            self.report({'WARNING'}, f"{os.path.basename(gpu_path)}: no geometry decoded.")
            return None

        # Universal LOD Deduplication
        if not self.import_lods and path_label == "cginst":
            groups = {}
            for idx, sub in enumerate(submeshes):
                group_id = getattr(sub, "group_id", None)
                if isinstance(group_id, str) and "_" in group_id:
                    comp_id = group_id.split("_")[0]
                    if comp_id not in groups:
                        groups[comp_id] = []
                    groups[comp_id].append(sub)
                else:
                    unique_id = f"unique_{idx}"
                    groups[unique_id] = [sub]

            deduped = []
            for comp_id, group in groups.items():
                best_sub = max(group, key=lambda s: len(s[0]))
                deduped.append(best_sub)

            submeshes = deduped

        # Build logical material index map based on submesh group IDs to correctly support multi-material models
        # Preserve original order of appearance to avoid alphabetical sorting bugs
        unique_group_ids = []
        for sub in submeshes:
            group_id = getattr(sub, "group_id", None)
            if group_id is None:
                group_id = getattr(sub, "index_offset", None)
            if group_id is None:
                group_id = len(sub[1]) if len(sub) > 1 else 0
            if group_id not in unique_group_ids:
                unique_group_ids.append(group_id)

        group_id_to_mat_idx = {gid: i for i, gid in enumerate(unique_group_ids)}

        base_name = os.path.splitext(os.path.basename(gpu_path))[0]
        valid = [(sub[0], sub[1]) for sub in submeshes if len(sub) >= 2 and sub[0] and sub[1]]

        parent_empty = None
        if len(valid) > 1:
            parent_empty = bpy.data.objects.new(base_name, None)
            parent_empty.empty_display_type = 'PLAIN_AXES'
            parent_empty.empty_display_size = 0.01
            bpy.context.collection.objects.link(parent_empty)

        created = []
        for idx, sub in enumerate(submeshes):
            if len(sub) == 3:
                verts, faces, uvs = sub
            else:
                verts, faces = sub
                uvs = None

            if not verts or not faces:
                continue

            if self.scale != 1.0:
                s = self.scale
                verts = [(x * s, y * s, z * s) for x, y, z in verts]

            name = base_name if parent_empty is None else f"{base_name}.{idx:03d}"
            obj = _build_blender_mesh(verts, faces, name)

            # Store logical material index on the object to ensure correct texture assignment in apply_textures_to_objects
            group_id = getattr(sub, "group_id", None)
            if group_id is None:
                group_id = getattr(sub, "index_offset", None)
            if group_id is None:
                group_id = len(sub[1]) if len(sub) > 1 else idx
            obj["evr_material_index"] = group_id_to_mat_idx.get(group_id, 0)


            if uvs and obj.data:
                uv_layer = obj.data.uv_layers.new(name="UVMap")
                for poly in obj.data.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = obj.data.loops[loop_idx]
                        v_idx = loop.vertex_index
                        if v_idx < len(uvs):
                            # Flip the V-coordinate (1.0 - V) for correct Blender viewport texturing
                            uv_layer.data[loop_idx].uv = (uvs[v_idx][0], 1.0 - uvs[v_idx][1])
                obj.data["evr_uv_flipped"] = True

            if not self.use_smooth:
                _apply_flat_shading(obj)
            if parent_empty is not None:
                obj.parent = parent_empty
            created.append(obj)

        if not created:
            if parent_empty is not None:
                bpy.data.objects.remove(parent_empty, do_unlink=True)
            self.report({'WARNING'}, f"{base_name}: all submeshes were empty.")
            return None

        total_tris = sum(len(obj.data.polygons) for obj in created)
        self.report(
            {'INFO'},
            f"{base_name}: {len(created)} mesh(es) {total_tris}f [{path_label}]",
        )

        # Automatically map PBR textures to imported meshes
        model_hash = base_name.split('.')[0]
        try:
            mats_count = apply_textures_to_objects(
                model_hash=model_hash,
                pcvr_extracted_dir=self.pcvr_extracted_dir,
                texture_cache_dir=self.texture_cache_dir,
                target_objects=created
            )
            if mats_count > 0:
                self.report({'INFO'}, f"Successfully applied {mats_count} PBR materials to mesh.")
        except Exception as e:
            self.report({'WARNING'}, f"Failed to auto-apply PBR textures: {e}")

        return parent_empty if parent_empty is not None else created[0]

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "auto_find_primary")
        layout.prop(self, "primary_file")
        layout.separator()
        layout.prop(self, "use_smooth")
        layout.prop(self, "scale")
        layout.separator()
        layout.label(text="PBR Textures (Optional):")
        layout.prop(self, "pcvr_extracted_dir")
        layout.prop(self, "texture_cache_dir")


def menu_func_import(self, context):
    self.layout.operator(EVR_OT_ImportMesh.bl_idname, text="EVR Raw Mesh")


# ============================================================
# Export operator
# ============================================================

class EVR_OT_ExportMesh(Operator, ExportHelper):
    """Export the active Blender mesh as a replacement EVR GPU binary"""
    bl_idname = "export_mesh.evr_raw"
    bl_label = "EVR Raw Mesh (Replace)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ""
    filter_glob: StringProperty(default="*", options={'HIDDEN'}, maxlen=255)

    encode_mode: EnumProperty(
        name="Encode Mode",
        description=(
            "Binary layout to write. Match this to the decode path shown in "
            "the Blender Info bar when you originally imported the file."
        ),
        items=[
            ('heuristic_s16',   "Heuristic — stride-16 (most props)",
             "Stream-0 uses 16-byte 0xFFFFFFFF records. "
             "Use for files that decoded as 'heuristic' with stride 16/52/36."),
            ('heuristic_s20',   "Heuristic — stride-20 (vertex-colored)",
             "Stream-0 uses 20-byte all-white color records. "
             "Use for files decoded via the vertex-colored heuristic path."),
            ('heuristic_dual28', "Heuristic — dual-28 (0xFF prefix both streams)",
             "Both stream-0 and stream-1 use stride-28. "
             "Use for files decoded via _extract_dual28_prefixed_mesh."),
            ('primary_described', "Primary Described (auto-detect CGML/CIMR, in-place patch)",
             "Automatically detects whether the original is CGML or CIMR and "
             "surgically patches the GPU + Primary files in-place. "
             "Safest option for all model types."),
            ('primary_described_patch', "Primary Described (Scale Only)",
             "Surgically scales vertex positions inside the original GPU binary in-place, "
             "leaving file size and LODs/shadow geometry completely original. "
             "Use to prevent load crashes on complex instanced props."),
            ('cgml',            "CGML — map geometry (multi-submesh, new file)",
             "Encodes each material slot as a separate submesh. "
             "Writes a new standalone GPU binary without patching the Primary."),
        ],
        default='primary_described',
    )

    compute_normals: BoolProperty(
        name="Compute Normals",
        description=(
            "Compute smooth vertex normals from mesh topology and pack them "
            "into stream-1 (snorm16). Disable to zero stream-1 normals — "
            "safe for a first geometry test, but lighting will be wrong in-game."
        ),
        default=True,
    )

    write_primary: BoolProperty(
        name="Write Primary File",
        description=(
            "Also write a matching Primary binary to the sibling Primary/ "
            "directory. Required for 'primary_described' mode."
        ),
        default=True,
    )

    stream0_stride: EnumProperty(
        name="Stream-0 Stride",
        description="Stride of the original file's stream-0 (Auto-detect, 16, 20, or 44 bytes).",
        items=[
            ('auto', "Auto-Detect", "Automatically detect from the original Primary template file"),
            ('16', "16 bytes", "Original used stride-16 stream-0"),
            ('20', "20 bytes", "Original used stride-20 stream-0"),
            ('44', "44 bytes", "Original used stride-44 stream-0 (props/environment)"),
        ],
        default='auto',
    )

    scale: FloatProperty(
        name="Scale",
        description="Uniform scale applied to all vertex positions before export",
        default=1.0,
        min=0.0001,
        max=10000.0,
        soft_min=0.01,
        soft_max=100.0,
    )

    scale_x: FloatProperty(
        name="Scale X",
        description="Scale factor applied to X coordinates during in-place patching",
        default=1.0,
    )

    scale_y: FloatProperty(
        name="Scale Y",
        description="Scale factor applied to Y coordinates during in-place patching",
        default=1.0,
    )

    scale_z: FloatProperty(
        name="Scale Z",
        description="Scale factor applied to Z coordinates during in-place patching",
        default=1.0,
    )

    @classmethod
    def poll(cls, context):
        return (context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        obj = context.active_object
        out_path = bpy.path.abspath(self.filepath)

        # ---- extract geometry from Blender ----
        try:
            if self.encode_mode in ('cgml',):
                # CGML new-file mode: split by material
                submeshes = mesh_from_blender_object(
                    obj, apply_transforms=True, split_by_material=True)
                if not submeshes:
                    self.report({'ERROR'}, "No geometry found in material slots.")
                    return {'CANCELLED'}
            else:
                # All other modes: single mesh
                verts, faces, uvs = mesh_from_blender_object(
                    obj, apply_transforms=True, split_by_material=False)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        # ---- apply uniform scale (non-patch modes) ----
        if self.encode_mode not in ('primary_described_patch',):
            if self.scale != 1.0:
                s = self.scale
                if self.encode_mode == 'cgml':
                    submeshes = [
                        ([(x*s, y*s, z*s) for x, y, z in v], f, u)
                        for v, f, u in submeshes
                    ]
                else:
                    verts = [(x*s, y*s, z*s) for x, y, z in verts]

        # ---- encode ----
        try:
            cn = self.compute_normals

            # ================================================================
            # HEURISTIC MODES (no Primary file needed)
            # ================================================================
            if self.encode_mode == 'heuristic_s16':
                gpu_data = encode_heuristic_s16(verts, faces, uvs=uvs, compute_normals=cn)
                self._write_gpu(out_path, gpu_data)
                self._report_ok(obj.name, verts, faces, out_path)

            elif self.encode_mode == 'heuristic_s20':
                gpu_data = encode_heuristic_s20(verts, faces, uvs=uvs, compute_normals=cn)
                self._write_gpu(out_path, gpu_data)
                self._report_ok(obj.name, verts, faces, out_path)

            elif self.encode_mode == 'heuristic_dual28':
                gpu_data = encode_heuristic_dual28(verts, faces, compute_normals=cn)
                self._write_gpu(out_path, gpu_data)
                self._report_ok(obj.name, verts, faces, out_path)

            # ================================================================
            # PRIMARY DESCRIBED (in-place patch, auto-detect CGML vs CIMR)
            # ================================================================
            elif self.encode_mode == 'primary_described':
                s0_stride = None if self.stream0_stride == 'auto' else int(self.stream0_stride)
                gpu_path = out_path
                from .primary import _find_primary_path
                primary_path = _find_primary_path(gpu_path)

                if not primary_path or not os.path.exists(primary_path):
                    self.report({'ERROR'},
                        "Could not find sibling Primary file. "
                        "Ensure the original Primary file exists alongside the GPU file, "
                        "or use a heuristic encode mode instead.")
                    return {'CANCELLED'}

                if not os.path.exists(gpu_path):
                    self.report({'ERROR'}, f"Original GPU file not found: {gpu_path}")
                    return {'CANCELLED'}

                with open(gpu_path, 'rb') as f:
                    orig_gpu = f.read()
                with open(primary_path, 'rb') as f:
                    orig_primary = f.read()

                if len(orig_primary) < 64:
                    self.report({'ERROR'},
                        f"Primary file is too small ({len(orig_primary)} bytes). "
                        "Expected at least 64 bytes for a valid descriptor.")
                    return {'CANCELLED'}

                # ---- DETECT: CGML or CIMR? ----
                is_cgml = _is_cgml_primary(orig_primary)
                # Auto-detect stride if needed
                if s0_stride is None:
                    if is_cgml:
                        s0_stride = _detect_cgml_stride(orig_primary)
                    else:
                        s0_stride = _detect_s0_stride_from_primary(orig_primary)
                    if s0_stride is None:
                        s0_stride = 16  # safe fallback

                try:
                    if is_cgml:
                        # CGML path: get Blender submeshes split by material
                        submesh_list = mesh_from_blender_object(
                            obj, apply_transforms=True, split_by_material=True)
                        
                        if not submesh_list:
                            self.report({'ERROR'}, "No geometry found in mesh.")
                            return {'CANCELLED'}
                        
                        # Count Array 2 entries for reporting
                        num_slots = 0
                        off = 0
                        sizes = (0x98, 0x70, 0x150, 0x150, 0x10, 0x10, 0x04, 0x04, 0x10, 0x18)
                        for idx, stride in enumerate(sizes):
                            if off + 4 > len(orig_primary):
                                break
                            count = struct.unpack_from("<I", orig_primary, off)[0]
                            if idx == 2:
                                num_slots = count
                            off = off + 4 + count * stride
                        
                        gpu_data, primary_data = encode_cgml_inplace_replace(
                            orig_gpu, orig_primary,
                            submesh_list,
                            stream0_stride=s0_stride,
                            compute_normals=cn
                        )
                        self._write_gpu(gpu_path, gpu_data)
                        with open(primary_path, 'wb') as f:
                            f.write(primary_data)
                        
                        first_sub = submesh_list[0]
                        first_verts = first_sub[0] if len(first_sub) >= 1 else []
                        first_faces = first_sub[1] if len(first_sub) >= 2 else []
                        self.report({'INFO'},
                            f"CGML patched: {len(first_verts)}v {len(first_faces)}f "
                            f"×{num_slots} slots → "
                            f"{os.path.basename(primary_path)}")
                        patched = True
                    else:
                        # CIMR path: check if multi-block
                        block_count = _count_cimr_blocks(orig_primary)
                        if block_count > 1:
                            # Multi-block CIMR (LODs, shadow geometry, etc.)
                            submesh_list = mesh_from_blender_object(
                                obj, apply_transforms=True, split_by_material=True)
                            gpu_data, primary_data = encode_primary_described_multi_submesh_replace(
                                orig_gpu, orig_primary,
                                submesh_list,
                                stream0_stride=s0_stride,
                                compute_normals=cn
                            )
                            self.report({'INFO'},
                                f"CIMR multi-block patched: {block_count} blocks, "
                                f"{len(submesh_list)} material submeshes → "
                                f"{os.path.basename(primary_path)}")
                        else:
                            # Single-block CIMR (simple prop/hero)
                            gpu_data, primary_data = encode_primary_described_full_replace(
                                orig_gpu, orig_primary,
                                verts, faces, uvs=uvs,
                                stream0_stride=s0_stride,
                                compute_normals=cn
                            )
                            self.report({'INFO'},
                                f"CIMR single-block patched: {len(verts)}v {len(faces)}f → "
                                f"{os.path.basename(primary_path)}")

                        self._write_gpu(gpu_path, gpu_data)
                        with open(primary_path, 'wb') as f:
                            f.write(primary_data)
                        patched = True

                except ValueError as e:
                    self.report({'ERROR'}, f"Patch failed: {e}")
                    return {'CANCELLED'}
                except Exception as e:
                    self.report({'ERROR'}, f"Unexpected error during patch: {e}")
                    import traceback
                    traceback.print_exc()
                    return {'CANCELLED'}

            # ================================================================
            # PRIMARY DESCRIBED PATCH (scale only, no geometry change)
            # ================================================================
            elif self.encode_mode == 'primary_described_patch':
                gpu_path = out_path
                from .primary import _find_primary_path
                primary_path = _find_primary_path(gpu_path)

                if not primary_path or not os.path.exists(primary_path):
                    self.report({'ERROR'},
                        "Could not locate sibling Primary file. "
                        "Ensure the original Primary file is in the corresponding sibling hash directory.")
                    return {'CANCELLED'}

                if not os.path.exists(gpu_path):
                    self.report({'ERROR'}, f"Original GPU file not found: {gpu_path}")
                    return {'CANCELLED'}

                with open(gpu_path, 'rb') as f:
                    orig_gpu = f.read()
                with open(primary_path, 'rb') as f:
                    orig_primary = f.read()

                try:
                    patched_gpu, _ = patch_primary_described_positions(
                        orig_gpu, orig_primary,
                        scale_x=self.scale_x,
                        scale_y=self.scale_y,
                        scale_z=self.scale_z
                    )
                except ValueError as e:
                    self.report({'ERROR'}, f"In-place patch failed: {e}")
                    return {'CANCELLED'}

                self._write_gpu(gpu_path, patched_gpu)
                self.report({'INFO'},
                    f"Scaled vertex positions in {os.path.basename(gpu_path)}. "
                    f"Primary file untouched.")

            # ================================================================
            # CGML (new file, no patching)
            # ================================================================
            elif self.encode_mode == 'cgml':
                gpu_data = encode_cgml(submeshes, compute_normals=cn)
                self._write_gpu(out_path, gpu_data)
                total_v = sum(len(v) for v, _, _ in submeshes)
                total_f = sum(len(f) for _, f, _ in submeshes)
                self.report({'INFO'},
                    f"{obj.name}: {len(submeshes)} submesh(es) "
                    f"{total_v}v {total_f}f → {os.path.basename(out_path)} [cgml]")

        except Exception as e:
            self.report({'ERROR'}, f"Encode failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}

    # ---- helpers ----

    def _write_gpu(self, path, data):
        with open(path, 'wb') as f:
            f.write(data)

    def _report_ok(self, name, verts, faces, path):
        self.report({'INFO'},
            f"{name}: {len(verts)}v {len(faces)}f → "
            f"{os.path.basename(path)} [{self.encode_mode}]")

    def _primary_sibling_path(self, gpu_path):
        """
        Mirror the GPU path to the sibling Primary directory.
        """
        fname = os.path.basename(gpu_path)
        parent = os.path.dirname(gpu_path)
        grandparent = os.path.dirname(parent)
        ggp = os.path.dirname(grandparent)

        gpu_to_primary = {
            "e7a8ab5ceaef49cb": "37102e4b27955a14",
            "e642bfb1abcf76df": "4e426f88c1b5d7ac",
            "CGMeshListResource": "CGMeshListResource",
            "CGInstancedModelResource": "CGInstancedModelResource",
        }
        parent_folder = os.path.basename(parent)
        primary_folder = gpu_to_primary.get(parent_folder, "37102e4b27955a14")

        # 1. Flat hash layout
        candidate1 = os.path.join(grandparent, primary_folder, fname)
        if os.path.isdir(os.path.dirname(candidate1)):
            return candidate1

        # 2. Nested hash layout
        candidate2 = os.path.join(ggp, "Primary", primary_folder, fname)
        if os.path.isdir(os.path.dirname(candidate2)) or os.path.basename(grandparent) == "GPU":
            return candidate2

        # 3. Default fallback
        gpu_dir_name = os.path.basename(grandparent)
        if gpu_dir_name != "GPU":
            if "GPU" in parent:
                candidate = gpu_path.replace(
                    os.path.join("GPU", os.path.basename(parent)),
                    os.path.join("Primary", os.path.basename(parent)),
                    1,
                )
                if candidate != gpu_path:
                    return candidate
            return None

        family = os.path.basename(parent)
        return os.path.join(ggp, "Primary", family, fname)

    def draw(self, context):
        layout = self.layout

        layout.label(text="Encode Mode")
        layout.prop(self, "encode_mode", text="")

        layout.separator()
        if self.encode_mode == 'primary_described_patch':
            layout.prop(self, "scale_x")
            layout.prop(self, "scale_y")
            layout.prop(self, "scale_z")
        else:
            layout.prop(self, "compute_normals")
            layout.prop(self, "scale")

        if self.encode_mode == 'primary_described':
            layout.separator()
            layout.label(text="Primary Described Options")
            layout.prop(self, "write_primary")
            layout.prop(self, "stream0_stride")


def menu_func_export(self, context):
    self.layout.operator(EVR_OT_ExportMesh.bl_idname, text="EVR Raw Mesh (Replace)")


# ============================================================
# Registration
# ============================================================

def register():
    bpy.utils.register_class(EVR_OT_ImportMesh)
    bpy.utils.register_class(EVR_OT_ExportMesh)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(EVR_OT_ImportMesh)
    bpy.utils.unregister_class(EVR_OT_ExportMesh)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)


if __name__ == "__main__":
    register()