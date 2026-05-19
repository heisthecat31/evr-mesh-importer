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

_submodules = ["decode", "primary", "encode"]
for _sub in _submodules:
    _full_name = f"{__package__}.{_sub}" if __package__ else _sub
    if _full_name in sys.modules:
        importlib.reload(sys.modules[_full_name])

from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (StringProperty, BoolProperty, FloatProperty, CollectionProperty, EnumProperty)
from bpy.types import Operator

from .decode import extract_mesh
from .primary import _find_primary_data
from .encode import (
    encode_heuristic_s16,
    encode_heuristic_s20,
    encode_heuristic_dual28,
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
    bl_idname = "import_mesh.evr_raw"
    bl_label = "EVR Raw Mesh"
    bl_options = {'REGISTER', 'UNDO'}
    filter_glob: StringProperty(default="*", options={'HIDDEN'}, maxlen=255)
    files: CollectionProperty(name="File Path", type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')
    primary_file: StringProperty(name="Primary File", default="", subtype='FILE_PATH')
    auto_find_primary: BoolProperty(name="Auto-find Primary", default=True)
    use_smooth: BoolProperty(name="Smooth Shading", default=False)
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=10000.0)

    def execute(self, context):
        paths = [os.path.join(self.directory, f.name) for f in self.files] if self.files else [self.filepath]
        primary_data = None
        if self.primary_file.strip():
            ppath = bpy.path.abspath(self.primary_file)
            if os.path.isfile(ppath):
                with open(ppath, "rb") as fh: primary_data = fh.read()
        
        all_roots = []
        for gpu_path in paths:
            root = self._import_single(context, gpu_path, primary_data)
            if root: all_roots.append(root)

        for obj in context.selected_objects: obj.select_set(False)
        for root in all_roots: root.select_set(True)
        if all_roots: context.view_layer.objects.active = all_roots[-1]
        return {'FINISHED'} if all_roots else {'CANCELLED'}

    def _import_single(self, context, gpu_path, primary_data=None):
        if not os.path.isfile(gpu_path): return None
        try:
            submeshes, path_label = extract_mesh(gpu_path, primary_data=primary_data, auto_find_primary=(primary_data is None and self.auto_find_primary))
        except Exception as exc:
            self.report({'ERROR'}, f"Decode failed: {exc}")
            return None
        if not submeshes: return None
        
        base_name = os.path.splitext(os.path.basename(gpu_path))[0]
        valid = [(sub[0], sub[1]) for sub in submeshes if len(sub) >= 2 and sub[0] and sub[1]]
        parent_empty = None
        if len(valid) > 1:
            parent_empty = bpy.data.objects.new(base_name, None)
            bpy.context.collection.objects.link(parent_empty)

        created = []
        for idx, sub in enumerate(submeshes):
            verts, faces, uvs = sub if len(sub) == 3 else (sub[0], sub[1], None)
            if not verts or not faces: continue
            if self.scale != 1.0: verts = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in verts]
            name = base_name if parent_empty is None else f"{base_name}.{idx:03d}"
            obj = _build_blender_mesh(verts, faces, name)
            if uvs and obj.data:
                uv_layer = obj.data.uv_layers.new(name="UVMap")
                for poly in obj.data.polygons:
                    for loop_idx in poly.loop_indices:
                        v_idx = obj.data.loops[loop_idx].vertex_index
                        if v_idx < len(uvs): uv_layer.data[loop_idx].uv = uvs[v_idx]
            if not self.use_smooth: _apply_flat_shading(obj)
            if parent_empty: obj.parent = parent_empty
            created.append(obj)
            
        return parent_empty if parent_empty else created[0] if created else None

def menu_func_import(self, context): self.layout.operator(EVR_OT_ImportMesh.bl_idname, text="EVR Raw Mesh")

class EVR_OT_ExportMesh(Operator, ExportHelper):
    bl_idname = "export_mesh.evr_raw"
    bl_label = "EVR Raw Mesh (Replace)"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ""
    filter_glob: StringProperty(default="*", options={'HIDDEN'}, maxlen=255)

    encode_mode: EnumProperty(
        name="Encode Mode",
        items=[
            ('heuristic_s16', "Heuristic — stride-16", ""),
            ('heuristic_s20', "Heuristic — stride-20", ""),
            ('heuristic_dual28', "Heuristic — dual-28", ""),
            ('primary_described', "Primary Described (Full Patch)", "Patches the original Primary template to safely preserve game collision data"),
            ('cgml', "CGML — map geometry", ""),
        ],
        default='heuristic_s16',
    )
    compute_normals: BoolProperty(name="Compute Normals", default=True)
    write_primary: BoolProperty(name="Write Primary File", default=True)
    stream0_stride: EnumProperty(name="Stream-0 Stride", items=[('auto', "Auto-Detect", ""), ('16', "16 bytes", ""), ('20', "20 bytes", "")], default='auto')
    scale: FloatProperty(name="Scale", default=1.0)

    @classmethod
    def poll(cls, context): return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        out_path = bpy.path.abspath(self.filepath)
        try:
            if self.encode_mode == 'cgml':
                submeshes = mesh_from_blender_object(obj, apply_transforms=True, split_by_material=True)
            else:
                verts, faces, uvs = mesh_from_blender_object(obj, apply_transforms=True, split_by_material=False)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if self.scale != 1.0:
            s = self.scale
            if self.encode_mode == 'cgml': submeshes = [([(x*s, y*s, z*s) for x,y,z in v], f, u) for v,f,u in submeshes]
            else: verts = [(x*s, y*s, z*s) for x,y,z in verts]

        try:
            cn = self.compute_normals
            if self.encode_mode == 'heuristic_s16':
                self._write_gpu(out_path, encode_heuristic_s16(verts, faces, compute_normals=cn))
            elif self.encode_mode == 'heuristic_s20':
                self._write_gpu(out_path, encode_heuristic_s20(verts, faces, compute_normals=cn))
            elif self.encode_mode == 'heuristic_dual28':
                self._write_gpu(out_path, encode_heuristic_dual28(verts, faces, compute_normals=cn))
            elif self.encode_mode == 'cgml':
                self._write_gpu(out_path, encode_cgml(submeshes, compute_normals=cn))
            elif self.encode_mode == 'primary_described':
                s0_stride = None if self.stream0_stride == 'auto' else int(self.stream0_stride)
                gpu_path = out_path
                from .primary import _find_primary_path
                primary_path = _find_primary_path(gpu_path)
                
                if primary_path and os.path.exists(primary_path) and os.path.exists(gpu_path):
                    with open(gpu_path, 'rb') as f: orig_gpu = f.read()
                    with open(primary_path, 'rb') as f: orig_primary = f.read()
                    
                    from .encode import encode_primary_described_full_replace
                    try:
                        gpu_data, primary_data = encode_primary_described_full_replace(
                            orig_gpu, orig_primary, verts, faces, uvs=uvs, stream0_stride=s0_stride, compute_normals=cn
                        )
                        self._write_gpu(gpu_path, gpu_data)
                        if self.write_primary:
                            with open(primary_path, 'wb') as f: f.write(primary_data)
                            self.report({'INFO'}, f"Wrote rebuilt GPU and patched Primary in-place: {os.path.basename(primary_path)}")
                    except ValueError as e:
                        self.report({'ERROR'}, str(e))
                        return {'CANCELLED'}
                else:
                    self.report({'ERROR'}, "Could not find original GPU and Primary files for patching. Ensure both exist.")
                    return {'CANCELLED'}

        except Exception as e:
            self.report({'ERROR'}, f"Encode failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    def _write_gpu(self, path, data):
        with open(path, 'wb') as f: f.write(data)

def menu_func_export(self, context): self.layout.operator(EVR_OT_ExportMesh.bl_idname, text="EVR Raw Mesh (Replace)")

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

if __name__ == "__main__": register()