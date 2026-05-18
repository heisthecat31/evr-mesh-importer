"""
EVR Raw Mesh Importer/Exporter — Blender add-on
Imports and exports raw GPU mesh binaries from Echo VR (Echo Arena).
https://github.com/Dualgame/evr-mesh-importer
"""

bl_info = {
    "name": "EVR Raw Mesh Importer",
    "author": "Dualgame",
    "version": (1, 1, 0),
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
from .encode import (
    encode_heuristic_s16,
    encode_heuristic_s20,
    encode_heuristic_dual28,
    encode_primary_described,
    encode_cgml,
    mesh_from_blender_object,
)


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

    @classmethod
    def poll(cls, context):
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

            # Assign UVs if present in the imported data
            if uvs and obj.data:
                uv_layer = obj.data.uv_layers.new(name="UVMap")
                for poly in obj.data.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = obj.data.loops[loop_idx]
                        v_idx = loop.vertex_index
                        if v_idx < len(uvs):
                            uv_layer.data[loop_idx].uv = uvs[v_idx]

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

        return parent_empty if parent_empty is not None else created[0]

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "auto_find_primary")
        layout.prop(self, "primary_file")
        layout.separator()
        layout.prop(self, "use_smooth")
        layout.prop(self, "scale")


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

    # ExportHelper sets self.filepath from this
    filename_ext = ""   # EVR binaries have no extension
    filter_glob: StringProperty(default="*", options={'HIDDEN'}, maxlen=255)

    # ---- encode mode ----
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
            ('primary_described', "Primary Described (also writes Primary file)",
             "Most accurate. Also writes a matching Primary binary alongside "
             "the GPU file. Use for files decoded as 'primary_described'."),
            ('primary_described_patch', "Primary Described (In-place Patch)",
             "Surgically patches vertex positions inside the original GPU binary in-place, "
             "leaving file size and LODs/shadow geometry completely original. "
             "Use to prevent load crashes on complex instanced props."),
            ('cgml',            "CGML — map geometry (multi-submesh)",
             "Encodes each material slot as a separate submesh. "
             "Use for CGMeshListResource map files."),
        ],
        default='heuristic_s16',
    )

    # ---- stream-1 options ----
    compute_normals: BoolProperty(
        name="Compute Normals",
        description=(
            "Compute smooth vertex normals from mesh topology and pack them "
            "into stream-1 (snorm16). Disable to zero stream-1 normals — "
            "safe for a first geometry test, but lighting will be wrong in-game."
        ),
        default=True,
    )

    # ---- primary options (primary_described mode only) ----
    write_primary: BoolProperty(
        name="Write Primary File",
        description=(
            "Also write a matching Primary binary to the sibling Primary/ "
            "directory. Required for 'primary_described' mode. The importer "
            "uses the same directory convention for auto-discovery."
        ),
        default=True,
    )

    stream0_stride: EnumProperty(
        name="Stream-0 Stride",
        description="Stride of the original file's stream-0 (Auto-detect, 16, or 20 bytes).",
        items=[
            ('auto', "Auto-Detect", "Automatically detect from the original Primary template file"),
            ('16', "16 bytes", "Original used stride-16 stream-0"),
            ('20', "20 bytes", "Original used stride-20 stream-0"),
        ],
        default='auto',
    )

    # ---- transform ----
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
            if self.encode_mode == 'cgml':
                submeshes = mesh_from_blender_object(
                    obj, apply_transforms=True, split_by_material=True)
                if not submeshes:
                    self.report({'ERROR'}, "No geometry found in material slots.")
                    return {'CANCELLED'}
            else:
                verts, faces, uvs = mesh_from_blender_object(
                    obj, apply_transforms=True, split_by_material=False)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        # ---- apply scale ----
        if self.encode_mode == 'cgml':
            if self.scale != 1.0:
                s = self.scale
                submeshes = [
                    ([(x*s, y*s, z*s) for x, y, z in v], f, u)
                    for v, f, u in submeshes
                ]
        else:
            if self.scale != 1.0:
                s = self.scale
                verts = [(x*s, y*s, z*s) for x, y, z in verts]

        # ---- encode ----
        try:
            cn = self.compute_normals

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

            elif self.encode_mode == 'primary_described':
                s0_stride = None if self.stream0_stride == 'auto' else int(self.stream0_stride)
                gpu_path = out_path
                from .primary import _find_primary_path
                primary_path = _find_primary_path(gpu_path)
                
                patched = False
                if primary_path and os.path.exists(primary_path) and os.path.exists(gpu_path):
                    with open(gpu_path, 'rb') as f:
                        orig_gpu = f.read()
                    with open(primary_path, 'rb') as f:
                        orig_primary = f.read()
                    
                    if len(orig_primary) > 64:
                        # Count how many rendering blocks are in the original Primary template
                        block_count = 0
                        import struct
                        n_meta = len(orig_primary)
                        for off in range(0, n_meta - 60, 4):
                            val = struct.unpack_from('<I', orig_primary, off)[0]
                            if val == 0x0B:
                                vc = struct.unpack_from('<I', orig_primary, off + 7*4)[0]
                                vc2 = struct.unpack_from('<I', orig_primary, off + 8*4)[0]
                                vc3 = struct.unpack_from('<I', orig_primary, off + 11*4)[0]
                                if vc == vc2 == vc3 and vc > 0:
                                    block_count += 1
                        
                        try:
                            if block_count > 1:
                                # Multi-submesh prop model!
                                submesh_list = mesh_from_blender_object(
                                    obj, apply_transforms=True, split_by_material=True)
                                from .encode import encode_primary_described_multi_submesh_replace
                                gpu_data, primary_data = encode_primary_described_multi_submesh_replace(
                                    orig_gpu, orig_primary,
                                    submesh_list,
                                    stream0_stride=s0_stride,
                                    compute_normals=cn
                                )
                                self.report({'INFO'}, f"Auto-detected multi-submesh prop ({block_count} submeshes).")
                            else:
                                # Single-submesh hero/cosmetic model
                                from .encode import encode_primary_described_full_replace
                                gpu_data, primary_data = encode_primary_described_full_replace(
                                    orig_gpu, orig_primary,
                                    verts, faces, uvs=uvs,
                                    stream0_stride=s0_stride,
                                    compute_normals=cn
                                )
                            
                            self._write_gpu(gpu_path, gpu_data)
                            with open(primary_path, 'wb') as f:
                                f.write(primary_data)
                            self.report({'INFO'}, f"Wrote rebuilt GPU and patched original Primary template in-place: {os.path.basename(primary_path)}")
                            patched = True
                        except Exception as e:
                            self.report({'WARNING'}, f"Template-based replacement failed: {e}. Falling back to standard export.")
                
                if not patched:
                    gpu_data, primary_data = encode_primary_described(
                        verts, faces, uvs=uvs, stream0_stride=s0_stride, compute_normals=cn)
                    self._write_gpu(gpu_path, gpu_data)
                    if self.write_primary:
                        ppath = self._primary_sibling_path(gpu_path)
                        if ppath:
                            os.makedirs(os.path.dirname(ppath), exist_ok=True)
                            with open(ppath, 'wb') as f:
                                f.write(primary_data)
                            self.report({'INFO'}, f"Wrote rebuilt GPU and clean 64-byte Primary descriptor: {os.path.basename(ppath)}")
                        else:
                            self.report({'WARNING'}, "Could not determine Primary path from GPU path. Primary skipped.")
                    else:
                        self._report_ok(obj.name, verts, faces, gpu_path)

            elif self.encode_mode == 'primary_described_patch':
                gpu_path = out_path
                from .primary import _find_primary_path
                primary_path = _find_primary_path(gpu_path)
                
                if not primary_path or not os.path.exists(primary_path):
                    self.report({'ERROR'}, "Could not locate sibling Primary file via auto-discovery. "
                                           "Ensure the original Primary file is in the corresponding sibling hash directory.")
                    return {'CANCELLED'}
                
                if not os.path.exists(gpu_path):
                    self.report({'ERROR'}, f"Original GPU file not found: {gpu_path}")
                    return {'CANCELLED'}
                
                with open(gpu_path, 'rb') as f:
                    orig_gpu = f.read()
                with open(primary_path, 'rb') as f:
                    orig_primary = f.read()
                
                from .encode import patch_primary_described_positions
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
                self.report({'INFO'}, f"Surgically scaled vertex positions in {os.path.basename(gpu_path)}. "
                                      f"Primary file remained untouched.")

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
        Supports both flat hash layout (e7a8ab5ceaef49cb -> 37102e4b27955a14) and nested layout.
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

        # 1. Flat hash layout: any/parent/fname -> any/sibling/fname
        candidate1 = os.path.join(grandparent, primary_folder, fname)
        if os.path.isdir(os.path.dirname(candidate1)):
            return candidate1

        # 2. Nested hash layout: any/GPU/parent/fname -> any/Primary/sibling/fname
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
